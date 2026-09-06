"""
Telegram-бот для расчёта доходности ASIC-майнеров.

Поддерживаемые монеты/алгоритмы:
  - BTC (SHA-256)   — Bitcoin ASIC (Antminer, Whatsminer)
  - DOGE (Scrypt)   — Dogecoin ASIC (Antminer L7)
  - DASH (X11)      — Dash ASIC (Antminer D9)
  - ZEC (Equihash)  — Zcash ASIC (Antminer Z15-серия)

Как это работает:
1. Пользователь пишет /start
2. Выбирает монету кнопкой
3. Выбирает производителя ИЛИ вводит всё вручную
4. Выбирает модель (например, Antminer S21)
5. Если у модели несколько версий по хешрейту/охлаждению — выбирает
   нужную; если версия одна — бот подставляет её автоматически
6. Вводит цену асика (руб) и тариф на э/энергию (руб/кВт·ч)
7. Бот берёт курс монеты и сложность сети через открытые официальные API
   (CoinGecko — курс, Blockchair — сложность сети) и считает доходность

ВАЖНО про DASH: итоговая награда за блок делится между майнерами (~45%),
мастернодами (~45%) и treasury (~10%), и уменьшается примерно на 7.14%
раз в год (не классический халвинг). block_reward ниже — это уже доля
майнера на текущий момент, раз в несколько месяцев её стоит сверять и
обновлять.

ВАЖНО про ZEC: с ноября 2024 (апгрейд NU6) итоговый саб сидии — 1.5625
ZEC, из них майнерам достаётся 80% = 1.25 ZEC. Следующее плановое
изменение — ноябрь 2028, дальше можно не проверять сборку.

Характеристики моделей (хешрейт/потребление) обновлены на момент
написания бота вручную по официальным данным производителей и
авторизованных дилеров — раз в несколько месяцев стоит свериться с
актуальными характеристиками и обновить список MODELS ниже, так как
линейки регулярно пополняются.

Если запрос к API не прошёл (нет интернета на сервере, лимиты и т.п.),
бот сообщает об этом и просит попробовать позже — расчёт не делается
на "выдуманных" цифрах, чтобы не давать пользователю неверную информацию.
"""

import csv
import io
import logging
import os
from typing import Optional

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
BOT_TOKEN = "8868576930:AAEEvDk5B3VPPj33IkSMEbT1lSLgIZMX7Zk"

# Картинка-заставка для /start — должна лежать рядом с bot.py в репозитории
# (тот же файл, который вы загружаете на GitHub).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WELCOME_IMAGE_PATH = os.path.join(BASE_DIR, "welcome.jpg")
BANNER_COIN = os.path.join(BASE_DIR, "banner_coin.jpg")
BANNER_MANUFACTURER = os.path.join(BASE_DIR, "banner_manufacturer.jpg")
BANNER_MODEL = os.path.join(BASE_DIR, "banner_model.jpg")
BANNER_VARIANT = os.path.join(BASE_DIR, "banner_variant.jpg")

# Ссылка на прайс-лист в Google Таблице, опубликованный как CSV.
# Как получить: Файл → Поделиться → Опубликовать в интернете → выбрать
# нужный лист → формат CSV → Опубликовать → скопировать ссылку сюда.
# Таблица должна иметь два столбца без заголовка или с заголовком:
# Модель | Цена  (название модели должно точно совпадать с тем, что
# показывает бот — см. названия в MODELS ниже, например "Antminer S21").
# Если оставить пустой строкой — бот всегда будет спрашивать цену вручную.
PRICE_SHEET_CSV_URL = ""

# Канал, на который нужно подписаться, чтобы пользоваться ботом.
# ВАЖНО: бот должен быть добавлен в этот канал администратором — иначе
# Telegram не даст ему проверять, кто подписан.
REQUIRED_CHANNEL = "@crypto_point38"

# Конфигурация поддерживаемых монет.
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
    "DASH": {
        "title": "Dash (DASH, X11)",
        "algorithm": "X11",
        "coingecko_id": "dash",
        "blockchair_chain": "dash",
        "block_reward": 0.9958,  # доля майнера (~45%) от общего сабсидия
        "hr_unit": "GH/s",
        "hr_multiplier": 1e9,
        "hr_example": "1770",
    },
    "ZEC": {
        "title": "Zcash (ZEC, Equihash)",
        "algorithm": "Equihash",
        "coingecko_id": "zcash",
        "blockchair_chain": "zcash",
        "block_reward": 1.25,  # доля майнера (80%) от сабсидия 1.5625 ZEC
        "hr_unit": "kSol/s",
        "hr_multiplier": 1e3,
        "hr_example": "840",
    },
}

# Модели асиков: MODELS[алгоритм][производитель][модель] = список версий.
# Каждая версия — (подпись, хешрейт, потребление в Вт). Хешрейт в тех же
# единицах, что и hr_unit соответствующей монеты.
MODELS = {
    "SHA-256": {
        "Antminer": {
            "Antminer S19": [("95 TH/s / 3250 Вт", 95, 3250)],
            "Antminer S19 Pro": [("110 TH/s / 3250 Вт", 110, 3250)],
            "Antminer S19 XP": [
                ("Air — 141 TH/s / 3010 Вт", 141, 3010),
                ("Hyd — 255 TH/s / 5304 Вт", 255, 5304),
            ],
            "Antminer S21": [
                ("Air — 200 TH/s / 3550 Вт", 200, 3550),
                ("Immersion — 301 TH/s / 5570 Вт", 301, 5570),
                ("Hydro — 335 TH/s / 5360 Вт", 335, 5360),
                ("Hydro 3U — 860 TH/s / 11180 Вт", 860, 11180),
            ],
            "Antminer S21 XP": [
                ("Air — 270 TH/s / 3645 Вт", 270, 3645),
                ("Hyd — 473 TH/s / 5676 Вт", 473, 5676),
            ],
        },
        "Whatsminer": {
            "Whatsminer M30S++": [
                ("106 TH/s / 3400 Вт", 106, 3400),
                ("110 TH/s / 3410 Вт", 110, 3410),
                ("112 TH/s / 3472 Вт", 112, 3472),
            ],
            "Whatsminer M50": [("122 TH/s / 3306 Вт", 122, 3306)],
            "Whatsminer M50S": [
                ("126 TH/s / 3276 Вт", 126, 3276),
                ("140 TH/s / 3500 Вт", 140, 3500),
            ],
            "Whatsminer M50S+": [("148 TH/s / 3404 Вт", 148, 3404)],
            "Whatsminer M50S++": [
                ("140 TH/s / 3080 Вт", 140, 3080),
                ("142 TH/s / 3124 Вт", 142, 3124),
            ],
            "Whatsminer M60": [
                ("160 TH/s / 3184 Вт", 160, 3184),
                ("180 TH/s / 3350 Вт", 180, 3350),
            ],
            "Whatsminer M60S": [
                ("186 TH/s / 3441 Вт", 186, 3441),
                ("188 TH/s / 3400 Вт", 188, 3400),
            ],
            "Whatsminer M60S+": [("200 TH/s / 3600 Вт", 200, 3600)],
            "Whatsminer M60S++": [("218 TH/s / 3379 Вт", 218, 3379)],
            "Whatsminer M63": [("372 TH/s / 7403 Вт", 372, 7403)],
            "Whatsminer M63S": [("408 TH/s / 7344 Вт", 408, 7344)],
            "Whatsminer M63S+": [("402 TH/s / 6834 Вт", 402, 6834)],
            "Whatsminer M63S++": [("464 TH/s / 7192 Вт", 464, 7192)],
            "Whatsminer M66": [("276 TH/s / 5492 Вт", 276, 5492)],
            "Whatsminer M66S": [("290 TH/s / 5365 Вт", 290, 5365)],
            "Whatsminer M66S+": [("342 TH/s / 5814 Вт", 342, 5814)],
            "Whatsminer M66S++": [("348 TH/s / 5394 Вт", 348, 5394)],
        },
    },
    "Scrypt": {
        "Antminer": {
            "Antminer L7": [
                ("8.8 GH/s / 3168 Вт", 8800, 3168),
                ("9.3 GH/s / 3425 Вт", 9300, 3425),
                ("9.5 GH/s / 3425 Вт", 9500, 3425),
            ],
        },
    },
    "X11": {
        "Antminer": {
            "Antminer D9": [("1770 GH/s / 2839 Вт", 1770, 2839)],
        },
    },
    "Equihash": {
        "Antminer": {
            "Antminer Z15": [
                ("Z15e — 200 kSol/s / 1500 Вт", 200, 1500),
                ("Z15j — 320 kSol/s / 1510 Вт", 320, 1510),
                ("Z15 — 420 kSol/s / 1510 Вт", 420, 1510),
                ("Z15 Pro — 840 kSol/s / 2780 Вт", 840, 2780),
            ],
        },
    },
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Состояния диалога
(
    CHECK_SUBSCRIPTION,
    CHOOSING_COIN,
    CHOOSING_MANUFACTURER,
    CHOOSING_FAMILY,
    CHOOSING_VARIANT,
    HASHRATE,
    POWER,
    PRICE,
    TARIFF,
) = range(9)


async def is_subscribed(bot, user_id: int) -> bool:
    """Проверяет подписку пользователя на REQUIRED_CHANNEL.
    Бот должен быть админом канала, иначе Telegram вернёт ошибку."""
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.error(
            f"Не удалось проверить подписку на {REQUIRED_CHANNEL}: {e}. "
            f"Проверьте, что бот добавлен в канал администратором."
        )
        return False


def subscription_prompt_keyboard() -> InlineKeyboardMarkup:
    channel_url = f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Открыть канал", url=channel_url)],
            [InlineKeyboardButton("✅ Я подписался", callback_data="check_sub")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id

    if not await is_subscribed(context.bot, user_id):
        await update.message.reply_text(
            f"Чтобы пользоваться ботом, подпишитесь на канал {REQUIRED_CHANNEL}, "
            f"а затем нажмите кнопку ниже.",
            reply_markup=subscription_prompt_keyboard(),
        )
        return CHECK_SUBSCRIPTION

    return await show_coin_menu(update.message, context)


async def check_subscription_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    user_id = query.from_user.id

    if await is_subscribed(context.bot, user_id):
        await query.answer("Подписка подтверждена!")
        await query.message.delete()
        return await show_coin_menu(query.message, context)

    await query.answer(
        "Пока не вижу подписку. Убедитесь, что вы подписались, и попробуйте снова.",
        show_alert=True,
    )
    return CHECK_SUBSCRIPTION


async def show_coin_menu(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отправляет картинку-заставку и меню выбора монеты."""
    keyboard = [
        [InlineKeyboardButton(cfg["title"], callback_data=code)]
        for code, cfg in COINS.items()
    ]
    caption = (
        "Привет! Посчитаю доходность и окупаемость ASIC-майнера.\n\n"
        "Какую монету считаем?"
    )

    try:
        with open(WELCOME_IMAGE_PATH, "rb") as photo:
            await message.reply_photo(
                photo=photo,
                caption=caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
    except FileNotFoundError:
        # Картинки нет рядом с bot.py — просто отправляем текст, чтобы бот
        # не падал.
        logger.warning(
            f"Файл {WELCOME_IMAGE_PATH} не найден — отправляю без картинки"
        )
        await message.reply_text(
            caption, reply_markup=InlineKeyboardMarkup(keyboard)
        )

    return CHOOSING_COIN


async def send_menu_photo(message, image_path: str, caption: str, keyboard):
    """Отправляет меню фирменной картинкой с подписью; если файл не найден
    рядом с bot.py — отправляет обычным текстом, чтобы бот не падал."""
    try:
        with open(image_path, "rb") as photo:
            await message.reply_photo(
                photo=photo, caption=caption, reply_markup=keyboard
            )
    except FileNotFoundError:
        logger.warning(f"Файл {image_path} не найден — отправляю без картинки")
        await message.reply_text(caption, reply_markup=keyboard)


async def safe_edit_caption(query, text: str):
    """Редактирует подпись/текст предыдущего сообщения независимо от того,
    было оно картинкой или обычным текстом — чтобы не падать на попытке
    отредактировать не тот тип сообщения."""
    try:
        await query.edit_message_caption(caption=text)
    except Exception:
        try:
            await query.edit_message_text(text)
        except Exception:
            pass


async def choose_coin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    coin_code = query.data
    coin = COINS[coin_code]
    context.user_data["coin_code"] = coin_code

    await safe_edit_caption(query, f"Монета: {coin['title']}")

    manufacturers = list(MODELS[coin["algorithm"]].keys())
    keyboard = [
        [InlineKeyboardButton(name, callback_data=name)] for name in manufacturers
    ]
    keyboard.append(
        [InlineKeyboardButton("✍️ Ввести вручную", callback_data="manual")]
    )
    await send_menu_photo(
        query.message,
        BANNER_MANUFACTURER,
        "Выберите производителя:",
        InlineKeyboardMarkup(keyboard),
    )
    return CHOOSING_MANUFACTURER


async def choose_manufacturer(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    coin = COINS[context.user_data["coin_code"]]

    if query.data == "manual":
        await safe_edit_caption(query, "Ввод вручную")
        await query.message.reply_text(
            f"Введите хешрейт устройства в {coin['hr_unit']} "
            f"(например: {coin['hr_example']})",
            reply_markup=ReplyKeyboardRemove(),
        )
        return HASHRATE

    manufacturer = query.data
    context.user_data["manufacturer"] = manufacturer

    families = list(MODELS[coin["algorithm"]][manufacturer].keys())
    context.user_data["family_list"] = families

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"f{idx}")]
        for idx, name in enumerate(families)
    ]
    await safe_edit_caption(query, f"Производитель: {manufacturer}")
    await send_menu_photo(
        query.message,
        BANNER_MODEL,
        "Выберите модель:",
        InlineKeyboardMarkup(keyboard),
    )
    return CHOOSING_FAMILY


async def choose_family(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    coin = COINS[context.user_data["coin_code"]]
    manufacturer = context.user_data["manufacturer"]

    idx = int(query.data[1:])
    family_name = context.user_data["family_list"][idx]
    variants = MODELS[coin["algorithm"]][manufacturer][family_name]
    context.user_data["family_name"] = family_name
    context.user_data["variants"] = variants

    await safe_edit_caption(query, f"Модель: {family_name}")

    if len(variants) == 1:
        # У модели только одна версия — подставляем её и идём дальше.
        label, hashrate, power_w = variants[0]
        context.user_data["hashrate"] = hashrate
        context.user_data["power_w"] = power_w
        await query.message.reply_text(f"Версия: {label}")
        return await proceed_after_model_choice(
            query.message, context, family_name
        )

    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"v{i}")]
        for i, (label, _, _) in enumerate(variants)
    ]
    await send_menu_photo(
        query.message,
        BANNER_VARIANT,
        "У этой модели несколько версий по хешрейту/охлаждению — выберите:",
        InlineKeyboardMarkup(keyboard),
    )
    return CHOOSING_VARIANT


async def choose_variant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    idx = int(query.data[1:])
    label, hashrate, power_w = context.user_data["variants"][idx]
    context.user_data["hashrate"] = hashrate
    context.user_data["power_w"] = power_w

    await safe_edit_caption(query, f"Версия: {label}")
    return await proceed_after_model_choice(
        query.message, context, context.user_data["family_name"]
    )


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

    try:
        await update.message.reply_text(result, parse_mode="Markdown")
    except Exception as e:
        # Если Telegram не смог разобрать Markdown-разметку — отправляем
        # обычным текстом, чтобы пользователь в любом случае получил ответ.
        logger.warning(f"Не удалось отправить с Markdown, отправляю без него: {e}")
        await update.message.reply_text(result)

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
    покрывает Bitcoin, Dogecoin, Dash, Zcash и другие цепочки)."""
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
    # Формула одинаково справедлива для SHA-256/Scrypt/X11 (все —
    # bitcoin-подобные форки с тем же определением difficulty) и для
    # Equihash (Zcash использует тот же класс сложности из ядра Bitcoin
    # Core, просто хешрейт называется "solution rate").
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
        f"будущем. Для нового расчёта — /start_\n\n"
        f"—\n"
        f"Приобрести это оборудование можно у нас, в Crypto Point:\n"
        f"Игорь: [написать в Telegram](https://t.me/Igor_Crypto_Point) · "
        f"+7 938 338-90-53\n"
        f"Захар: [написать в Telegram](https://t.me/zaHarik2008) · "
        f"+7 908 778-03-04"
    )


def get_price_from_sheet(model_name: str) -> Optional[float]:
    """Ищет цену модели в опубликованном CSV прайс-листе.
    Возвращает None, если ссылка не настроена, модель не найдена или
    запрос не прошёл (тогда бот просто попросит цену вручную)."""
    if not PRICE_SHEET_CSV_URL:
        return None

    try:
        r = requests.get(PRICE_SHEET_CSV_URL, timeout=10)
        r.raise_for_status()
        reader = csv.reader(io.StringIO(r.text))
        for row in reader:
            if len(row) < 2:
                continue
            name, price_str = row[0].strip(), row[1].strip()
            if name.lower() == model_name.lower():
                try:
                    return float(price_str.replace(" ", "").replace(",", "."))
                except ValueError:
                    return None
    except Exception as e:
        logger.warning(f"Не удалось прочитать прайс-лист: {e}")

    return None


async def proceed_after_model_choice(
    message, context: ContextTypes.DEFAULT_TYPE, family_name: str
) -> int:
    """После того как хешрейт/потребление известны — пробует найти цену
    в прайс-листе; если не нашёл, просит ввести цену вручную."""
    price = get_price_from_sheet(family_name)

    if price is not None:
        context.user_data["price_rub"] = price
        await message.reply_text(
            f"Цена по вашему прайсу: {price:,.0f} ₽\n\n"
            f"Тариф на электричество в руб/кВт·ч (например: 4.5)"
        )
        return TARIFF

    await message.reply_text("Введите цену устройства в рублях (например: 250000)")
    return PRICE


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено. Наберите /start, чтобы начать заново.")
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит любые необработанные ошибки, чтобы бот не молчал при сбое,
    а пользователь получил хоть какой-то ответ вместо тишины."""
    logger.error(f"Необработанная ошибка: {context.error}")
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Что-то пошло не так. Попробуйте /start, чтобы начать заново."
            )
        except Exception:
            pass


def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHECK_SUBSCRIPTION: [
                CallbackQueryHandler(check_subscription_callback)
            ],
            CHOOSING_COIN: [CallbackQueryHandler(choose_coin)],
            CHOOSING_MANUFACTURER: [CallbackQueryHandler(choose_manufacturer)],
            CHOOSING_FAMILY: [CallbackQueryHandler(choose_family)],
            CHOOSING_VARIANT: [CallbackQueryHandler(choose_variant)],
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
    application.add_error_handler(error_handler)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
