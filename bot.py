"""
Telegram-бот для расчёта доходности ASIC-майнеров.

Поддерживаемые монеты/алгоритмы:
  - BTC (SHA-256)  — Bitcoin ASIC (Antminer S19/S21 и т.п.)
  - LTC (Scrypt)   — Litecoin ASIC (Antminer L7 и т.п.)
  - DOGE (Scrypt)  — Dogecoin ASIC (обычно те же устройства, что и LTC)

Как это работает:
1. Пользователь пишет /start
2. Выбирает монету кнопкой
3. Выбирает готовую модель асика из списка ИЛИ вводит хешрейт/потребление
   вручную
4. Вводит цену асика (руб) и тариф на э/энергию (руб/кВт·ч)
5. Бот берёт курс монеты и сложность сети через открытые официальные API
   (CoinGecko — курс, Blockchair — сложность сети) и считает доходность

Модели асиков и их характеристики (хешрейт/потребление) обновлены на
момент написания бота вручную — производители выпускают новые модели
регулярно, поэтому раз в несколько месяцев стоит свериться с актуальными
характеристиками и обновить список MODELS ниже.

Если запрос к API не прошёл (нет интернета на сервере, лимиты и т.п.),
бот сообщает об этом и просит попробовать позже — расчёт не делается
на "выдуманных" цифрах, чтобы не давать пользователю неверную информацию.
"""

import logging
import requests
from telegram import (
    Update,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ==== НАСТРОЙКИ ====
BOT_TOKEN = "8868576930:AAFs7Ebe6SRXSd2o_QO2bwMxvlxYzFGYT_Q"  # <-- вставьте токен, который дал BotFather

# Конфигурация поддерживаемых монет.
# block_reward обновляется только на халвинге (редкое, известное заранее
# событие) — для BTC следующий халвинг в 2028 году, для LTC в 2027,
# у DOGE награда фиксирована навсегда.
COINS = {
    "BTC": {
        "title": "Bitcoin (BTC, SHA-256)",
        "algorithm": "SHA-256",
        "coingecko_id": "bitcoin",
        "blockchair_chain": "bitcoin",
        "block_reward": 3.125,
        "hr_unit": "TH/s",
        "hr_multiplier": 1e12,
        "hr_example": "110",
    },
    "LTC": {
        "title": "Litecoin (LTC, Scrypt)",
        "algorithm": "Scrypt",
        "coingecko_id": "litecoin",
        "blockchair_chain": "litecoin",
        "block_reward": 6.25,
        "hr_unit": "MH/s",
        "hr_multiplier": 1e6,
        "hr_example": "9500",
    },
    "DOGE": {
        "title": "Dogecoin (DOGE, Scrypt)",
        "algorithm": "Scrypt",
        "coingecko_id": "dogecoin",
        "blockchair_chain": "dogecoin",
        "block_reward": 10000,
        "hr_unit": "MH/s",
        "hr_multiplier": 1e6,
        "hr_example": "9500",
    },
}

# Готовые модели асиков по алгоритмам: (название, хешрейт, потребление в Вт).
# Хешрейт указан в тех же единицах, что и hr_unit соответствующей монеты
# (TH/s для SHA-256, MH/s для Scrypt).
MODELS = {
    "SHA-256": [
        ("Antminer S19 XP — 141 TH/s / 3010 Вт", 141, 3010),
        ("Antminer S21 — 200 TH/s / 3550 Вт", 200, 3550),
        ("Antminer S21 XP — 270 TH/s / 3645 Вт", 270, 3645),
        ("Antminer S21 XP Hyd — 473 TH/s / 5676 Вт", 473, 5676),
    ],
    "Scrypt": [
        ("Antminer L7 8.8 GH/s / 3168 Вт", 8800, 3168),
        ("Antminer L7 9.3 GH/s / 3425 Вт", 9300, 3425),
        ("Antminer L7 9.5 GH/s / 3425 Вт", 9500, 3425),
    ],
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Состояния диалога
CHOOSING_COIN, CHOOSING_MODEL, HASHRATE, POWER, PRICE, TARIFF = range(6)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton(cfg["title"], callback_data=code)]
        for code, cfg in COINS.items()
    ]
    await update.message.reply_text(
        "Привет! Посчитаю доходность и окупаемость ASIC-майнера.\n\n"
        "Какую монету считаем?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHOOSING_COIN


async def choose_coin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    coin_code = query.data
    coin = COINS[coin_code]
    context.user_data["coin_code"] = coin_code

    await query.edit_message_text(f"Монета: {coin['title']}")

    models = MODELS[coin["algorithm"]]
    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"m{idx}")]
        for idx, (label, _, _) in enumerate(models)
    ]
    keyboard.append(
        [InlineKeyboardButton("✍️ Ввести вручную", callback_data="manual")]
    )
    await query.message.reply_text(
        "Выберите модель асика из списка или введите параметры вручную:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHOOSING_MODEL


async def choose_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    coin = COINS[context.user_data["coin_code"]]

    if query.data == "manual":
        await query.edit_message_text("Ввод вручную")
        await query.message.reply_text(
            f"Введите хешрейт устройства в {coin['hr_unit']} "
            f"(например: {coin['hr_example']})",
            reply_markup=ReplyKeyboardRemove(),
        )
        return HASHRATE

    idx = int(query.data[1:])
    label, hashrate, power_w = MODELS[coin["algorithm"]][idx]
    context.user_data["hashrate"] = hashrate
    context.user_data["power_w"] = power_w

    await query.edit_message_text(f"Модель: {label}")
    await query.message.reply_text("Введите цену устройства в рублях (например: 250000)")
    return PRICE


async def get_hashrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    coin = COINS[context.user_data["coin_code"]]
    try:
        value = float(update.message.text.replace(",", "."))
        if value <= 0:
            raise ValueError
        context.user_data["hashrate"] = value
    except ValueError:
        await update.message.reply_text(
            f"Нужно число больше нуля, например: {coin['hr_example']}. "
            f"Попробуйте ещё раз:"
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
            coin_code=context.user_data["coin_code"],
            hashrate=context.user_data["hashrate"],
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


def get_price_rub(coingecko_id: str) -> float:
    """Курс монеты в рублях через CoinGecko (официальный публичный API)."""
    r = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": coingecko_id, "vs_currencies": "rub"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()[coingecko_id]["rub"]


def get_network_difficulty(blockchair_chain: str) -> float:
    """Текущая сложность сети через Blockchair (официальный публичный API,
    покрывает Bitcoin, Litecoin, Dogecoin и другие UTXO-цепочки)."""
    r = requests.get(
        f"https://api.blockchair.com/{blockchair_chain}/stats", timeout=10
    )
    r.raise_for_status()
    return float(r.json()["data"]["difficulty"])


def calculate_profitability(
    coin_code: str,
    hashrate: float,
    power_w: float,
    price_rub: float,
    tariff_rub_kwh: float,
) -> str:
    coin = COINS[coin_code]

    coin_price_rub = get_price_rub(coin["coingecko_id"])
    difficulty = get_network_difficulty(coin["blockchair_chain"])

    hashrate_hs = hashrate * coin["hr_multiplier"]

    # Стандартная формула ожидаемого дохода в монетах/день.
    # Она не зависит от времени блока — сложность уже нормирует его.
    coins_per_day = (hashrate_hs * 86400 * coin["block_reward"]) / (
        difficulty * 2**32
    )

    revenue_rub_day = coins_per_day * coin_price_rub

    energy_kwh_day = (power_w / 1000) * 24
    energy_cost_day = energy_kwh_day * tariff_rub_kwh

    profit_day = revenue_rub_day - energy_cost_day
    profit_month = profit_day * 30

    if profit_day <= 0:
        payback_text = (
            "при текущих условиях устройство не окупается "
            "(расход на электричество выше дохода)"
        )
    else:
        payback_months = price_rub / profit_month
        payback_text = f"≈ {payback_months:.1f} мес."

    return (
        f"*Результат расчёта — {coin['title']}*\n\n"
        f"Курс: {coin_price_rub:,.2f} ₽\n"
        f"Ожидаемый доход: {coins_per_day:.6f} {coin_code}/день "
        f"≈ {revenue_rub_day:,.0f} ₽/день\n"
        f"Расход на э/энергию: {energy_cost_day:,.0f} ₽/день "
        f"({energy_kwh_day:.1f} кВт·ч)\n\n"
        f"*Чистая прибыль:*\n"
        f"– в день: {profit_day:,.0f} ₽\n"
        f"– в месяц: {profit_month:,.0f} ₽\n\n"
        f"*Окупаемость:* {payback_text}\n\n"
        f"_Расчёт по официальным данным сети (сложность майнинга + текущий "
        f"курс). Не учитывает комиссию пула, рост сложности и курс в "
        f"будущем. Для нового расчёта — /start_"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено. Наберите /start, чтобы начать заново.")
    return ConversationHandler.END


def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_COIN: [CallbackQueryHandler(choose_coin)],
            CHOOSING_MODEL: [CallbackQueryHandler(choose_model)],
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
