import os
import json
import logging
import re
import asyncio
from collections import defaultdict
from urllib.parse import urlsplit, urlunsplit, quote
from dotenv import load_dotenv
from aiogram import Bot, types
from openai import OpenAI
from db import Session, Flats as DBFlats

# === Инициализация ===
load_dotenv()
client = OpenAI(api_key=os.getenv("API_KEY"))
logger = logging.getLogger(__name__)

# === Кэши ===
user_conversations = defaultdict(list)
last_filters_cache = {}
shown_flats_cache = defaultdict(set)
SUPPORTED_LANGS = {"ru", "uz", "en", "kk"}


# === РЕЗЕРВНЫЙ ПАРСЕР (fallback) ===
def fallback_parse_filters(text: str) -> dict:
    filters = {}

    low = text.lower()

    # номер квартиры: "№123", "номер 123"
    if match := re.search(r'(?:№|номер)\s*#?\s*(\d+)', text, re.IGNORECASE):
        try:
            filters["number"] = int(match.group(1))
        except Exception:
            pass

    # 1–5 комнат / "2 комнат" / uz: "2 honali", "2 xonali", "2 xona"
    # проверяем несколько вариантов слов для комнат
    if match := re.search(r'(\d+)\s*[- ]?\s*(?:комнат|комн|honali|xonali|xona|хонали)', text, re.IGNORECASE):
        try:
            filters["rooms"] = int(match.group(1))
        except Exception:
            pass

    # этаж: диапазон "этаж 1-5" или "этаж от 1 до 5" или "4-5 qavat"
    if match := re.search(r'(?:этаж|qavat|қават)(?:а|ей)?\s*(?:от\s*)?(\d+)\s*(?:до|-)\s*(\d+)', low, re.IGNORECASE):
        try:
            start = int(match.group(1))
            end = int(match.group(2))
            if start <= end:
                filters["stage_min"] = start
                filters["stage_max"] = end
        except Exception:
            pass
    else:
        # одиночный этаж "3 этаж", "на 3 этаже", "5 qavat", "5-qavat"
        if match := re.search(r'(\d+)\s*(?:этаж|этаже|qavat|қават)', text, re.IGNORECASE):
            try:
                filters["stage"] = int(match.group(1))
            except Exception:
                pass

    # цена до / максимум (поддержка тыс)
    if match := re.search(r'(\d+[.,]?\d*)\s*(тыс|тысяч)?\s*(?:\$|доллар|сом|сум|usd)?', text, re.IGNORECASE):
        try:
            price = float(match.group(1).replace(',', '.'))
            if match.group(2):
                # если указано тыс(яч), умножаем
                price *= 1000
            filters["price_max"] = int(price)
        except Exception:
            pass

    # тип: магазин / студия / квартира (по умолчанию Квартира)
    if 'магазин' in low:
        filters["type"] = "Магазин"
    elif 'студ' in low or re.search(r'\b1\s*комн', low):
        filters["type"] = "Студия"
    elif 'uy' in low or 'дом' in low or 'uy' in text.lower():
        # если упоминается 'uy' (узбекское слово для дома) — всё равно считаем квартирой в терминах БД
        filters["type"] = "Квартира"
    else:
        # вернуть поведение по умолчанию: если тип не указан явно — искать "Квартира"
        filters.setdefault("type", "Квартира")

    return filters


# === УТИЛИТА ДЛЯ URL ===
def normalize_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        path = quote(parts.path, safe="/%") if parts.path else ""
        query = quote(parts.query, safe="=&?") if parts.query else ""
        return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))
    except Exception:
        return url


# === "ПЕЧАТАЕТ..." ===
async def show_typing(bot: Bot, chat_id: int, duration: int = 5):
    """
    Показывает индикатор 'typing' в чате.
    Использует types.ChatActions.TYPING для корректной работы с aiogram.
    Защищено от исключений, чтобы не ломать основной поток.
    """
    try:
        end_time = asyncio.get_event_loop().time() + duration
        while asyncio.get_event_loop().time() < end_time:
            await bot.send_chat_action(chat_id, types.ChatActions.TYPING)
            await asyncio.sleep(4)
    except Exception as e:
        logger.debug(f"show_typing error: {e}")
        pass


# === ОПРЕДЕЛЕНИЕ ЯЗЫКА ===
async def detect_language(text: str) -> str:
    try:
        resp = client.chat.completions.create(
            model="gpt-5-chat-latest",
            messages=[
                {"role": "system", "content": "Respond ONLY with one code: ru, en, uz, kk."},
                {"role": "user", "content": text},
            ],
            max_completion_tokens=5,
        )
        lang = resp.choices[0].message.content.strip().lower()
        return lang if lang in SUPPORTED_LANGS else "ru"
    except Exception:
        return "ru"


# === GPT-ПАРСЕР ФИЛЬТРОВ ===
async def extract_filters_with_gpt(text: str) -> dict:
    """
    GPT парсит фильтры из пользовательского запроса.
    Гарантирует возврат корректного JSON.
    Поддерживаем дополнительные поля:
      - number (int) — номер квартиры
      - stage_min, stage_max (int) — диапазон этажей
    """
    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "Ты парсер фильтров недвижимости. "
                    "Верни только JSON без текста и комментариев. "
                    "Если данных нет — верни '{}'. "
                    "Поля: number (int) если клиент сказал, type (Квартира|Студия|Магазин), rooms (int), "
                    "stage (int), stage_min (int), stage_max (int), price_max (int), price_order (min|max). "
                    "Также понимай узбекские слова 'qavat', 'xonali', 'honali' как этажи и комнаты."
                ),
            },
            {"role": "user", "content": text},
        ]

        resp = client.chat.completions.create(
            model="gpt-5-chat-latest",
            messages=messages,
            max_completion_tokens=250,
        )

        raw = resp.choices[0].message.content.strip()
        print("\n[GPT RAW FILTERS]:", raw, "\n")

        # убираем markdown-мусор ```json ``` и т.п.
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()

        data = json.loads(cleaned) if cleaned else {}
        if not isinstance(data, dict):
            raise ValueError("GPT ответил не JSON")

        # нормализуем числовые строки в int (если будут)
        for k in ["number", "rooms", "stage", "stage_min", "stage_max", "price_max"]:
            if k in data and data[k] is not None:
                try:
                    data[k] = int(data[k])
                except Exception:
                    data.pop(k, None)

        # в случае, если указан stage (единичный) и также указан диапазон — приоритет диапазону
        if "stage_min" in data and "stage_max" in data and "stage" in data:
            data.pop("stage", None)

        logger.info(f"✅ GPT parsed filters: {data}")
        return data

    except Exception as e:
        logger.warning(f"⚠️ Ошибка парсинга фильтров GPT: {e}")
        filters = fallback_parse_filters(text)
        logger.info(f"🔄 Используем fallback: {filters}")
        return filters


# === ГЛАВНАЯ ФУНКЦИЯ ===
async def ask_openai_sync(user_id: int, text: str, bot: Bot = None, chat_id: int = None):
    print(f"\n=== USER MESSAGE ===\n{text}\n====================\n")
    text = text.strip()
    if not text:
        return {"text": "❗ Пустой запрос"}

    if bot and chat_id:
        asyncio.create_task(show_typing(bot, chat_id, duration=5))

    # язык
    lang = await detect_language(text)
    user_conversations[user_id].append(text)

    # фильтры
    filters = await extract_filters_with_gpt(text)
    if not filters:
        filters = fallback_parse_filters(text)

    if not filters:
        msg = {
            "ru": "Пожалуйста, уточните хотя бы одно пожелание 💬",
            "uz": "Iltimos, kamida bitta talabni kiriting 💬",
            "en": "Please specify at least one preference 💬",
            "kk": "Кем дегенде бір қалауыңызды көрсетіңіз 💬",
        }[lang]
        return {"text": msg}

    last_filters_cache[user_id] = filters
    shown_flats_cache[user_id].clear()

    if bot and chat_id:
        asyncio.create_task(show_typing(bot, chat_id, duration=5))

    # === Поиск в БД ===
    session = Session()
    query = session.query(DBFlats)

    # По номеру квартиры — если указан, ищем строго по номеру
    if filters.get("number") is not None:
        try:
            query = query.filter(DBFlats.number == filters["number"])
        except Exception:
            try:
                query = query.filter(DBFlats.number == str(filters["number"]))
            except Exception:
                pass

    else:
        # тип
        if filters.get("type"):
            query = query.filter(DBFlats.type == filters["type"])
        if filters.get("rooms"):
            query = query.filter(DBFlats.rooms == filters["rooms"])

        # этаж: диапазон или конкретный
        if filters.get("stage_min") is not None and filters.get("stage_max") is not None:
            try:
                query = query.filter(DBFlats.stage >= filters["stage_min"])
                query = query.filter(DBFlats.stage <= filters["stage_max"])
            except Exception:
                pass
        elif filters.get("stage") is not None:
            query = query.filter(DBFlats.stage == filters["stage"])

        # цена
        if filters.get("price_max"):
            query = query.filter(DBFlats.price <= filters["price_max"])
        if filters.get("price_order") == "min":
            query = query.order_by(DBFlats.price.asc())
        elif filters.get("price_order") == "max":
            query = query.order_by(DBFlats.price.desc())

    flats = query.filter(DBFlats.status == "Свободно").all()
    session.close()

    if not flats:
        msg = {
            "ru": "К сожалению, объекты с такими параметрами не найдены. 🏙",
            "uz": "Afsuski, bunday parametrli obyektlar topilmadi. 🏙",
            "en": "Unfortunately, no properties match these parameters. 🏙",
            "kk": "Өкінішке орай, мұндай параметрлермен нысандар табылмады. 🏙",
        }[lang]
        return {"text": msg}

    # === Фильтруем уже показанные ===
    seen = shown_flats_cache[user_id]
    new_flats = [f for f in flats if f.number not in seen][:4]
    if not new_flats:
        seen.clear()
        new_flats = flats[:4]
    for f in new_flats:
        seen.add(f.number)

    # === Формируем вывод ===
    results = []
    for f in new_flats:
        text_base = (
            f"🏠 {f.type} №{f.number}\n"
            f"• Комнат: {f.rooms}\n"
            f"• Этаж: {f.stage}\n"
            f"• Площадь: {f.sq_m} м²\n"
            f"• Цена: {f.price} $\n"
            f"• Подъезд: {f.lobby}\n\n"
            f"{f.description}\n\n"
            "С вами свяжется менеджер для уточнения деталей. 🏙"
        )

        # перевод, если язык не русский
        if lang != "ru":
            try:
                translation = client.chat.completions.create(
                    model="gpt-5-chat-latest",
                    messages=[
                        {
                            "role": "system",
                            "content": f"Translate to {lang}, but keep numbers and building names unchanged.",
                        },
                        {"role": "user", "content": text_base},
                    ],
                )
                text_base = translation.choices[0].message.content.strip()
            except Exception as e:
                logger.warning(f"Ошибка перевода: {e}")

        photo_val = normalize_url(f.plan.strip()) if getattr(f, "plan", None) else None
        results.append({"text": text_base, "photo": photo_val})

    return {"flats": results}


# === Очистка истории ===
def clear_user(user_id: int):
    user_conversations[user_id].clear()
    last_filters_cache.pop(user_id, None)
    shown_flats_cache.pop(user_id, None)
