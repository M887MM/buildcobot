import os
import json
import logging
import re
import asyncio
from collections import defaultdict
from typing import List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit, quote

from dotenv import load_dotenv
from aiogram import Bot, types
from openai import OpenAI

try:
    from anthropic import Anthropic
except ImportError:  # библиотека может быть не установлена
    Anthropic = None

from db import Session, Flats as DBFlats

# === Инициализация ===
load_dotenv()
client = OpenAI(api_key=os.getenv("API_KEY"))
logger = logging.getLogger(__name__)

DEFAULT_GPT_MODEL = "gpt-5-chat-latest"
CLAUDE_MODEL = "claude-3-5-haiku-latest"

_claude_client = None
_claude_key = os.getenv("ANTHROPIC_API_KEY")
if _claude_key and Anthropic:
    try:
        _claude_client = Anthropic(api_key=_claude_key)
    except Exception as claude_init_error:
        logger.warning("Не удалось инициализировать клиент Claude: %s", claude_init_error)
elif _claude_key and not Anthropic:
    logger.warning("Библиотека anthropic не установлена, fallback Claude недоступен")

# === Кэши ===
user_conversations = defaultdict(list)
last_filters_cache = {}
shown_flats_cache = defaultdict(set)
SUPPORTED_LANGS = {"ru", "uz", "en", "kk"}


def _normalize_message_text(content) -> str:
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content) if content is not None else ""


def _is_empty_response(text: str) -> bool:
    if not text or not text.strip():
        return True
    normalized = text.strip()
    return normalized in {"[]", "[ ]"}


def _call_openai(messages: List[dict], max_tokens: Optional[int], model: str) -> str:
    params = {
        "model": model,
        "messages": messages,
    }
    if max_tokens is not None:
        params["max_completion_tokens"] = max_tokens

    response = client.chat.completions.create(**params)
    choice = response.choices[0]
    content = _normalize_message_text(choice.message.content)
    return content.strip()


def _call_claude(messages: List[dict], max_tokens: Optional[int]) -> str:
    if not _claude_client:
        raise RuntimeError("Claude client is not configured")

    system_parts: List[str] = []
    claude_messages = []

    for message in messages:
        role = message.get("role")
        text = _normalize_message_text(message.get("content"))
        if not text:
            continue
        if role == "system":
            system_parts.append(text)
        elif role in {"user", "assistant"}:
            claude_messages.append({
                "role": role,
                "content": text,
            })

    system_prompt = "\n".join(system_parts) if system_parts else None
    output_tokens = max_tokens if max_tokens and max_tokens > 0 else 512

    response = _claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_output_tokens=output_tokens,
        system=system_prompt,
        messages=claude_messages,
    )

    if not response.content:
        return ""

    parts = []
    for item in response.content:
        if isinstance(item, dict):
            text = item.get("text")
        else:
            text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "".join(parts).strip()


def call_chat_with_fallback(
    messages: List[dict],
    *,
    max_tokens: Optional[int] = None,
    model: str = DEFAULT_GPT_MODEL,
) -> Tuple[str, str]:
    try:
        gpt_content = _call_openai(messages, max_tokens, model)
        if _is_empty_response(gpt_content):
            raise ValueError("GPT вернул пустой ответ")
        return gpt_content, "gpt"
    except Exception as gpt_error:
        logger.warning("GPT недоступен или вернул пустой ответ: %s", gpt_error)

        try:
            claude_content = _call_claude(messages, max_tokens)
            if _is_empty_response(claude_content):
                raise ValueError("Claude вернул пустой ответ")
            logger.info("Использован fallback Claude Haiku 3.5")
            return claude_content, "claude"
        except Exception as claude_error:
            logger.error("Не удалось получить ответ от Claude: %s", claude_error)
            raise RuntimeError("Claude fallback failed") from claude_error


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
        content, provider = call_chat_with_fallback(
            [
                {"role": "system", "content": "Respond ONLY with one code: ru, en, uz, kk."},
                {"role": "user", "content": text},
            ],
            max_tokens=5,
        )
        lang = content.strip().lower()
        if lang in SUPPORTED_LANGS:
            return lang
        logger.debug("Неизвестный язык '%s' от %s, используем ru по умолчанию", lang, provider)
    except Exception as error:
        logger.warning("Ошибка определения языка: %s", error)
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

        raw, provider = call_chat_with_fallback(messages, max_tokens=250)
        print(f"\n[{provider.upper()} RAW FILTERS]:", raw, "\n")

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

        logger.info("✅ %s parsed filters: %s", provider.upper(), data)
        return data

    except Exception as e:
        logger.warning("⚠️ Ошибка парсинга фильтров AI: %s", e)
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
                translated, provider = call_chat_with_fallback(
                    [
                        {
                            "role": "system",
                            "content": f"Translate to {lang}, but keep numbers and building names unchanged.",
                        },
                        {"role": "user", "content": text_base},
                    ],
                )
                text_base = translated.strip()
                logger.info("Перевод выполнен с использованием %s", provider.upper())
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
