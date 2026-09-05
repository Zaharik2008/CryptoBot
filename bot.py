"""
Telegram-бот для расчёта доходности ASIC-майнеров.

Как это работает:
1. Пользователь пишет /start
2. Бот спрашивает: хешрейт (TH/s), потребление (Вт), цену асика (руб),
   тариф на электричество (руб/кВт·ч)
3. Бот берёт текущий курс BTC и сложность сети через открытые API
   (mempool.space и coingecko) и считает:
   - ожидаемый доход в BTC/день
   - доход в рублях/день
   - расход на электричество/день
   - чистую прибыль/день и /месяц
   - срок окупаемости в месяцах

Если запрос к API не прошёл (нет интернета на сервере, лимиты и т.п.),
бот сообщает об этом и просит попробовать позже — расчёт не делается
на "выдуманных" цифрах, чтобы не давать пользователю неверную информацию.
"""

import logging
import requests
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ==== НАСТРОЙКИ ====
BOT_TOKEN = "8868576930:AAFs7Ebe6SRXSd2o_QO2bwMxvlxYzFGYT_Q"  # <-- вставьте токен, который дал BotFather

# Награда за блок BTC (обновляется примерно раз в 4 года, сейчас — 3.125)
BLOCK_REWARD = 3.125

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Состояния диалога
HASHRATE, POWER, PRICE, TARIFF = range(4)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Привет! Посчитаю доходность и окупаемость ASIC-майнера.\n\n"
        "Введите хешрейт устройства в TH/s (например: 110)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return HASHRATE


async def get_hashrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = float(update.message.text.replace(",", "."))
        if value <= 0:
            raise ValueError
        context.user_data["hashrate_th"] = value
    except ValueError:
        await update.message.reply_text(
            "Нужно число больше нуля, например: 110. Попробуйте ещё раз:"
        )
        return HASHRATE

    await update.message.reply_text("Теперь введите потребление в ваттах (например: 3250)")
    return POWER


async def get_power(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = float(update.message.text.replace(",", "."))
        if value <= 0:
            raise ValueError
        context.user_data["power_w"] = value
    except ValueError:
        await update.message.reply_text(
            "Нужно число больше нуля, например: 3250. Попробуйте ещё раз:"
        )
        return POWER

    await update.message.reply_text("Введите цену устройства в рублях (например: 250000)")
    return PRICE


async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = float(update.message.text.replace(",", "").replace(" ", ""))
        if value <= 0:
            raise ValueError
        context.user_data["price_rub"] = value
    except ValueError:
        await update.message.reply_text(
            "Нужно число больше нуля, например: 250000. Попробуйте ещё раз:"
        )
        return PRICE

    await update.message.reply_text(
        "И последнее: ваш тариф на электричество в руб/кВт·ч (например: 4.5)"
    )
    return TARIFF


async def get_tariff_and_calculate(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    try:
        tariff = float(update.message.text.replace(",", "."))
        if tariff <= 0:
            raise ValueError
        context.user_data["tariff"] = tariff
    except ValueError:
        await update.message.reply_text(
            "Нужно число больше нуля, например: 4.5. Попробуйте ещё раз:"
        )
        return TARIFF

    await update.message.reply_text("Считаю, секунду...")

    try:
        result = calculate_profitability(
            hashrate_th=context.user_data["hashrate_th"],
            power_w=context.user_data["power_w"],
            price_rub=context.user_data["price_rub"],
            tariff_rub_kwh=tariff,
        )
    except Exception as e:
        logger.error(f"Ошибка расчёта: {e}")
        await update.message.reply_text(
            "Не удалось получить актуальный курс/сложность сети (проблема с "
            "подключением к источникам данных). Попробуйте, пожалуйста, "
            "чуть позже — /start чтобы начать заново."
        )
        return ConversationHandler.END

    await update.message.reply_text(result, parse_mode="Markdown")
    return ConversationHandler.END


def get_btc_price_rub() -> float:
    """Курс BTC в рублях через CoinGecko."""
    r = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "bitcoin", "vs_currencies": "rub"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["bitcoin"]["rub"]


def get_network_difficulty() -> float:
    """Текущая сложность сети BTC через mempool.space."""
    r = requests.get("https://mempool.space/api/v1/difficulty-adjustment", timeout=10)
    r.raise_for_status()
    # У mempool.space нет "текущей сложности" напрямую в этом эндпоинте,
    # поэтому берём через blockchain.info как основной источник сложности.
    r2 = requests.get("https://blockchain.info/q/getdifficulty", timeout=10)
    r2.raise_for_status()
    return float(r2.text)


def calculate_profitability(
    hashrate_th: float, power_w: float, price_rub: float, tariff_rub_kwh: float
) -> str:
    btc_price_rub = get_btc_price_rub()
    difficulty = get_network_difficulty()

    hashrate_hs = hashrate_th * 1e12  # переводим TH/s в H/s

    # Стандартная формула ожидаемого дохода в BTC/день
    btc_per_day = (hashrate_hs * 86400 * BLOCK_REWARD) / (difficulty * 2**32)

    revenue_rub_day = btc_per_day * btc_price_rub

    energy_kwh_day = (power_w / 1000) * 24
    energy_cost_day = energy_kwh_day * tariff_rub_kwh

    profit_day = revenue_rub_day - energy_cost_day
    profit_month = profit_day * 30

    if profit_day <= 0:
        payback_text = "при текущих условиях устройство не окупается (расход на электричество выше дохода)"
    else:
        payback_months = price_rub / profit_month
        payback_text = f"≈ {payback_months:.1f} мес."

    return (
        f"*Результат расчёта*\n\n"
        f"Курс BTC: {btc_price_rub:,.0f} ₽\n"
        f"Ожидаемый доход: {btc_per_day:.8f} BTC/день ≈ {revenue_rub_day:,.0f} ₽/день\n"
        f"Расход на э/энергию: {energy_cost_day:,.0f} ₽/день ({energy_kwh_day:.1f} кВт·ч)\n\n"
        f"*Чистая прибыль:*\n"
        f"– в день: {profit_day:,.0f} ₽\n"
        f"– в месяц: {profit_month:,.0f} ₽\n\n"
        f"*Окупаемость:* {payback_text}\n\n"
        f"_Расчёт приблизительный: не учитывает комиссию пула, рост сложности "
        f"сети и курс в будущем. Для нового расчёта — /start_"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено. Наберите /start, чтобы начать заново.")
    return ConversationHandler.END


def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            HASHRATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_hashrate)],
            POWER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_power)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_price)],
            TARIFF: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, get_tariff_and_calculate
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
