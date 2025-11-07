import asyncio
import logging
import time
import os
import re
from collections import defaultdict
from typing import Iterable, List, Optional, Tuple, Protocol
from types import SimpleNamespace
from difflib import SequenceMatcher

from aiogram import Bot, types
from dotenv import load_dotenv
from openai import OpenAI

try:
    from anthropic import Anthropic
except ImportError:  # библиотека может быть не установлена
    Anthropic = None

from db import Session, Product as DBProduct
from sqlalchemy.orm import selectinload
from text_utils import normalize_text
from knowledge_base import get_material_reference

# === Инициализация ===
load_dotenv()
client = OpenAI(api_key=os.getenv("API_KEY"))
logger = logging.getLogger(__name__)

class ProductLike(Protocol):
    id: Optional[int]
    name: Optional[str]
    category: Optional[str]
    description: Optional[str]
    tags: Optional[str]
    picture: List[str]
    cover: Optional[str]
    price: Optional[float]
    old_price: Optional[float]

DEFAULT_GPT_MODEL = "gpt-5-chat-latest"
CLAUDE_MODEL = "claude-3-5-haiku-latest"
PLACEHOLDER_PHOTO = "https://via.placeholder.com/600x400.png?text=No+Image"
CATEGORY_MATCH_BOOST = 2
LLM_PROVIDER_ORDER_ENV = os.getenv("LLM_PROVIDER_ORDER")
GENERIC_PRODUCT_TOKENS = {
    "крем",
    "крема",
    "кремы",
    "creme",
    "cream",
    "косметика",
    "средство",
    "средства",
    "уход",
    "уходовый",
    "beauty",
    "product",
    "товар",
    "товары",
    "care",
}

ANTHROPIC_ENABLED = os.getenv("ANTHROPIC_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
_claude_client = None
_claude_key = os.getenv("ANTHROPIC_API_KEY") if ANTHROPIC_ENABLED else None
if _claude_key and Anthropic:
    try:
        _claude_client = Anthropic(api_key=_claude_key)
    except Exception as claude_init_error:
        hint = ""
        if isinstance(claude_init_error, TypeError) and "proxies" in str(claude_init_error).lower():
            hint = (
                " Проверьте совместимость версий: anthropic<=0.39.0 ожидает httpx<0.28. "
                "Обновите anthropic или понизьте httpx."
            )
        logger.warning("Не удалось инициализировать клиент Claude: %s.%s", claude_init_error, hint)
elif _claude_key and not Anthropic:
    logger.warning("Библиотека anthropic не установлена, fallback Claude недоступен")


def _resolve_provider_alias(name: str) -> Optional[str]:
    normalized = (name or "").strip().lower()
    if not normalized:
        return None
    if normalized in {"openai", "gpt", "chatgpt"}:
        return "openai"
    if normalized in {"anthropic", "claude", "claude-haiku"}:
        return "anthropic"
    return None


def _parse_provider_order(env_value: Optional[str]) -> List[str]:
    order: List[str] = []
    raw_tokens = (env_value or "").split(",")
    for token in raw_tokens:
        alias = _resolve_provider_alias(token)
        if alias and alias not in order:
            order.append(alias)
    if not order:
        order = ["openai", "anthropic"]
    return order


if not _claude_key or _claude_client is None:
    LLM_PROVIDER_SEQUENCE = [
        provider for provider in _parse_provider_order(LLM_PROVIDER_ORDER_ENV)
        if provider != "anthropic"
    ]
else:
    LLM_PROVIDER_SEQUENCE = _parse_provider_order(LLM_PROVIDER_ORDER_ENV)

PROVIDER_LABELS = {"openai": "gpt", "anthropic": "claude"}

# === Кэши ===
user_conversations = defaultdict(list)
SUPPORTED_LANGS = {"ru", "uz", "en", "kk"}
INTENT_KEYWORDS = {
    "ru": {
        "товар",
        "товары",
        "косметика",
        "косметику",
        "средство",
        "средства",
        "уход",
        "уходовый",
        "макияж",
        "makeup",
        "аромат",
        "парфюм",
        "подарок",
        "каталог",
        "каталогу",
        "прайс",
        "цена",
        "цены",
        "купить",
        "подобрать",
        "подобери",
        "посоветуй",
        "подбор",
        "покажи",
        "список",
        "beauty",
    },
    "uz": {
        "mahsulot",
        "mahsulotlar",
        "narx",
        "narxi",
        "katalog",
        "sotib",
        "olish",
        "tanlash",
        "korsat",
        "kosmetika",
        "parvarish",
        "makiyaj",
        "parfyum",
        "sovga",
    },
    "en": {
        "product",
        "products",
        "catalog",
        "price",
        "prices",
        "buy",
        "purchase",
        "show",
        "recommend",
        "list",
        "cosmetic",
        "cosmetics",
        "beauty",
        "skincare",
        "makeup",
        "fragrance",
        "gift",
    },
    "kk": {
        "тауар",
        "тауарлар",
        "каталог",
        "баға",
        "бағалар",
        "сатып",
        "ұсыныңыз",
        "таңдау",
        "косметика",
        "beauty",
        "макияж",
        "тері",
        "сыйлық",
    },
    "default": {"store", "beauty", "cosmetic", "cosmetics"},
}
NO_MATCH_RESPONSES = {
    "ru": (
        "🚫 По запросу «{query}» подходящих средств не нашёл.\n"
        "Расскажите подробнее:\n"
        "🔸 уход за кожей (кремы, сыворотки, SPF)\n"
        "🔸 макияж (тональные основы, тени, тушь)\n"
        "🔸 ароматы и подарочные наборы\n"
        "🔸 уход за волосами и аксессуары\n"
        "Уточните тип кожи, повод или бюджет — подберу варианты и доставку."
    ),
    "uz": (
        "🚫 \"{query}\" bo‘yicha mos kosmetika topilmadi.\n"
        "Batafsil yozing:\n"
        "🔸 teri parvarishi (krem, sarum, SPF)\n"
        "🔸 makiyaj (tonal krem, ten, tush)\n"
        "🔸 atirlar va sovg‘a to‘plamlari\n"
        "🔸 soch parvarishi va aksessuarlar\n"
        "Teri turi, byudjet yoki voqeani ko‘rsating — mos variantlarni tavsiya qilaman."
    ),
    "en": (
        "🚫 I couldn't find beauty products for “{query}”.\n"
        "Let me know more:\n"
        "🔸 skincare (creams, serums, SPF)\n"
        "🔸 makeup (foundations, palettes, mascara)\n"
        "🔸 fragrances and gift sets\n"
        "🔸 hair care and accessories\n"
        "Share skin type, occasion, or budget and I’ll suggest options with delivery."
    ),
    "kk": (
        "🚫 \"{query}\" сұрауына сәйкес косметика табылмады.\n"
        "Толығырақ жазыңыз:\n"
        "🔸 тері күтімі (крем, сарысу, SPF)\n"
        "🔸 макияж (тон, тень, тушь)\n"
        "🔸 хош иістер мен сыйлық жиынтықтар\n"
        "🔸 шаш күтімі және аксессуарлар\n"
        "Тері түрі, жағдай немесе бюджет жайлы айтыңыз — лайықты нұсқалар ұсынамын."
    ),
}
LOW_INFO_RESPONSES = {
    "ru": (
        "🙂 Похоже, сообщение вышло бессвязным. Если просто шутите — улыбаюсь вместе с вами. "
        "Если нужна помощь, напишите, какое средство ищете, тип кожи или желаемый эффект — подберу уход или макияж."
    ),
    "uz": (
        "🙂 Xabaringiz biroz chalkash chiqdi. Agar hazillashgan bo‘lsangiz — mayli. "
        "Yordam kerak bo‘lsa, qaysi mahsulot, teri turi yoki kerakli effekt haqida yozing, mos parvarish yoki makiyajni tavsiya qilaman."
    ),
    "en": (
        "🙂 That message looked a bit incoherent. If you’re joking, all good! "
        "If you do need help, tell me what product, skin type, or result you’re after and I’ll suggest skincare or makeup."
    ),
    "kk": (
        "🙂 Хабыңыз сәл түсініксіз болды. Егер жай әзіл болса — жарайды. "
        "Көмек қажет болса, қандай өнім, тері түрі немесе эффект іздейтініңізді жазыңыз, лайықты күтім немесе макияж ұсынамын."
    ),
}

TOKEN_SEARCH_STOPWORDS = {
    "ru": {
        "какие",
        "какой",
        "какая",
        "каких",
        "виды",
        "вид",
        "тип",
        "типы",
        "типов",
        "есть",
        "наличии",
        "наличие",
        "имеются",
        "нужны",
        "нужно",
        "нужен",
        "подскажите",
        "подскажи",
        "расскажи",
        "покажи",
        "покажите",
        "можно",
        "можешь",
        "подскажешь",
        "подскажите",
        "интересуют",
        "интересует",
        "что",
        "тебя",
        "тебе",
        "для",
    },
    "en": {
        "what",
        "which",
        "kind",
        "kinds",
        "types",
        "type",
        "do",
        "you",
        "have",
        "available",
        "in",
        "stock",
        "are",
        "there",
        "any",
        "please",
    },
    "uz": {
        "qanday",
        "qaysi",
        "turlari",
        "tur",
        "bor",
        "bormi",
        "mavjud",
        "iltimos",
    },
    "kk": {
        "қандай",
        "қайсы",
        "түрлері",
        "түрі",
        "бар",
        "барма",
        "қорда",
        "сұраймын",
    },
    "default": set(),
}

GREETING_RESPONSES = {
    "ru": "👋 Привет! Я помогу подобрать косметику, уход и парфюмерию. Расскажите, какую задачу решаем или для кого ищем подарок.",
    "uz": "👋 Salom! Kosmetika, parvarish va atirlarni tanlashda yordam beraman. Qaysi vazifa yoki kim uchun sovга kerakligini yozing.",
    "en": "👋 Hi! I’m here to help you choose beauty care, makeup, and fragrances. Tell me the goal or who we’re shopping for.",
    "kk": "👋 Сәлем! Косметика, күтім және хош иістерді таңдауға көмектесемін. Қандай мақсатқа немесе кімге сыйлық керек екенін айтыңыз.",
}

MATERIAL_KNOWLEDGE_BASE = (
    "Уход за кожей: очищение, тонизация, увлажнение, SPF, активы (витамин C, AHA, ретинол).\n"
    "Макияж: базы, тональные основы, корректоры, тени, тушь, румяна, фиксаторы и аксессуары.\n"
    "Парфюмерия: семейства ароматов — цветочные, восточные, древесные, фреш, гурманские.\n"
    "Уход за волосами: шампуни, кондиционеры, маски, несмываемые средства, термозащита, стайлинг.\n"
    "Подарки: готовые наборы, миниатюры, бьюти-боксы, свечи и home-spa коллекции.\n"
)

INTENT_LABELS = {"product", "informational", "greeting", "other"}
SHOW_MORE_PHRASES = {
    "ru": {
        "еще",
        "ещё",
        "покажи еще",
        "покажи ещё",
        "покажи больше",
        "давай еще",
        "давай ещё",
        "хочу еще",
        "больше",
        "продолжай",
        "дальше",
    },
    "uz": {
        "yana",
        "yana ko'rsat",
        "ko'proq",
        "yana ko'proq",
    },
    "en": {
        "more",
        "show more",
        "next",
        "continue",
        "keep going",
    },
    "kk": {
        "тағы",
        "тағы көрсет",
        "көбірек",
        "жалғастыр",
    },
    "default": {"more", "next"},
}
SHOW_MORE_TOKENS = {
    "ru": {"еще", "ещё", "больше", "дальше"},
    "uz": {"yana", "koproq", "ko'proq"},
    "en": {"more", "next"},
    "kk": {"тағы", "көбірек", "одан әрі"},
    "default": {"more"},
}
CONTINUATION_TEMPLATES = {
    "ru": "Продолжаю подбор: показано {shown} из {total}, добавляю ещё {new}.",
    "uz": "Tanlovni davom ettiraman: jami {total} mahsulotdan {shown} ta ko'rsatildi, yana {new} ta qo'shaman.",
    "en": "Continuing selection: showing {shown} of {total}, adding {new} more.",
    "kk": "Таңдауды жалғастырамын: {total} тауардың {shown} көрсетілді, тағы {new} қосамын.",
}
PREVIOUS_LABELS = {
    "ru": "Ранее показанные товары:",
    "uz": "Ilgari ko'rsatilgan mahsulotlar:",
    "en": "Items already shown:",
    "kk": "Бұрын көрсетілген тауарлар:",
}
NO_MORE_RESULTS_MESSAGES = {
    "ru": "Все подходящие товары уже показаны. Уточните запрос, чтобы найти что-то ещё.",
    "uz": "Mos barcha mahsulotlar ko'rsatildi. Yangi natijalar uchun so'rovni aniqlashtiring.",
    "en": "All relevant items have been shown. Refine your request to explore more options.",
    "kk": "Барлық сәйкес тауарлар көрсетілді. Қосымша нұсқалар үшін сұранысты нақтылаңыз.",
}
NO_PREVIOUS_RESULTS_MESSAGES = {
    "ru": "Пока ничего не показывал. Напишите, что требуется подобрать — и начну с подборки.",
    "uz": "Hali hech narsa ko'rsatilgani yo'q. Qanday mahsulot kerakligini yozing — tanlab beraman.",
    "en": "I haven't shown any products yet. Tell me what you need and I'll start the selection.",
    "kk": "Әзірге ештеңе көрсетілмеді. Қандай тауар керек екенін жазыңыз — іріктеуді бастаймын.",
}
CATEGORY_UNAVAILABLE_RESPONSES = {
    "ru": "🚫 В категории {categories} сейчас нет товаров на складе. Напишите, какой аналог ищете — предложу доступные варианты или сообщу о сроках поступления.",
    "uz": "🚫 {categories} toifasida hozircha mahsulot qolmadi. Qanday o‘xshash tovar kerakligini yozing — mavjud variantlarni taklif qilaman yoki kelish vaqtini aytaman.",
    "en": "🚫 The category {categories} is currently out of stock. Tell me what alternative you need and I’ll suggest available options or advise on restock timing.",
    "kk": "🚫 {categories} санатында қазір тауар жоқ. Қандай балама керек екенін жазыңыз — қолжетімді нұсқаларды ұсынамын немесе жеткізу мерзімін хабарлаймын.",
}
SYNONYM_PREFIXES = {
    "spf": {"санскрин", "санкрем", "sunblock", "sunscreen"},
    "тональн": {"foundation", "тоналка", "bb", "cc"},
    "сыворот": {"серум", "serum", "ampoule"},
    "парфюм": {"аромат", "fragrance", "духи"},
    "lip": {"губы", "balm"},
}

SYNONYM_EXACT = {
    "санскрин": {"spf"},
    "sunblock": {"spf"},
    "foundation": {"тональный"},
    "серум": {"сыворотка"},
    "serum": {"сыворотка"},
    "ampoule": {"сыворотка"},
    "duhi": {"аромат"},
    "духи": {"аромат"},
    "balm": {"губы"},
    "бальзам": {"губы"},
    "lipstick": {"губы"},
    "lipbalm": {"губы"},
    "губы": {"помада", "бальзам", "блеск", "тинт", "lip", "balm", "gloss"},
    "тональный": {"foundation", "тональник", "bb", "cc", "кушон"},
}

PRODUCT_PAGE_SIZE = 4


def _get_product_cache_ttl() -> int:
    raw = os.getenv("PRODUCT_CACHE_TTL", "60")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Некорректное значение PRODUCT_CACHE_TTL='%s', используем 60 секунд.", raw)
        return 60
    return max(0, value)


PRODUCT_CACHE_TTL = _get_product_cache_ttl()
PRODUCT_CACHE = {"items": None, "loaded_at": 0.0}
user_product_sessions: dict[int, dict] = {}
user_short_memory: defaultdict[int, dict] = defaultdict(dict)
USER_MEMORY_HISTORY_SIZE = 5
CATEGORY_TOKEN_ALIASES = {
    "лицо": "лицо",
    "лица": "лицо",
    "лицу": "лицо",
    "лице": "лицо",
    "лицом": "лицо",
    "лицевой": "лицо",
    "лицевое": "лицо",
    "лицевого": "лицо",
    "лицевым": "лицо",
    "лицев": "лицо",
    "facial": "лицо",
    "face": "лицо",
    "skin": "кожа",
    "кожа": "кожа",
    "коже": "кожа",
    "кожу": "кожа",
    "кожи": "кожа",
    "волос": "волосы",
    "волосы": "волосы",
    "волосам": "волосы",
    "hair": "волосы",
    "глаз": "глаза",
    "глаза": "глаза",
    "eyes": "глаза",
    "губ": "губы",
    "губа": "губы",
    "губы": "губы",
    "lips": "губы",
    "губной": "губы",
    "губная": "губы",
    "губного": "губы",
    "lip": "губы",
    "помада": "губы",
    "помады": "губы",
    "помадой": "губы",
    "помад": "губы",
    "помаде": "губы",
    "помаду": "губы",
    "бальзам": "губы",
    "бальзамы": "губы",
    "бальзама": "губы",
    "бальзамом": "губы",
    "balm": "губы",
    "tint": "губы",
    "lipstick": "губы",
    "lipgloss": "губы",
    "ногти": "ногти",
    "ногтей": "ногти",
    "ногтю": "ногти",
    "ногтя": "ногти",
    "ногте": "ногти",
    "ногтям": "ногти",
    "ногтями": "ногти",
    "ногтях": "ногти",
    "ногт": "ногти",
    "маникюр": "ногти",
    "маникюра": "ногти",
    "маникюру": "ногти",
    "маникюре": "ногти",
    "маникюрный": "ногти",
    "маникюрная": "ногти",
    "маникюрные": "ногти",
    "маникюрных": "ногти",
    "nail": "ногти",
    "nails": "ногти",
    "cuticle": "ногти",
    "нокти": "ногти",
    "ноктей": "ногти",
    "ноктю": "ногти",
    "ноктя": "ногти",
    "нокте": "ногти",
    "ноктям": "ногти",
    "ноктями": "ногти",
    "ноктях": "ногти",
    "нокт": "ногти",
    "лак": "ногти",
    "лаки": "ногти",
    "лаком": "ногти",
    "лаке": "ногти",
    "лаков": "ногти",
    "гельлак": "ногти",
    "гель-лак": "ногти",
    "шеллак": "ногти",
    "накладные": "ногти",
    "накладной": "ногти",
    "накладных": "ногти",
    "накладка": "ногти",
    "накладку": "ногти",
    "тональный": "тональный",
    "тональная": "тональный",
    "тональные": "тональный",
    "тонального": "тональный",
    "тональному": "тональный",
    "тональным": "тональный",
    "тональном": "тональный",
    "тоналка": "тональный",
    "тоналке": "тональный",
    "тоналку": "тональный",
    "тоналкой": "тональный",
    "тоналок": "тональный",
    "тональник": "тональный",
    "тональники": "тональный",
    "тональников": "тональный",
    "тональнику": "тональный",
    "тональником": "тональный",
    "тональниках": "тональный",
    "foundation": "тональный",
    "фондашн": "тональный",
    "кушон": "тональный",
    "кушоны": "тональный",
    "bb-крем": "тональный",
    "cc-крем": "тональный",
    "bbcream": "тональный",
    "cccream": "тональный",
    "bb": "тональный",
    "cc": "тональный",
    "крем-тон": "тональный",
    "тон-крем": "тональный",
    "пудра": "пудра",
    "пудры": "пудра",
    "пудре": "пудра",
    "пудру": "пудра",
    "пудрой": "пудра",
    "powder": "пудра",
    "праймер": "праймер",
    "праймера": "праймер",
    "праймеру": "праймер",
    "праймером": "праймер",
    "праймеры": "праймер",
    "primer": "праймер",
    "консилер": "консилер",
    "консилера": "консилер",
    "консилеру": "консилер",
    "консилером": "консилер",
    "консилеры": "консилер",
    "concealer": "консилер",
    "камуфляж": "консилер",
    "корректор": "консилер",
    "корректоры": "консилер",
    "корректора": "консилер",
    "румяна": "румяна",
    "румян": "румяна",
    "румянам": "румяна",
    "blush": "румяна",
    "бронзер": "бронзер",
    "бронзера": "бронзер",
    "бронзером": "бронзер",
    "bronzer": "бронзер",
    "контур": "контур",
    "контуринг": "контур",
    "скульптор": "контур",
    "скульптур": "контур",
    "скульпт": "контур",
    "хайлайтер": "хайлайтер",
    "хайлайтера": "хайлайтер",
    "highlighter": "хайлайтер",
    "иллюминатор": "хайлайтер",
    "фиксатор": "фиксатор",
    "фиксаторы": "фиксатор",
    "setting": "фиксатор",
    "спрей": "фиксатор",
    "спреем": "фиксатор",
    "фиксинг": "фиксатор",
    "фиксирующий": "фиксатор",
    "фиксирующая": "фиксатор",
    "фиксирующее": "фиксатор",
    "тушь": "глаза",
    "тушью": "глаза",
    "маскара": "глаза",
    "mascara": "глаза",
    "подводка": "глаза",
    "подводки": "глаза",
    "лайнер": "глаза",
    "eyeliner": "глаза",
    "тени": "глаза",
    "теней": "глаза",
    "палетка": "глаза",
    "палетки": "глаза",
    "палетк": "глаза",
    "eyeshadow": "глаза",
    "smoky": "глаза",
    "смоки": "глаза",
    "бровь": "глаза",
    "брови": "глаза",
    "бровям": "глаза",
    "бровях": "глаза",
    "бровей": "глаза",
    "brow": "глаза",
    "brows": "глаза",
    "ресниц": "глаза",
    "ресницы": "глаза",
    "lashes": "глаза",
    "lash": "глаза",
    "подкручивающая": "глаза",
    "лицо": "лицо",
    "лица": "лицо",
    "губам": "губы",
    "губах": "губы",
    "тело": "кожа",
    "body": "кожа",
    "спину": "кожа",
    "волосы": "волосы",
    "волосам": "волосы",
    "шампуни": "волосы",
    "бальзамов": "волосы",
    "масло для волос": "волосы",
    "масла для волос": "волосы",
    "haircare": "волосы",
    "hair-mask": "волосы",
    "hair-serum": "волосы",
    "styler": "волосы",
    "styling": "волосы",
    "укладки": "волосы",
    "термозащита": "волосы",
    "термозащит": "волосы",
    "термозащитный": "волосы",
    "термозащитная": "волосы",
    "кожи": "кожа",
    "кожей": "кожа",
    "кожам": "кожа",
    "scrub": "кожа",
    "scrubs": "кожа",
    "скрабы": "кожа",
    "пилинги": "кожа",
    "молочко": "кожа",
    "milk": "кожа",
    "butter": "кожа",
    "масло-баттер": "кожа",
    "масло для тела": "кожа",
    "пилка": "ногти",
    "пилки": "ногти",
    "пилк": "ногти",
    "баф": "ногти",
    "бафф": "ногти",
    "баффер": "ногти",
    "manicure": "ногти",
    "cuticle remover": "ногти",
    "oil": "ногти",
    "масло для кутикулы": "ногти",
    "масло для ногтей": "ногти",
    "тонер": "лицо",
    "эссенция": "лицо",
    "essence": "лицо",
    "эмульсия": "лицо",
    "эмульсии": "лицо",
    "serum": "сыворотка",
    "serums": "сыворотка",
    "сыворотка": "сыворотка",
    "сыворотки": "сыворотка",
}

AREA_KEYWORD_TABLE = {
    "лицо": {
        "крем",
        "сыворот",
        "маска",
        "тонер",
        "очищен",
        "тональн",
        "пудра",
        "консилер",
        "румян",
        "бронзер",
        "контур",
        "хайлайтер",
        "праймер",
        "primer",
        "foundation",
        "concealer",
        "bb",
        "cc",
        "иллюминатор",
        "фиксатор",
        "setting",
        "essence",
        "эмульсия",
    },
    "губы": {
        "помада",
        "бальзам",
        "тинт",
        "блеск",
        "lip",
        "gloss",
        "масло",
        "lipstick",
        "oil",
        "scrub",
    },
    "волосы": {
        "шампун",
        "бальзам",
        "кондиционер",
        "маска",
        "oil",
        "styling",
        "уклад",
        "термозащита",
        "сыворотк",
        "спрей",
    },
    "кожа": {
        "лосьон",
        "крем",
        "масло",
        "body",
        "scrub",
        "скраб",
        "пилинг",
        "молочко",
        "баттер",
        "milk",
    },
    "глаза": {
        "тушь",
        "подводка",
        "лайнер",
        "тени",
        "mascara",
        "eyeliner",
        "eyeshadow",
        "палетк",
        "brow",
        "карандаш",
        "gel",
        "lash",
        "liner",
    },
    "ногти": {
        "ногти",
        "лак",
        "гельлак",
        "гель-лак",
        "маникюр",
        "топ",
        "база",
        "cuticle",
        "пилка",
        "бафф",
        "oil",
    },
}

CATEGORY_SPELLCHECK_KEYS: tuple[str, ...] = tuple(
    sorted({token for token in CATEGORY_TOKEN_ALIASES if len(token) >= 3})
)
_MAX_CATEGORY_SPELLCHECK_DISTANCE = 2


def _bounded_levenshtein(left: str, right: str, max_distance: int) -> Optional[int]:
    if left == right:
        return 0
    if not left:
        return len(right) if len(right) <= max_distance else None
    if not right:
        return len(left) if len(left) <= max_distance else None
    if abs(len(left) - len(right)) > max_distance:
        return None

    previous = list(range(len(right) + 1))
    for i, l_ch in enumerate(left, 1):
        current = [i]
        row_min = current[0]
        for j, r_ch in enumerate(right, 1):
            cost = 0 if l_ch == r_ch else 1
            insert_cost = current[j - 1] + 1
            delete_cost = previous[j] + 1
            replace_cost = previous[j - 1] + cost
            best = min(insert_cost, delete_cost, replace_cost)
            current.append(best)
            if best < row_min:
                row_min = best
        if row_min > max_distance:
            return None
        previous = current
    distance = previous[-1]
    return distance if distance <= max_distance else None


def _spellcheck_category_token(token: str) -> Optional[str]:
    if len(token) < 3:
        return None
    first_char = token[0]
    best_candidate: Optional[str] = None
    best_distance = _MAX_CATEGORY_SPELLCHECK_DISTANCE + 1
    for candidate in CATEGORY_SPELLCHECK_KEYS:
        if candidate[0] != first_char:
            continue
        distance = _bounded_levenshtein(token, candidate, _MAX_CATEGORY_SPELLCHECK_DISTANCE)
        if distance is None:
            continue
        if distance < best_distance:
            best_candidate = candidate
            best_distance = distance
            if best_distance == 1:
                break
    return best_candidate

RECOMMENDATION_LANGUAGE_HINTS = {
    "ru": "Отвечай на русском языке.",
    "en": "Respond in English.",
    "uz": "Javobni o'zbek tilida yoz.",
    "kk": "Жауапты қазақ тілінде жаз.",
}

RECOMMENDATION_TONE_HINTS = {
    "ru": "Коммуницируй дружелюбно, уважительно и профессионально, как бьюти-консультант люксового бутика.",
    "en": "Keep a warm, respectful, boutique consultant tone.",
    "uz": "Ohangingiz iliq va professionallik bilan to'lgan bo'lsin, go'yoki premium butik maslahatchisisiz.",
    "kk": "Тон жылылықпен және кәсіби түрде болсын, сиқырушы дүкен кеңесшісі тәрізді.",
}

MAX_RECOMMENDATION_PRODUCTS = 2

HEALTH_KEYWORDS = {
    "ru": {
        "здоровье",
        "здоровья",
        "здоров",
        "аллергия",
        "аллергии",
        "аллерген",
        "аллергическая",
        "акне",
        "прыщи",
        "прыщ",
        "дерматит",
        "экзема",
        "высыпания",
        "высыпание",
        "сыпь",
        "воспаление",
        "беременность",
        "беременным",
        "лактация",
        "гормоны",
        "противопоказания",
        "болит",
        "болезненные",
        "зож",
        "врач",
        "дерматолог",
        "дерматолога",
        "косметолог",
        "диагноз",
    },
    "en": {
        "health",
        "healthy",
        "allergy",
        "allergies",
        "hypoallergenic",
        "acne",
        "breakouts",
        "eczema",
        "dermatitis",
        "inflammation",
        "pregnant",
        "pregnancy",
        "breastfeeding",
        "sensitive",
        "sensitivity",
        "doctor",
        "dermatologist",
        "contraindication",
        "pain",
        "redness",
    },
    "uz": {
        "salomatlik",
        "ogriq",
        "allergiya",
        "akne",
        "tochka",
        "yalliglanish",
        "homilador",
        "emizish",
        "sezgir",
        "shifokor",
    },
    "kk": {
        "денсаулық",
        "аллергия",
        "акне",
        "жүктілік",
        "емізу",
        "қышу",
        "қызару",
        "дәрігер",
        "дерматолог",
    },
    "default": {
        "contraindications",
        "hypo",
        "irritation",
        "rash",
    },
}

HEALTH_SUBSTRINGS = {
    "ru": (
        "можно ли при беременности",
        "можно ли беременным",
        "после родов",
        "при лактации",
        "после процедуры",
        "после пилинга",
        "после лазера",
        "после татуажа",
        "болезни кожи",
        "состояние здоровья",
    ),
    "en": (
        "during pregnancy",
        "while pregnant",
        "breastfeeding safe",
        "medical condition",
        "doctor said",
        "after procedure",
    ),
    "default": (
        "contraindication",
        "medical history",
    ),
}

SKIN_TYPE_KEYWORDS = {
    "sensitive": {
        "чувствит",
        "sensitive",
        "сенситив",
        "reactive",
        "щиплет",
        "покраснен",
        "irritation",
    },
    "dry": {
        "сух",
        "dry",
        "обезвож",
        "шелуш",
        "tightness",
    },
    "oily": {
        "жир",
        "oily",
        "shine",
        "блест",
        "сальный",
        "акне",
        "comed",
        "поры",
    },
    "combination": {
        "комбинир",
        "mixed",
        "t-zone",
        "т-зона",
        "комби",
    },
    "normal": {
        "нормальн",
        "balanced",
        "обычная",
    },
}

SKIN_TYPE_LABELS = {
    "sensitive": {
        "ru": "чувствительная кожа",
        "en": "sensitive skin",
        "uz": "sezgir teri",
        "kk": "сезімтал тері",
    },
    "dry": {
        "ru": "сухая кожа",
        "en": "dry skin",
        "uz": "quruq teri",
        "kk": "құрғақ тері",
    },
    "oily": {
        "ru": "жирная кожа",
        "en": "oily skin",
        "uz": "yog'li teri",
        "kk": "майлы тері",
    },
    "combination": {
        "ru": "комбинированная кожа",
        "en": "combination skin",
        "uz": "kombinatsiyalangan teri",
        "kk": "аралас тері",
    },
    "normal": {
        "ru": "нормальная кожа",
        "en": "normal skin",
        "uz": "normal teri",
        "kk": "қалыпты тері",
    },
}

SKIN_TYPE_PRODUCT_HINTS = {
    "sensitive": {
        "keywords": {
            "чувств",
            "sensitive",
            "calm",
            "soothe",
            "барьер",
            "cicaplast",
            "recovery",
        },
        "usage": "Учитываю чувствительную кожу: выбираю мягкие формулы без агрессивных кислот.",
    },
    "dry": {
        "keywords": {
            "увлажн",
            "hydr",
            "керамид",
            "масло",
            "lipid",
        },
        "usage": "Учитываю сухость: делаю акцент на глубоком увлажнении и восстановлении барьера.",
    },
    "oily": {
        "keywords": {
            "матир",
            "sebum",
            "oil-control",
            "niacinamide",
            "salycil",
            "bha",
        },
        "usage": "Учитываю избыточный себум: подбираю лёгкие формулы с балансирующими компонентами.",
    },
    "combination": {
        "keywords": {
            "баланс",
            "комбинирован",
            "matte",
            "lightweight",
            "t-zone",
        },
        "usage": "Учитываю комбинированный тип: делаю упор на баланс Т-зоны и мягкое увлажнение.",
    },
    "normal": {
        "keywords": {
            "баланс",
            "универсаль",
            "daily",
            "ежеднев",
        },
        "usage": "Учитываю нормальную кожу: фокус на поддержании баланса и комфорта.",
    },
}


def _to_product_record(item: DBProduct) -> SimpleNamespace:
    """Преобразует ORM-объект товара в независимую структуру для кеша."""
    category_obj = getattr(item, "category_obj", None)
    category_name = getattr(category_obj, "name", None) if category_obj is not None else None
    pictures_raw = getattr(item, "picture", None) or []
    pictures = [normalize_text(photo) or photo for photo in pictures_raw if photo]

    return SimpleNamespace(
        id=getattr(item, "id", None),
        name=getattr(item, "name", None),
        category=category_name,
        description=getattr(item, "description", None),
        tags=getattr(item, "tags", None),
        picture=pictures,
        cover=getattr(item, "cover", None),
        price=getattr(item, "price", None),
        old_price=getattr(item, "old_price", None),
        tag=getattr(item, "tag", None),
        status=getattr(item, "status", None),
    )


ALTERNATIVE_KEYWORDS = {
    "spf": ["санскрин", "sunblock", "sunscreen"],
    "санскрин": ["spf", "sunscreen"],
    "тональн": ["bb крем", "cc крем", "кушон"],
    "тональный": ["bb крем", "cc крем", "foundation"],
    "сыворот": ["серум", "эссенция", "ampoule"],
    "серум": ["сыворотка", "essence"],
    "помада": ["lip tint", "блеск", "lip gloss"],
    "тушь": ["маскара", "lash"],
    "шампун": ["кондиционер", "маска для волос", "balsam"],
    "аромат": ["парфюм", "туалетная вода", "body mist"],
}

ANALOG_RESPONSES = {
    "ru": "Прямого наличия по запросу «{topic}» нет, показываю ближайшие аналоги:",
    "en": "I don’t see “{topic}” in stock, here are close alternatives:",
    "uz": "«{topic}» bo‘yicha omborda yo‘q, o‘xshash variantlarni ko‘rsataman:",
    "kk": "«{topic}» қазір қолжетімді емес, ұқсас нұсқалар:",
}

MEMORY_STOPWORDS = {
    "ru": {
        "что",
        "это",
        "такое",
        "про",
        "расскажи",
        "подскажи",
        "нужно",
        "нужен",
        "нужна",
        "нужны",
        "у",
        "тебя",
        "есть",
        "в",
        "на",
        "ли",
        "сколько",
        "какой",
        "какая",
        "какие",
        "мне",
        "для",
        "как",
        "можно",
        "если",
        "покажи",
        "покажите",
        "вид",
        "виды",
        "тип",
        "типы",
        "ищу",
        "интересует",
        "интересуют",
    },
    "en": {
        "what",
        "is",
        "about",
        "tell",
        "need",
        "do",
        "you",
        "have",
        "in",
        "the",
        "a",
        "an",
        "for",
        "how",
        "many",
        "can",
        "get",
        "please",
    },
    "uz": {"nima", "bu", "haqida", "kerak", "menga", "bor", "bormi"},
    "kk": {"не", "ол", "туралы", "маған", "бар", "барма"},
    "default": {"что", "это", "есть", "в", "на", "ли", "the", "and", "for", "how"},
}

FOLLOWUP_PHRASES = {
    "ru": {
        "есть в наличии",
        "в наличии есть",
        "есть ли в наличии",
        "у тебя есть",
        "у вас есть",
        "наличие есть",
        "в наличии?",
    },
    "en": {
        "in stock",
        "do you have",
        "have it",
        "is it available",
        "available now",
    },
    "uz": {"bor mi", "zaxirada bormi", "mavjudmi"},
    "kk": {"бар ма", "қоймада бар ма", "қорда бар ма"},
    "default": set(),
}

FOLLOWUP_KEYWORDS = {
    "ru": {"наличии", "наличие", "имеется", "доступен", "доступно", "продаешь"},
    "en": {"available", "stock", "have", "carry", "sell"},
    "uz": {"mavjud", "zaxira", "bor"},
    "kk": {"бар", "қорда", "қоймада"},
    "default": {"available", "stock"},
}

FOLLOWUP_NOISE_TOKENS = {
    "ru": {"у", "тебя", "вас", "есть", "ли", "в", "на", "по", "про", "меня", "подскажи", "подскажите", "скажите", "мне"},
    "en": {"do", "you", "have", "it", "any", "the", "a", "an", "is", "there", "please", "now", "in"},
    "uz": {"bor", "mi", "menga", "iltimos"},
    "kk": {"бар", "ма", "маған"},
    "default": {"do", "you", "have", "it", "any", "the", "a", "an", "in", "is"},
}

FOLLOWUP_RESPONSES = {
    "ru": "Проверил по запросу «{topic}» — вот что есть в наличии.",
    "uz": "«{topic}» bo‘yicha mavjud mahsulotlar:",
    "en": "Here’s what we have in stock for “{topic}”.",
    "kk": "«{topic}» бойынша қолжетімді тауарлар:",
}

FOLLOWUP_NOT_FOUND = {
    "ru": "По «{topic}» сейчас нет позиций в каталоге. Напишите параметры — проверю склад вручную.",
    "uz": "«{topic}» bo‘yicha hozircha mahsulot topilmadi. O‘lcham yoki miqdorni yozing — omborni qo‘lda tekshirib beraman.",
    "en": "I don’t see items for “{topic}” in the catalog right now. Share details and I’ll double-check stock manually.",
    "kk": "«{topic}» бойынша каталогта әзірге тауар жоқ. Өлшемін немесе көлемін жазыңыз — қойманы жеке тексеремін.",
}


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
    params = {"model": model, "messages": messages}
    if max_tokens is not None:
        params["max_completion_tokens"] = max_tokens

    response = client.chat.completions.create(**params)
    choice = response.choices[0]
    content = _normalize_message_text(choice.message.content)
    return content.strip()


def _extract_focus_keyword(text: str, lang: str) -> Optional[str]:
    tokens = _tokenize_simple(text)
    if not tokens:
        return None
    stopwords = MEMORY_STOPWORDS.get(lang, MEMORY_STOPWORDS["default"])
    for token in reversed(tokens):
        if token not in stopwords and len(token) >= 3:
            return token
    for token in reversed(tokens):
        if token not in stopwords:
            return token
    return tokens[-1]


def _remember_user_topic(user_id: int, lang: str, query: str) -> None:
    if not query:
        return
    keyword = _extract_focus_keyword(query, lang)
    display = (keyword or query).strip()
    if len(display) > 80 and keyword:
        display = keyword
    if not display:
        return
    memory = user_short_memory[user_id]
    topic = (keyword or display).strip()
    memory["topic"] = topic
    memory["query"] = query.strip()
    memory["display"] = display
    memory["lang"] = lang
    memory["timestamp"] = time.time()
    history = memory.setdefault("history", [])
    history.append(
        {
            "timestamp": memory["timestamp"],
            "query": query.strip(),
            "topic": topic,
            "display": display,
            "lang": lang,
        }
    )
    if len(history) > USER_MEMORY_HISTORY_SIZE:
        del history[0 : len(history) - USER_MEMORY_HISTORY_SIZE]


def _extract_user_profile(text: str, lang: str) -> dict:
    normalized = _normalize_query(text)
    profile: dict = {}
    for skin_type, keywords in SKIN_TYPE_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            profile["skin_type"] = skin_type
            label_map = SKIN_TYPE_LABELS.get(skin_type, {})
            profile["skin_type_display"] = label_map.get(lang) or label_map.get("ru") or skin_type
            break
    if profile:
        profile["updated_at"] = time.time()
    return profile


def _remember_user_profile(user_id: int, lang: str, profile: dict) -> None:
    if not profile:
        return
    memory = user_short_memory[user_id]
    stored = memory.setdefault("profile", {})
    stored.update(profile)
    stored["lang"] = lang
    stored["timestamp"] = time.time()


def _get_user_profile(user_id: int) -> dict:
    memory = user_short_memory.get(user_id)
    if not memory:
        return {}
    return memory.get("profile", {})


def _format_profile_hint(profile: dict, lang: str) -> Optional[str]:
    if not profile:
        return None
    skin_type = profile.get("skin_type")
    if not skin_type:
        return None
    label_map = SKIN_TYPE_LABELS.get(skin_type, {})
    label = label_map.get(lang) or label_map.get("ru") or skin_type
    hint_map = SKIN_TYPE_PRODUCT_HINTS.get(skin_type, {})
    usage = hint_map.get("usage")
    core = f"✨ Учитываю: {label}."
    if usage:
        return f"{core} {usage}"
    return core


def _collect_analog_queries(text: str, lang: str) -> List[str]:
    normalized = _normalize_query(text)
    analogs = []
    for key, values in ALTERNATIVE_KEYWORDS.items():
        if key in normalized:
            analogs.extend(values)
    if lang != "ru":
        for key, values in ALTERNATIVE_KEYWORDS.items():
            if any(value in normalized for value in values):
                analogs.extend(values)
    seen = set()
    ordered: List[str] = []
    for candidate in analogs:
        candidate_norm = candidate.strip().lower()
        if candidate_norm and candidate_norm not in seen:
            ordered.append(candidate)
            seen.add(candidate_norm)
    return ordered


def _build_analog_intro(lang: str, topic: str) -> str:
    template = ANALOG_RESPONSES.get(lang) or ANALOG_RESPONSES["ru"]
    return template.format(topic=topic)


def _is_availability_followup(normalized_query: str, tokens: List[str], lang: str) -> bool:
    if not normalized_query:
        return False
    token_set = set(tokens)
    structural_tokens = {"что", "какие", "какой", "какая", "каких", "какую", "какого", "для"}
    structural_block = len(token_set) >= 4 and bool(structural_tokens & token_set)
    noise_tokens = (
        FOLLOWUP_NOISE_TOKENS.get(lang, set())
        | FOLLOWUP_NOISE_TOKENS.get("default", set())
        | FOLLOWUP_KEYWORDS.get(lang, set())
        | FOLLOWUP_KEYWORDS.get("default", set())
    )

    def has_meaningful_tokens() -> bool:
        for token in token_set:
            if len(token) <= 2:
                continue
            if token not in noise_tokens:
                return True
        return False

    lang_phrases = FOLLOWUP_PHRASES.get(lang, set()) | FOLLOWUP_PHRASES.get("default", set())
    for phrase in lang_phrases:
        if phrase and phrase in normalized_query and not structural_block:
            if has_meaningful_tokens():
                return False
            return True
    if structural_block:
        return False
    if not token_set:
        return False
    keywords = FOLLOWUP_KEYWORDS.get(lang, set()) | FOLLOWUP_KEYWORDS.get("default", set())
    if keywords & token_set and len(token_set) <= 6:
        if has_meaningful_tokens():
            return False
        return True
    return False


def _build_followup_intro(lang: str, topic: str) -> str:
    template = FOLLOWUP_RESPONSES.get(lang) or FOLLOWUP_RESPONSES["ru"]
    return template.format(topic=topic)


def _build_followup_not_found(lang: str, topic: str) -> str:
    template = FOLLOWUP_NOT_FOUND.get(lang) or FOLLOWUP_NOT_FOUND["ru"]
    return template.format(topic=topic)


def _handle_availability_followup(user_id: int, lang: str) -> Optional[dict]:
    memory = user_short_memory.get(user_id)
    if not memory:
        return None

    stored_query = (memory.get("query") or "").strip()
    topic = (memory.get("topic") or stored_query).strip()
    if not stored_query and not topic:
        return None

    candidate_queries: List[str] = []
    candidate_set: set[str] = set()
    if stored_query:
        candidate_queries.append(stored_query)
        candidate_set.add(stored_query)
    if topic and topic not in candidate_queries:
        candidate_queries.append(topic)
        candidate_set.add(topic)

    history = memory.get("history") or []
    for entry in reversed(history):
        q = (entry or {}).get("query", "").strip()
        t = (entry or {}).get("topic", "").strip()
        if q and q not in candidate_set:
            candidate_queries.append(q)
            candidate_set.add(q)
        if t and t not in candidate_set:
            candidate_queries.append(t)
            candidate_set.add(t)

    analog_queries = _collect_analog_queries(topic or stored_query, lang)
    for alt in analog_queries:
        if alt not in candidate_set:
            candidate_queries.append(alt)
            candidate_set.add(alt)

    last_categories: List[str] = []

    profile = memory.get("profile") if memory else None

    for query_option in candidate_queries:
        if not query_option:
            continue
        products_payload, matched_products, price_limit, full_payload, mentioned_categories = search_products(
            query_option,
            lang,
            user_profile=profile,
        )
        if products_payload:
            _store_product_session(user_id, query_option, lang, full_payload, price_limit, profile)
            _remember_user_topic(user_id, lang, query_option)
            if query_option.strip().lower() in {stored_query.lower(), (topic or "").lower()}:
                intro = _build_followup_intro(lang, memory.get("display") or topic or query_option)
                meta_followup = {"followup": True}
            else:
                intro = _build_analog_intro(lang, memory.get("display") or topic or query_option)
                meta_followup = {"followup": True, "analog": True}
            summary = build_summary_text(matched_products, lang, price_limit, total_count=len(full_payload))
            text_response = f"{intro}\n\n{summary}" if summary else intro
            return {
                "text": text_response,
                "products": products_payload,
                "meta": {
                    "page": 1,
                    "total": len(full_payload),
                    "display_text": True,
                    "continuation": False,
                    **meta_followup,
                },
            }
        if mentioned_categories:
            last_categories = mentioned_categories

    if last_categories:
        unavailable_text = _build_category_unavailable_message(last_categories, lang)
        return {
            "text": unavailable_text,
            "products": [],
            "meta": {
                "display_text": True,
                "continuation": False,
                "followup": True,
                "category_unavailable": True,
            },
        }

    display_topic = memory.get("display") or topic
    session_snapshot = user_product_sessions.get(user_id)
    if session_snapshot and session_snapshot.get("products"):
        cached_products: List[dict] = session_snapshot.get("products", [])
        if cached_products:
            first_batch = cached_products[: PRODUCT_PAGE_SIZE]
            return {
                "text": "Показываю последние подобранные позиции. Напишите «ещё», если нужно продолжить.",
                "products": first_batch,
                "meta": {
                    "display_text": True,
                    "continuation": True,
                    "followup": True,
                },
            }

    if display_topic:
        message = _build_followup_not_found(lang, memory.get("display") or topic)
        return {
            "text": message,
            "products": [],
            "meta": {
                "display_text": True,
                "continuation": False,
                "followup": True,
                "no_match": True,
            },
        }
    return None


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
            claude_messages.append({"role": role, "content": text})

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
    errors: List[Tuple[str, Exception]] = []
    for provider in LLM_PROVIDER_SEQUENCE:
        label = PROVIDER_LABELS.get(provider, provider)
        if provider == "openai":
            try:
                gpt_content = _call_openai(messages, max_tokens, model)
                if _is_empty_response(gpt_content):
                    raise ValueError("GPT returned an empty answer")
                return gpt_content, label
            except Exception as error:  # noqa: BLE001
                errors.append(("openai", error))
                logger.warning("OpenAI недоступен или вернул пустой ответ: %s", error)
        elif provider == "anthropic":
            if not _claude_client:
                errors.append(("anthropic", RuntimeError("Anthropic client is not configured")))
                logger.warning("Anthropic клиент недоступен — пропуск провайдера.")
                continue
            try:
                claude_content = _call_claude(messages, max_tokens)
                if _is_empty_response(claude_content):
                    raise ValueError("Claude returned an empty answer")
                logger.info("Использован Anthropic (%s)", CLAUDE_MODEL)
                return claude_content, label
            except Exception as error:  # noqa: BLE001
                errors.append(("anthropic", error))
                logger.error("Не удалось получить ответ от Anthropic: %s", error)
        else:
            logger.warning("Неизвестный провайдер LLM '%s' — пропуск.", provider)

    error_details = "; ".join(f"{name}: {err}" for name, err in errors) or "нет доступных провайдеров"
    raise RuntimeError(f"Все провайдеры LLM недоступны ({error_details})")


# === Утилиты ===
def normalize_url(url: str) -> str:
    from urllib.parse import quote, urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
        path = quote(parts.path, safe="/%") if parts.path else ""
        query = quote(parts.query, safe="=&?") if parts.query else ""
        return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))
    except Exception:
        return url


def _combine_blocks(*parts: Optional[str]) -> str:
    blocks: List[str] = []
    for part in parts:
        if not part:
            continue
        cleaned = part.strip()
        if cleaned:
            blocks.append(cleaned)
    return "\n\n".join(blocks)


def _normalize_category_token(token: str) -> str:
    base = token.lower().replace("ё", "е")
    alias = CATEGORY_TOKEN_ALIASES.get(base)
    if alias:
        return alias
    corrected = _spellcheck_category_token(base)
    if corrected:
        return CATEGORY_TOKEN_ALIASES.get(corrected, corrected)
    return base


def _normalize_query(text: str) -> str:
    return text.strip().lower().replace("ё", "е")


def _tokenize_simple(text: str) -> List[str]:
    return [token.replace("ё", "е") for token in re.findall(r"[a-zа-я0-9]+", text.lower()) if token]


def _expand_token_set(tokens: Iterable[str]) -> set[str]:
    expanded: set[str] = set()
    queue: List[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token:
            queue.append(token.replace("ё", "е"))

    prefix_strips = ("для", "про", "под", "по")

    while queue:
        raw = queue.pop()
        if not raw:
            continue
        if raw in seen:
            continue
        seen.add(raw)
        normalized = _normalize_category_token(raw)
        if len(normalized) > 2:
            expanded.add(normalized)
        for variant in _token_variants(raw):
            if variant and variant not in seen:
                queue.append(variant)
        for prefix in prefix_strips:
            if raw.startswith(prefix) and len(raw) - len(prefix) >= 3:
                stripped = raw[len(prefix):]
                if stripped not in seen:
                    queue.append(stripped)
            if raw.startswith(prefix + "-") and len(raw) - len(prefix) - 1 >= 3:
                stripped = raw[len(prefix) + 1 :]
                if stripped not in seen:
                    queue.append(stripped)
        for prefix, synonyms in SYNONYM_PREFIXES.items():
            if normalized.startswith(prefix):
                for synonym in synonyms:
                    normalized_syn = synonym.replace("ё", "е")
                    if normalized_syn not in seen:
                        queue.append(normalized_syn)
        exact_synonyms = SYNONYM_EXACT.get(normalized)
        if exact_synonyms:
            for synonym in exact_synonyms:
                normalized_syn = synonym.replace("ё", "е")
                if normalized_syn not in seen:
                    queue.append(normalized_syn)
    return expanded


def _semantic_similarity_score(query: str, text: str) -> float:
    if not query or not text:
        return 0.0
    query_norm = query.lower()
    text_norm = text.lower()
    ratio = SequenceMatcher(None, query_norm, text_norm).ratio()
    query_tokens = set(_tokenize_simple(query_norm))
    if not query_tokens:
        return ratio
    text_tokens = set(_tokenize_simple(text_norm))
    overlap = len(query_tokens & text_tokens)
    coverage = overlap / len(query_tokens)
    return (ratio * 0.6) + (coverage * 0.4)


def _is_show_more_request(text: str, lang: str) -> bool:
    base = _normalize_query(text)
    phrases = SHOW_MORE_PHRASES.get(lang, set()) | SHOW_MORE_PHRASES.get("default", set())
    if base in phrases:
        return True

    tokens = set(_tokenize_simple(text))
    if not tokens:
        return False

    token_keywords = SHOW_MORE_TOKENS.get(lang, set()) | SHOW_MORE_TOKENS.get("default", set())
    if tokens & token_keywords:
        # если сообщение короткое (до 3 слов) и состоит из служебных слов — считаем продолжением
        if len(tokens) <= 3:
            return True
        # или если начинается с фраз "покажи", "давай", и т.п.
        for word in ("покажи", "давай", "show"):
            if base.startswith(word) and (("еще" in tokens) or ("ещё" in tokens) or ("more" in tokens)):
                return True
    return False


LOW_INFO_KEYWORDS = {
    "шо",
    "чо",
    "че",
    "чё",
    "что",
    "ок",
    "окей",
    "ага",
    "да",
    "нет",
    "ну",
    "ладно",
    "привет",
    "салам",
    "hey",
    "hi",
    "hello",
    "yo",
    "ok",
    "okey",
    "hmm",
    "мм",
    "??",
    "?",
    "....",
}


def _is_low_information_query(text: str) -> bool:
    normalized = _normalize_query(text)
    if not normalized:
        return True

    stripped = normalized.strip()
    tokens = _tokenize_simple(text)
    compact = re.sub(r"[^a-zа-я0-9]+", "", stripped)

    if stripped in LOW_INFO_KEYWORDS or compact in LOW_INFO_KEYWORDS:
        return True
    if not compact:
        return True
    if not tokens and len(compact) <= 3:
        return True
    if len(tokens) == 1 and len(tokens[0]) <= 2:
        return True
    return False


def _build_low_info_response(lang: str) -> str:
    return LOW_INFO_RESPONSES.get(lang) or LOW_INFO_RESPONSES.get("ru")


def _build_greeting_response(lang: str) -> str:
    return GREETING_RESPONSES.get(lang) or GREETING_RESPONSES.get("ru")


def _is_health_query(text: str, lang: str) -> bool:
    normalized = _normalize_query(text)
    tokens = set(_tokenize_simple(text))
    keyword_pool = set(HEALTH_KEYWORDS.get("default", set()))
    keyword_pool.update(HEALTH_KEYWORDS.get(lang, set()))
    if tokens & keyword_pool:
        return True
    substrings = set(HEALTH_SUBSTRINGS.get("default", ()))
    substrings.update(HEALTH_SUBSTRINGS.get(lang, ()))
    for needle in substrings:
        if needle and needle in normalized:
            return True
    return False


def _build_health_response(text: str, lang: str) -> str:
    language_hint = RECOMMENDATION_LANGUAGE_HINTS.get(lang, RECOMMENDATION_LANGUAGE_HINTS["ru"])
    tone_hint = RECOMMENDATION_TONE_HINTS.get(lang, RECOMMENDATION_TONE_HINTS["ru"])
    system_prompt = (
        "Ты — beauty-консультант LuxeBeauty, а не врач. "
        "Объясняй понятным языком, избегай медицинских диагнозов и обещаний излечения. "
        f"{tone_hint} {language_hint} "
        "Всегда подчёркивай, что нужна консультация профильного специалиста при серьёзных симптомах.\n"
        "Структура ответа:\n"
        "1) Коротко и сочувственно перефразируй беспокойство клиента.\n"
        "2) Дай 2–3 общих рекомендации по уходу и образу жизни, основанных на запросе. "
        "Используй мягкие формулировки («можно попробовать», «обратите внимание»), без упоминания лекарств.\n"
        "3) Добавь блок «Важно», где двумя короткими пунктами предупреди о необходимости консультации врача и о тесте на чувствительность (патч-тест).\n"
        "4) Заверши вопросом, приглашающим уточнить состояние кожи или ограничения врача.\n"
        "Не упоминай цены, не давай медицинских назначений."
    )
    user_prompt = f"Вопрос клиента: {text.strip() or 'не указан'}"
    try:
        content, provider = call_chat_with_fallback(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=360,
        )
        answer = (content or "").strip()
        if not answer:
            logger.debug("LLM provider %s вернул пустой health-ответ", provider)
            raise ValueError("empty health response")
        return answer
    except Exception as error:
        logger.warning("Не удалось получить health-ответ: %s", error)
        fallback_map = {
            "ru": (
                "Я могу подсказать по уходу, но точную диагностику и лечение назначает только врач. "
                "Постарайтесь выбрать максимально мягкие формулы, сделайте патч-тест и обсудите симптомы с дерматологом."
            ),
            "en": (
                "I can talk through skincare ideas, but only a medical professional can diagnose and prescribe treatment. "
                "Pick gentle formulas, patch-test first, and speak with your dermatologist about ongoing symptoms."
            ),
            "uz": (
                "Men parvarish bo‘yicha maslahat bera olaman, ammo aniq tashxis va davolashni faqat shifokor belgilaydi. "
                "Yumshoq formulalarni tanlang, patç-test qiling va dermatolog bilan maslahatlashishni unutmang."
            ),
            "kk": (
                "Мен күтім жайлы кеңес бере аламын, бірақ нақты диагноз бен емді тек дәрігер қояды. "
                "Жұмсақ формулаларды таңдаңыз, патч-тест өткізіп, дерматологпен кеңесіңіз."
            ),
        }
        return fallback_map.get(lang, fallback_map["ru"])


def _classify_intent(text: str, lang: str) -> str:
    prompt = (
        "Classify the user's message about cosmetics, skincare, makeup, or beauty shopping.\n"
        "Labels:\n"
        "- product: the user wants to buy, pick, compare, or request specific products or delivery.\n"
        "- informational: the user asks for definitions, explanations, ingredients, routines, pros/cons, or usage guidance without requesting a purchase.\n"
        "- greeting: the user greets, thanks, or exchanges pleasantries.\n"
        "- other: anything else.\n"
        "Return ONLY one label in lowercase."
    )
    try:
        content, _ = call_chat_with_fallback(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
            max_tokens=5,
        )
        label = content.strip().lower()
        if label in INTENT_LABELS:
            return label
    except Exception as error:
        logger.debug("Intent classification failed: %s", error)
    return "other"


def _build_informational_answer(text: str, lang: str) -> str:
    reference_answer = get_material_reference(text, lang)
    if reference_answer:
        return reference_answer

    system_prompt = (
        "Ты — консультант по косметике и уходу. Отвечай дружелюбно, структурировано и по делу.\n"
        "Формат ответа:\n"
        "1) одна строка-заголовок без emoji и без выделения жирным;\n"
        "2) 1–2 строки с общим описанием ситуации или средства;\n"
        "3) блоки «Плюсы» и «Минусы» в виде списков, где каждый пункт начинается с дефиса;\n"
        "4) при необходимости добавь заключительный совет, начинающийся со слова «Совет:» (или эквивалента на языке пользователя);\n"
        "5) завершай приглашением задать уточнения.\n"
        "Используй следующую справку по продуктам (не перечисляй её полностью, выбирай по смыслу запроса):\n"
        f"{MATERIAL_KNOWLEDGE_BASE}\n"
        "Если вопрос о выборе, упомяни критерии: тип кожи, текстура, аромат, стойкость, бюджет. Не придумывай цены. "
        "Не используй emoji, стикеры и Markdown-форматирование.\n"
        "Отвечай на языке пользователя; если не уверен, используй русский."
    )
    try:
        content, _ = call_chat_with_fallback(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            max_tokens=400,
        )
        cleaned = content.strip()
        if cleaned:
            return cleaned
    except Exception as error:
        logger.warning("Не удалось получить информационный ответ от GPT: %s", error)

    fallback = MATERIAL_KNOWLEDGE_BASE.split("\n", 1)[0]
    return fallback


def _build_no_match_response(lang: str, query: str) -> str:
    template = NO_MATCH_RESPONSES.get(lang) or NO_MATCH_RESPONSES.get("ru")
    safe_query = query.strip() or "запрос"
    return template.format(query=safe_query)


def _format_categories(categories: List[str]) -> str:
    if not categories:
        return ""
    quoted = [f"«{category}»" for category in categories]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + " и " + quoted[-1]


def _build_category_unavailable_message(categories: List[str], lang: str) -> str:
    message_template = CATEGORY_UNAVAILABLE_RESPONSES.get(lang) or CATEGORY_UNAVAILABLE_RESPONSES["ru"]
    return message_template.format(categories=_format_categories(categories))


GENERAL_CARE_RESPONSES = {
    "ru": (
        "💡 Советы по уходу:\n"
        "1. Снимайте макияж двухэтапным очищением, даже если не красились.\n"
        "2. Комбинируйте увлажняющие и защитные средства по сезону.\n"
        "3. Вводите новые активы по одному и отслеживайте реакцию кожи.\n"
        "4. Используйте SPF 30+ круглый год и обновляйте каждые 2–3 часа на улице.\n"
        "5. Храните косметику в прохладном месте и следите за сроком годности."
    ),
    "en": (
        "💡 Beauty care essentials:\n"
        "1. Remove makeup with a two-step cleanse every evening.\n"
        "2. Layer hydration and protection according to the season.\n"
        "3. Introduce new actives one at a time and monitor your skin response.\n"
        "4. Wear SPF 30+ daily and reapply every 2–3 hours outdoors.\n"
        "5. Store products in a cool place and track expiration dates."
    ),
    "uz": (
        "💡 Parvarish bo‘yicha maslahatlar:\n"
        "1. Kechqurun, hatto makiyaj bo‘lmasa ham, ikki bosqichli tozalash qiling.\n"
        "2. Mavsumga qarab namlovchi va himoya qiluvchi vositalarni qatlamlang.\n"
        "3. Yangi faol moddalarni bittadan kiriting va teri reaksiyasini kuzating.\n"
        "4. SPF 30+ ni yil bo‘yi surting va tashqarida 2–3 soatda yangilang.\n"
        "5. Kosmetikani salqin joyda saqlang va amal qilish muddatiga e’tibor bering."
    ),
    "kk": (
        "💡 Күтім бойынша кеңестер:\n"
        "1. Макияжды кеш сайын екі кезеңмен тазалаңыз, тіпті боянбасаңыз да.\n"
        "2. Маусымға сай ылғалдандыру мен қорғаныс құралдарын қабаттастырыңыз.\n"
        "3. Жаңа белсенді компоненттерді біртіндеп енгізіп, тері реакциясын бақылаңыз.\n"
        "4. SPF 30+ күн сайын жағып, далада әр 2–3 сағатта қайталаңыз.\n"
        "5. Косметиканы салқын жерде сақтап, жарамдылық мерзімін қадағалаңыз."
    ),
}


def _build_general_care_advice(text: str, lang: str) -> str | None:
    normalized = _normalize_query(text)
    keywords = GENERAL_CARE_KEYWORDS.get(lang, ())
    default_keywords = GENERAL_CARE_KEYWORDS.get("ru", ())
    triggers = (*keywords, *default_keywords, "уход", "beauty", "skin")
    if not any(keyword in normalized for keyword in triggers):
        return None
    base = GENERAL_CARE_RESPONSES.get(lang, GENERAL_CARE_RESPONSES["ru"])
    catalog_summary = _build_material_catalog_summary(lang)
    if catalog_summary:
        return base + "\n\n" + catalog_summary
    return base


HOUSE_GUIDANCE_RESPONSES = {
    "ru": (
        "🧴 План ухода: утро и вечер",
        "1. Очищение: мягкое средство утром и двухэтапное вечером.",
        "2. Тонизирование: лосьон без спирта для восстановления pH и гидратации.",
        "3. Активы: сыворотки по задачам — витамин C утром, ретинол или кислоты вечером.",
        "4. Увлажнение: крем или гель, отдельный уход для зоны вокруг глаз.",
        "5. Защита: утром SPF 30+, вечером питательная маска или масло 2–3 раза в неделю.",
        "Расскажите о типе кожи, ритме дня и предпочтениях — настрою программу точнее."
    ),
    "en": (
        "🧴 Daily beauty ritual:",
        "1. Cleanse: gentle gel in the morning, oil + foam double cleanse at night.",
        "2. Balance: alcohol-free toner or essence to restore pH and hydration.",
        "3. Actives: targeted serums — vitamin C in the morning, retinol or AHA/BHA at night.",
        "4. Moisturize: cream or gel plus dedicated eye care matching your concerns.",
        "5. Protect: SPF 30+ every morning; at night add a nourishing mask or oil 2–3 times a week.",
        "Share skin type, lifestyle, and scent/texture preferences for a tailored routine."
    ),
    "uz": (
        "🧴 Kunlik parvarish rejasi:",
        "1. Tozalash: ertalab yumshoq gel, kechqurun yog‘li va ko‘pikdan iborat ikki bosqich.",
        "2. Tonlash: spirtsiz tonik yoki essensiya pH va namlikni tiklaydi.",
        "3. Aktivlar: serumlar — ertalab vitamin C, kechqurun retinol yoki AHA/BHA.",
        "4. Namlanish: krem yoki gel, ko‘z atrofida alohida parvarish.",
        "5. Himoya: ertalab SPF 30+, kechqurun haftasiga 2–3 marta niqob yoki yog‘ qo‘shing.",
        "Teri turi, kun tartibi va tekstura xohishlarini yozing — rejani moslashtiraman."
    ),
    "kk": (
        "🧴 Күнделікті күтім жоспары:",
        "1. Таңертең жұмсақ гель, кешке май мен көбікпен екі кезеңді тазалау.",
        "2. Тонер: спиртсіз лосьон немесе эссенция pH мен ылғалды қалпына келтіреді.",
        "3. Активтер: мақсатты сарысулар — таңертең витамин C, кешке ретинол немесе AHA/BHA.",
        "4. Ылғалдандыру: крем немесе гель, көз айналасы үшін бөлек күтім.",
        "5. Қорғау: таңертең SPF 30+; кешке аптасына 2–3 рет қоректік маска немесе май қосыңыз.",
        "Тері түрі, өмір салты және текстура талғамы туралы жазыңыз — бағдарламаны нақтаймын."
    ),
}


def _build_house_guidance(text: str, lang: str) -> Optional[str]:
    normalized = _normalize_query(text)
    if not any(keyword in normalized for keyword in ROUTINE_KEYWORDS):
        return None
    return "\n".join(HOUSE_GUIDANCE_RESPONSES.get(lang, HOUSE_GUIDANCE_RESPONSES["ru"]))


def _build_care_response(text: str, lang: str, user_id: Optional[int] = None) -> str | dict | None:
    general_advice = _build_general_care_advice(text, lang)
    if general_advice:
        return general_advice
    house_guidance = _build_house_guidance(text, lang)
    if house_guidance:
        return house_guidance
    return None


async def show_typing(bot: Bot, chat_id: int, duration: int = 5):
    """Показывает статус 'печатает...' заданное количество секунд."""
    try:
        end_time = asyncio.get_event_loop().time() + duration
        while asyncio.get_event_loop().time() < end_time:
            await bot.send_chat_action(chat_id, types.ChatActions.TYPING)
            await asyncio.sleep(4)
    except Exception as error:
        logger.debug("show_typing error: %s", error)


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


def _expand_tokens(tokens: List[str]) -> List[str]:
    expanded: List[str] = []
    seen = set()
    for token in tokens:
        normalized = token.replace("ё", "е")
        if normalized not in seen:
            expanded.append(normalized)
            seen.add(normalized)
        for prefix, synonyms in SYNONYM_PREFIXES.items():
            if normalized.startswith(prefix):
                for synonym in synonyms:
                    synonym_norm = synonym.replace("ё", "е")
                    if synonym_norm not in seen:
                        expanded.append(synonym_norm)
                        seen.add(synonym_norm)
        if normalized in SYNONYM_EXACT:
            for synonym in SYNONYM_EXACT[normalized]:
                synonym_norm = synonym.replace("ё", "е")
                if synonym_norm not in seen:
                    expanded.append(synonym_norm)
                    seen.add(synonym_norm)
    return expanded


def tokenize(query: str) -> List[str]:
    base_tokens = [token for token in re.findall(r"[a-zа-я0-9]+", query.lower()) if len(token) > 2]
    if not base_tokens:
        return base_tokens

    expanded = _expand_tokens(base_tokens)
    normalized_tokens: List[str] = []
    seen: set[str] = set()
    for token in expanded:
        normalized = token.lower().replace("ё", "е")
        for candidate in (normalized, _normalize_category_token(normalized)):
            if not candidate:
                continue
            if candidate not in seen:
                normalized_tokens.append(candidate)
                seen.add(candidate)
    return normalized_tokens


def _token_variants(token: str) -> set[str]:
    variants = {token}
    if len(token) <= 3:
        return variants

    def add_variant(base: str):
        if len(base) >= 3:
            variants.add(base)

    endings = [
        ("ами", ""),
        ("ями", ""),
        ("ями", ""),
        ("ями", ""),
        ("ями", ""),
        ("ями", ""),
        ("ями", ""),
        ("ями", ""),
        ("ями", ""),
        ("ами", ""),
        ("ами", ""),
        ("ами", ""),
        ("ами", ""),
        ("ами", ""),
        ("ами", ""),
        ("ами", ""),
        ("ами", ""),
        ("ами", ""),
        ("ями", ""),
        ("ях", ""),
        ("ах", ""),
        ("ам", ""),
        ("ям", ""),
        ("ов", ""),
        ("ев", ""),
        ("ей", ""),
        ("ый", ""),
        ("ий", ""),
        ("ой", ""),
        ("ая", ""),
        ("яя", ""),
        ("ые", ""),
        ("ие", ""),
        ("ое", ""),
        ("ее", ""),
        ("ы", ""),
        ("и", ""),
        ("а", ""),
        ("я", ""),
        ("ю", ""),
    ]

    for suffix, _ in endings:
        if token.endswith(suffix) and len(token) > len(suffix) + 2:
            base = token[: -len(suffix)]
            add_variant(base)
            if suffix in {"ей", "ий", "ый"}:
                add_variant(base + "ь")

    if token.endswith("ей") and len(token) > 3:
        base = token[:-2]
        add_variant(base + "ь")

    if token.endswith("ь") and len(token) > 3:
        add_variant(token[:-1])

    return variants


def _token_matches(token: str, text: str) -> bool:
    for variant in _token_variants(token):
        if variant and variant in text:
            return True
    return False


def _filter_search_tokens(tokens: List[str], lang: str) -> List[str]:
    stopwords = TOKEN_SEARCH_STOPWORDS.get(lang, TOKEN_SEARCH_STOPWORDS["default"])
    return [token for token in tokens if len(token) > 2 and token not in stopwords]


def parse_price_limit(query: str) -> Optional[int]:
    lower = query.lower()
    match = re.search(r"(?:до|<=)\s*([\d\s.,]+)\s*(тыс|тысяч|млн|милл|миллион|uzs|сум|so'm|som)?", lower)
    if not match:
        match = re.search(r"([\d\s.,]+)\s*(тыс|тысяч|млн|милл|миллион|uzs|сум|so'm|som)\b", lower)
    if not match:
        return None

    raw = match.group(1)
    unit = (match.group(2) or "").strip()
    try:
        value = float(raw.replace(" ", "").replace(",", "."))
    except ValueError:
        return None

    if unit in {"тыс", "тысяч"}:
        value *= 1_000
    elif unit in {"млн", "милл", "миллион"}:
        value *= 1_000_000
    return int(value)


def load_active_products(force: bool = False) -> List[ProductLike]:
    """Возвращает товары из БД с кешированием, чтобы снизить нагрузку на запросы."""
    now = time.time()
    cached_items = PRODUCT_CACHE.get("items")
    loaded_at = PRODUCT_CACHE.get("loaded_at", 0.0)

    if PRODUCT_CACHE_TTL <= 0:
        force = True

    if not force and cached_items is not None and (now - loaded_at) < PRODUCT_CACHE_TTL:
        return cached_items

    try:
        with Session() as session:
            db_items = (
                session.query(DBProduct)
                .options(selectinload(DBProduct.category_obj))
                .order_by(DBProduct.name.asc())
                .all()
            )
    except Exception as error:
        logger.exception("Ошибка загрузки товаров из БД: %s", error)
        return cached_items or []

    items = [_to_product_record(item) for item in db_items]
    PRODUCT_CACHE["items"] = items
    PRODUCT_CACHE["loaded_at"] = now
    return items


def invalidate_products_cache():
    """Очищает кеш товаров (можно вызвать после административных изменений)."""
    PRODUCT_CACHE["items"] = None
    PRODUCT_CACHE["loaded_at"] = 0.0


def calculate_match_score(product: ProductLike, tokens: List[str]) -> int:
    name_text = (normalize_text(getattr(product, "name", "") or "") or "").lower()
    category_text = (normalize_text(getattr(product, "category", "") or "") or "").lower()
    description_text = (normalize_text(getattr(product, "description", "") or "") or "").lower()
    tags_text = (normalize_text(getattr(product, "tags", "") or "") or "").lower()
    base_text = " ".join(filter(None, [name_text, category_text, description_text, tags_text]))

    score = 0
    for token in tokens:
        matched = False
        if name_text and _token_matches(token, name_text):
            score += 4
            matched = True
        if description_text and _token_matches(token, description_text):
            score += 3
            matched = True
        if category_text and _token_matches(token, category_text):
            score += 2
            matched = True
        if tags_text and _token_matches(token, tags_text):
            score += 1
            matched = True
        if not matched and base_text and _token_matches(token, base_text):
            score += 1
    return score


def format_product_text(product: ProductLike, lang: str) -> str:
    category = normalize_text(getattr(product, "category", "") or "") or ""
    description = normalize_text(getattr(product, "description", "") or "") or ""
    if description and len(description) > 160:
        description = description[:157].rstrip() + "…"
    tags = (normalize_text(getattr(product, "tags", "") or "") or "").strip()

    if lang == "uz":
        category_line = f"Kategoriya: {category}" if category else ""
        desc_line = description or "Mahsulot tavsifi keyinroq qo'shiladi."
        tags_line = f"Teglar: {tags}" if tags else ""
        price_line = "Narx: menejer so'rov bo'yicha ma'lum qiladi."
    elif lang == "en":
        category_line = f"Category: {category}" if category else ""
        desc_line = description or "Description will be added later."
        tags_line = f"Tags: {tags}" if tags else ""
        price_line = "Price: please contact the manager for details."
    elif lang == "kk":
        category_line = f"Санат: {category}" if category else ""
        desc_line = description or "Сипаттама кейінірек қосылады."
        tags_line = f"Тегтер: {tags}" if tags else ""
        price_line = "Бағасы: менеджер сұраныс бойынша хабарлайды."
    else:  # ru
        category_line = f"Категория: {category}" if category else ""
        desc_line = description or "Описание появится позже."
        tags_line = f"Теги: {tags}" if tags else ""
        price_line = "Стоимость: уточняйте у менеджера."

    parts = [f"🔹 {normalize_text(getattr(product, 'name', 'Товар')) or 'Товар'}"]
    if category_line:
        parts.append(category_line)
    parts.append(price_line)
    parts.append(desc_line)
    if tags_line:
        parts.append(tags_line)
    return "\n".join(parts)


def build_summary_text(
    products: List[ProductLike],
    lang: str,
    price_limit: Optional[int],
    total_count: Optional[int] = None,
) -> str:
    return ""


def _serialize_product(product: ProductLike, lang: str, index_map: dict[int, int]) -> dict:
    entry = {"text": format_product_text(product, lang)}
    pictures = getattr(product, "picture", None) or []
    if pictures:
        photo_raw = normalize_text(pictures[0]) or pictures[0]
        entry["photo"] = normalize_url(photo_raw)
    else:
        entry["photo"] = PLACEHOLDER_PHOTO
    product_id = getattr(product, "id", None)
    entry["product_id"] = product_id if product_id is not None else id(product)
    if product_id is not None and product_id in index_map:
        entry["product_index"] = index_map[product_id]
    return entry


def _clean_product_text(value: Optional[str]) -> str:
    if not value:
        return ""
    cleaned = normalize_text(value) or value
    return cleaned.strip()


def _shorten_for_prompt(value: Optional[str], limit: int = 180) -> str:
    text = _clean_product_text(value)
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip(" .,;") + "…"


def _prepare_product_outline(products: List[ProductLike]) -> str:
    if not products:
        return ""
    lines: List[str] = []
    for idx, product in enumerate(products[:MAX_RECOMMENDATION_PRODUCTS], start=1):
        name = _clean_product_text(getattr(product, "name", None)) or f"Товар {idx}"
        category = _clean_product_text(getattr(product, "category", None))
        description = _shorten_for_prompt(getattr(product, "description", None))
        tags = _clean_product_text(getattr(product, "tags", None))
        parts = [f"{idx}. {name}"]
        if category:
            parts.append(f"Категория: {category}")
        if description:
            parts.append(f"Описание: {description}")
        if tags:
            parts.append(f"Теги: {tags}")
        lines.append("\n   ".join(parts))
    return "\n".join(lines)


def _generate_recommendation_message(
    query: str,
    lang: str,
    products: List[ProductLike],
    user_profile: Optional[dict] = None,
) -> Optional[str]:
    outline = _prepare_product_outline(products)
    if not outline:
        return None

    language_hint = RECOMMENDATION_LANGUAGE_HINTS.get(lang, RECOMMENDATION_LANGUAGE_HINTS["ru"])
    tone_hint = RECOMMENDATION_TONE_HINTS.get(lang, RECOMMENDATION_TONE_HINTS["ru"])

    profile_hint = _format_profile_hint(user_profile or {}, lang)

    system_prompt = (
        "Ты — персональный beauty-консультант бутика LuxeBeauty. "
        f"{tone_hint} {language_hint} "
        "Соблюдай деловой, но тёплый стиль общения. "
        "Отвечай без emoji и Markdown. Держи ответ максимально компактным.\n"
        "Структура ответа:\n"
        "1) Одно предложение, подтверждающее запрос клиента.\n"
        "2) Для каждой рекомендации (минимум 2, максимум 3) оформи блок из трёх строк:\n"
        "   N. Название — кратко опиши ключевой эффект или актив.\n"
        "   Как использовать: конкретные шаги применения (когда, в какой очередности, сколько). Добавь «Подходит: …», если можно описать тип кожи или задачу.\n"
        "   Предосторожности: что учесть (патч-тест, чувствительные зоны, сочетание с активами).\n"
        "3) Заверши отдельным предложением «Стоимость уточняйте у менеджера.» и вопросом, приглашающим рассказать о состоянии кожи, аллергиях или рекомендациях врача.\n"
        "Не придумывай новые товары и не указывай цену. Не давай медицинских диагнозов и не обещай излечения.\n"
        "Лимиты: до 6 предложений и до 3 блоков рекомендаций, каждая строка не длиннее 120 символов."
    )
    if profile_hint:
        system_prompt += f"\nКонтекст клиента: {profile_hint}"

    user_prompt = (
        f"Запрос клиента: {query.strip() or 'не указан'}\n\n"
        f"Доступные товары:\n{outline}\n\n"
        "Сформируй консультацию по указанной структуре."
    )

    try:
        content, provider = call_chat_with_fallback(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=420,
        )
        recommendation = (content or "").strip()
        if not recommendation:
            logger.debug("LLM provider %s вернул пустую рекомендацию", provider)
            return None
        return recommendation
    except Exception as error:
        logger.warning("Не удалось построить консультацию LLM: %s", error)
        return None


def _get_catalog_categories() -> List[str]:
    cached = PRODUCT_CACHE.get("items")
    if cached is None:
        cached = load_active_products()
    categories: set[str] = set()
    for product in cached or []:
        cat = getattr(product, "category", None)
        if cat:
            normalized = normalize_text(cat) or cat
            categories.add(normalized)
    return sorted(categories)


def _build_material_catalog_summary(lang: str) -> Optional[str]:
    categories = _get_catalog_categories()
    if not categories:
        return None

    header_map = {
        "ru": "🧾 Основные разделы каталога LuxeBeauty:",
        "uz": "🧾 LuxeBeauty katalogining asosiy bo‘limlari:",
        "en": "🧾 Core sections of the LuxeBeauty catalog:",
        "kk": "🧾 LuxeBeauty каталогының негізгі бөлімдері:",
    }
    footer_map = {
        "ru": "Напишите тип кожи, повод или бюджет — подберу средства и подарки.",
        "uz": "Teri turi, voqea yoki byudjetni yozing — mos vositalar va sovg‘alarni taklif qilaman.",
        "en": "Share skin type, occasion, or budget and I’ll suggest products and gift ideas.",
        "kk": "Тері түрі, жағдай немесе бюджет жайлы жазыңыз — тиісті өнімдер мен сыйлықтар ұсынамын.",
    }
    header = header_map.get(lang, header_map["ru"])
    footer = footer_map.get(lang, footer_map["ru"])
    top_categories = categories[:12]
    lines = [header]
    lines.extend(f"• {name}" for name in top_categories)
    lines.append(footer)
    return "\n".join(lines)


def search_products(
    query: str,
    lang: str,
    limit: int = PRODUCT_PAGE_SIZE,
    user_profile: Optional[dict] = None,
) -> Tuple[List[dict], List[ProductLike], Optional[int], List[dict], List[str]]:
    stopword_set = TOKEN_SEARCH_STOPWORDS.get(lang, TOKEN_SEARCH_STOPWORDS["default"])
    tokens = _filter_search_tokens(tokenize(query), lang)
    simple_tokens = _filter_search_tokens(_tokenize_simple(query), lang)
    simple_token_set = set(simple_tokens)
    normalized_simple_tokens = {_normalize_category_token(token) for token in simple_token_set}
    normalized_query = _normalize_query(query)
    price_limit = parse_price_limit(query)
    products = load_active_products()
    if not products:
        return [], [], price_limit, [], []

    keyword_candidates = simple_token_set
    keywords = INTENT_KEYWORDS.get(lang, set()) | INTENT_KEYWORDS.get("default", set())
    force_listing = bool(keyword_candidates & keywords)

    requested_areas = {
        token
        for token in normalized_simple_tokens
        if token in {"лицо", "губы", "волосы", "кожа", "глаза", "ногти"}
    }
    area_synonym_tokens: set[str] = set()
    missing_category_candidates: set[str] = set()
    query_token_pool = _expand_token_set(tokens) | _expand_token_set(simple_tokens)
    if not query_token_pool and normalized_simple_tokens:
        query_token_pool = set(normalized_simple_tokens)
    significant_query_tokens = {token for token in query_token_pool if token not in GENERIC_PRODUCT_TOKENS}

    index_map: dict[int, int] = {}
    category_map: dict[str, str] = {}
    category_tokens_map: dict[str, set[str]] = {}
    product_text_cache: dict[int, str] = {}
    product_name_cache: dict[int, str] = {}
    product_token_cache: dict[int, set[str]] = {}
    product_match_stat: dict[int, Tuple[int, int]] = {}
    for idx, product in enumerate(products, start=1):
        product_id = getattr(product, "id", None)
        if product_id is not None:
            index_map[product_id] = idx
        category = normalize_text(getattr(product, "category", "") or "") or ""
        if category:
            alias = category.lower().replace("ё", "е")
            category_map.setdefault(alias, category)
            alias_tokens = {
                _normalize_category_token(token)
                for token in re.findall(r"[a-zа-я0-9]+", alias)
            }
            if alias_tokens:
                category_tokens_map[alias] = alias_tokens

    profile_skin = (user_profile or {}).get("skin_type")
    skin_keyword_set = set()
    if profile_skin:
        skin_keyword_set = set(
            kw.lower()
            for kw in SKIN_TYPE_PRODUCT_HINTS.get(profile_skin, {}).get("keywords", set())
        )

    matches: List[Tuple[int, ProductLike]] = []
    for product in products:
        price = getattr(product, "price", None)
        if price_limit and price and price > price_limit:
            continue
        score = calculate_match_score(product, tokens)
        if not tokens and not price_limit:
            score = max(score, 1)
        product_key = getattr(product, "id", None) or id(product)
        base_fields = product_text_cache.get(product_key)
        normalized_name = product_name_cache.get(product_key)
        tokens_for_product = product_token_cache.get(product_key)
        if base_fields is None or normalized_name is None or tokens_for_product is None:
            normalized_name = (normalize_text(getattr(product, "name", "") or "") or "").lower()
            normalized_category = (normalize_text(getattr(product, "category", "") or "") or "").lower()
            normalized_description = (normalize_text(getattr(product, "description", "") or "") or "").lower()
            normalized_tags = (normalize_text(getattr(product, "tags", "") or "") or "").lower()
            base_fields = " ".join(
                filter(None, [normalized_name, normalized_category, normalized_description, normalized_tags])
            )
            product_text_cache[product_key] = base_fields
            product_name_cache[product_key] = normalized_name
            tokens_for_product = _expand_token_set(_tokenize_simple(base_fields))
            product_token_cache[product_key] = tokens_for_product
        else:
            tokens_for_product = product_token_cache[product_key]
        if query_token_pool:
            match_tokens = tokens_for_product & query_token_pool
            significant_matches = match_tokens & significant_query_tokens if significant_query_tokens else set()
            if not match_tokens:
                continue
            if significant_query_tokens and not significant_matches:
                continue
            product_match_stat[product_key] = (len(match_tokens), len(significant_matches))
        else:
            product_match_stat[product_key] = (0, 0)
        if profile_skin and skin_keyword_set:
            if any(keyword in base_fields for keyword in skin_keyword_set):
                score += 3
        if score > 0:
            matches.append((score, product))

    if not matches and requested_areas:
        base_area_tokens = {"лицо", "губы", "волосы", "кожа", "глаза", "ногти"}
        for area_token in requested_areas:
            anchors = AREA_KEYWORD_TABLE.get(area_token)
            if not anchors:
                continue
            expanded = _expand_token_set(list(anchors))
            banned = base_area_tokens - {area_token}
            filtered = {token for token in expanded if token not in banned}
            area_synonym_tokens.update(filtered)
        if area_synonym_tokens:
            for product in products:
                product_key = getattr(product, "id", None) or id(product)
                tokens_for_product = product_token_cache.get(product_key)
                base_fields = product_text_cache.get(product_key)
                if tokens_for_product is None:
                    if base_fields is None:
                        normalized_name = (normalize_text(getattr(product, "name", "") or "") or "").lower()
                        normalized_category = (normalize_text(getattr(product, "category", "") or "") or "").lower()
                        normalized_description = (normalize_text(getattr(product, "description", "") or "") or "").lower()
                        normalized_tags = (normalize_text(getattr(product, "tags", "") or "") or "").lower()
                        base_fields = " ".join(
                            filter(None, [normalized_name, normalized_category, normalized_description, normalized_tags])
                        )
                        product_text_cache[product_key] = base_fields
                        product_name_cache.setdefault(product_key, normalized_name)
                    else:
                        normalized_name = product_name_cache.get(product_key)
                        if normalized_name is None:
                            normalized_name = (normalize_text(getattr(product, "name", "") or "") or "").lower()
                            product_name_cache[product_key] = normalized_name
                    tokens_for_product = _expand_token_set(_tokenize_simple(base_fields or ""))
                    product_token_cache[product_key] = tokens_for_product
                if not tokens_for_product:
                    continue
                match_tokens = tokens_for_product & area_synonym_tokens
                if not match_tokens:
                    continue
                base_score = max(len(match_tokens), 1)
                matches.append((base_score + 1, product))
                product_match_stat[product_key] = (len(match_tokens), 0)

    if not matches and price_limit:
        for product in products:
            price = getattr(product, "price", None)
            if price and price <= price_limit * 1.15:
                product_key = getattr(product, "id", None) or id(product)
                tokens_for_product = product_token_cache.get(product_key) or set()
                if query_token_pool:
                    match_tokens = tokens_for_product & query_token_pool
                    significant_matches = match_tokens & significant_query_tokens if significant_query_tokens else set()
                    if not match_tokens:
                        continue
                    if significant_query_tokens and not significant_matches:
                        continue
                    product_match_stat[product_key] = (len(match_tokens), len(significant_matches))
                else:
                    product_match_stat[product_key] = (0, 0)
                matches.append((1, product))

    if not matches and tokens:
        # попытка по частичному совпадению
        for product in products:
            product_key = getattr(product, "id", None) or id(product)
            name = product_name_cache.get(product_key)
            if name is None:
                name = (normalize_text(getattr(product, "name", "") or "") or "").lower()
                product_name_cache[product_key] = name
            if any(token in name for token in tokens):
                tokens_for_product = product_token_cache.get(product_key) or set()
                if query_token_pool:
                    match_tokens = tokens_for_product & query_token_pool
                    significant_matches = match_tokens & significant_query_tokens if significant_query_tokens else set()
                    if not match_tokens:
                        continue
                    if significant_query_tokens and not significant_matches:
                        continue
                    product_match_stat[product_key] = (len(match_tokens), len(significant_matches))
                else:
                    product_match_stat[product_key] = (0, 0)
                matches.append((1, product))

    if not matches:
        raw_tokens = [
            token
            for token in _tokenize_simple(query)
            if len(token) > 2 and token not in stopword_set
        ]
        fallback_matches: List[Tuple[int, ProductLike]] = []
        if raw_tokens:
            for product in products:
                product_key = getattr(product, "id", None) or id(product)
                base_fields = product_text_cache.get(product_key)
                if base_fields is None:
                    normalized_name = (normalize_text(getattr(product, "name", "") or "") or "").lower()
                    normalized_category = (normalize_text(getattr(product, "category", "") or "") or "").lower()
                    normalized_description = (normalize_text(getattr(product, "description", "") or "") or "").lower()
                    normalized_tags = (normalize_text(getattr(product, "tags", "") or "") or "").lower()
                    base_fields = " ".join(
                        filter(None, [normalized_name, normalized_category, normalized_description, normalized_tags])
                    )
                    product_text_cache[product_key] = base_fields
                    product_name_cache.setdefault(product_key, normalized_name)
                tokens_for_product = product_token_cache.get(product_key)
                if tokens_for_product is None:
                    tokens_for_product = _expand_token_set(_tokenize_simple(base_fields))
                    product_token_cache[product_key] = tokens_for_product
                hits = sum(1 for token in raw_tokens if token in base_fields)
                if hits:
                    if query_token_pool:
                        match_tokens = tokens_for_product & query_token_pool
                        significant_matches = match_tokens & significant_query_tokens if significant_query_tokens else set()
                        if not match_tokens:
                            continue
                        if significant_query_tokens and not significant_matches:
                            continue
                        product_match_stat[product_key] = (len(match_tokens), len(significant_matches))
                    else:
                        product_match_stat[product_key] = (0, 0)
                    fallback_matches.append((hits + 1, product))
        if fallback_matches:
            matches = fallback_matches

    if matches:
        query_semantic = _normalize_query(query)
        boosted_matches: List[Tuple[int, ProductLike]] = []
        for score, product in matches:
            product_key = getattr(product, "id", None) or id(product)
            base_fields = product_text_cache.get(product_key)
            if base_fields is None:
                normalized_name = (normalize_text(getattr(product, "name", "") or "") or "").lower()
                normalized_category = (normalize_text(getattr(product, "category", "") or "") or "").lower()
                normalized_description = (normalize_text(getattr(product, "description", "") or "") or "").lower()
                normalized_tags = (normalize_text(getattr(product, "tags", "") or "") or "").lower()
                base_fields = " ".join(
                    filter(None, [normalized_name, normalized_category, normalized_description, normalized_tags])
                )
                product_text_cache[product_key] = base_fields
                product_name_cache.setdefault(product_key, normalized_name)
            semantic_score = _semantic_similarity_score(query_semantic, base_fields)
            match_count, significant_count = product_match_stat.get(product_key, (0, 0))
            boosted_score = (
                score
                + int(round(semantic_score * 8))
                + significant_count * 3
                + max(0, match_count - significant_count)
            )
            boosted_matches.append((boosted_score, product))
        matches = boosted_matches

    if requested_areas and matches:
        area_filtered: List[Tuple[int, ProductLike]] = []
        filter_tokens = set(requested_areas)
        if area_synonym_tokens:
            filter_tokens.update(area_synonym_tokens)
        for score, product in matches:
            product_key = getattr(product, "id", None) or id(product)
            tokens_for_product = product_token_cache.get(product_key) or set()
            if tokens_for_product & filter_tokens:
                area_filtered.append((score, product))
        if area_filtered:
            matches = area_filtered
        else:
            missing_category_candidates.update(requested_areas)

    if requested_areas and matches:
        # Отдаём приоритет товарам, где нужная зона фигурирует в названии, тегах или категории.
        area_variants: set[str] = set()
        for token in requested_areas:
            area_variants.add(token)
            area_variants.update(_expand_token_set([token]))
            anchors = AREA_KEYWORD_TABLE.get(token)
            if anchors:
                area_variants.update(_expand_token_set(list(anchors)))

        strong_matches: List[Tuple[int, ProductLike]] = []
        fallback_matches: List[Tuple[int, ProductLike]] = []
        for score, product in matches:
            product_key = getattr(product, "id", None) or id(product)
            name_text = product_name_cache.get(product_key)
            if name_text is None:
                name_text = (normalize_text(getattr(product, "name", "") or "") or "").lower()
                product_name_cache[product_key] = name_text
            category_text = (normalize_text(getattr(product, "category", "") or "") or "").lower()
            tags_text = (normalize_text(getattr(product, "tags", "") or "") or "").lower()

            field_values = [name_text, category_text, tags_text]
            strong = False
            for variant in area_variants:
                if not variant:
                    continue
                if any(field and _token_matches(variant, field) for field in field_values):
                    strong = True
                    break

            if strong:
                strong_matches.append((score, product))
            else:
                fallback_matches.append((score, product))

        if strong_matches:
            matches = strong_matches
        elif fallback_matches:
            matches = fallback_matches

    mentioned_categories: set[str] = set()
    for alias_norm, original_name in category_map.items():
        if not alias_norm:
            continue
        alias_tokens = category_tokens_map.get(alias_norm, set())
        if alias_tokens and alias_tokens & normalized_simple_tokens:
            mentioned_categories.add(original_name)
            continue
        if alias_norm in normalized_query:
            mentioned_categories.add(original_name)
            continue
        parts = alias_norm.split()
        if parts and any(part in simple_token_set for part in parts):
            mentioned_categories.add(original_name)
            continue
        if parts and any(_normalize_category_token(part) in normalized_simple_tokens for part in parts):
            mentioned_categories.add(original_name)

    if requested_areas:
        mentioned_categories.update(requested_areas)

    category_filtered: List[Tuple[int, ProductLike]] = []
    if mentioned_categories:
        mentioned_aliases = {
            (normalize_text(name) or name).lower().replace("ё", "е")
            for name in mentioned_categories
        }
        for score, product in matches:
            product_category = normalize_text(getattr(product, "category", "") or "") or ""
            product_alias = product_category.lower().replace("ё", "е")
            if product_alias in mentioned_aliases:
                category_filtered.append((score + CATEGORY_MATCH_BOOST, product))
        if category_filtered:
            matches = category_filtered
        else:
            missing_category_candidates.update(mentioned_categories)

    missing_hint = sorted(missing_category_candidates) or sorted(mentioned_categories) or sorted(requested_areas)

    if not matches:
        if force_listing and products:
            matches = [(1, product) for product in products]
        else:
            return [], [], price_limit, [], missing_hint

    def sort_key(pair: Tuple[int, ProductLike]):
        score, product = pair
        price = getattr(product, "price", None)
        sort_price = price if price is not None else float("inf")
        return (-score, sort_price)

    ordered: List[ProductLike] = []
    seen_ids = set()
    for score, product in sorted(matches, key=sort_key):
        product_id = getattr(product, "id", None) or id(product)
        if product_id in seen_ids:
            continue
        seen_ids.add(product_id)
        ordered.append(product)

    if not ordered:
        ordered = products[:]

    first_page_products = ordered[:limit]
    payload = [_serialize_product(product, lang, index_map) for product in first_page_products]
    full_payload = [_serialize_product(product, lang, index_map) for product in ordered]

    missing_hint = sorted(missing_category_candidates)
    return payload, first_page_products, price_limit, full_payload, missing_hint


def _reset_product_session(user_id: int):
    user_product_sessions.pop(user_id, None)


def _store_product_session(
    user_id: int,
    query: str,
    lang: str,
    full_payload: List[dict],
    price_limit: Optional[int],
    user_profile: Optional[dict] = None,
):
    user_product_sessions[user_id] = {
        "query": query,
        "lang": lang,
        "products": full_payload,
        "page": 1,
        "page_size": PRODUCT_PAGE_SIZE,
        "price_limit": price_limit,
        "total": len(full_payload),
        "profile": user_profile,
    }


def _rehydrate_session_products(session: dict, lang: str, page_size: int) -> Tuple[List[dict], bool]:
    """
    Переинициализирует список товаров для сессии.
    Возвращает пары (products, used_catalog_fallback).
    """
    query = session.get("query")
    used_catalog_fallback = False

    if query:
        _, _, price_limit, full_payload, _ = search_products(
            query,
            lang,
            limit=page_size,
            user_profile=session.get("profile"),
        )
        if full_payload:
            session["products"] = full_payload
            session["page"] = 0
            session["total"] = len(full_payload)
            session["page_size"] = page_size
            session["lang"] = lang
            if price_limit is not None:
                session["price_limit"] = price_limit
            return full_payload, used_catalog_fallback

    catalog = load_active_products()
    if catalog:
        index_map: dict[int, int] = {}
        for idx, product in enumerate(catalog, start=1):
            product_id = getattr(product, "id", None)
            if product_id is not None:
                index_map[product_id] = idx
        fallback_entries = [_serialize_product(product, lang, index_map) for product in catalog]
        session["products"] = fallback_entries
        session["page"] = 0
        session["total"] = len(fallback_entries)
        session["page_size"] = page_size
        session["lang"] = lang
        used_catalog_fallback = True
        return fallback_entries, used_catalog_fallback

    return [], used_catalog_fallback


def _extract_titles(entries: List[dict]) -> List[str]:
    titles: List[str] = []
    for entry in entries:
        text = (entry or {}).get("text") or ""
        if not text:
            continue
        first_line = text.strip().splitlines()[0].strip()
        if first_line.startswith("🔹"):
            first_line = first_line.lstrip("🔹").strip()
        titles.append(first_line)
    return titles


def _build_continuation_text(lang: str, shown: int, total: int, new: int, previous_titles: List[str]) -> str:
    template = CONTINUATION_TEMPLATES.get(lang, CONTINUATION_TEMPLATES["ru"])
    message = template.format(shown=shown, total=total, new=new)
    if previous_titles:
        label = PREVIOUS_LABELS.get(lang, PREVIOUS_LABELS["ru"])
        lines = [message, "", label]
        for idx, title in enumerate(previous_titles, start=1):
            lines.append(f"{idx}. {title}")
        return "\n".join(lines)
    return message


def _fetch_additional_session_products(session: dict, lang: str) -> List[dict]:
    """Подгружает дополнительные товары из общего каталога, избегая повторов."""
    catalog = load_active_products()
    if not catalog:
        return []

    seen_ids = {
        (entry or {}).get("product_id")
        for entry in session.get("products") or []
        if (entry or {}).get("product_id") is not None
    }

    index_map: dict[int, int] = {}
    for idx, product in enumerate(catalog, start=1):
        product_id = getattr(product, "id", None)
        if product_id is not None:
            index_map[product_id] = idx

    extra_entries: List[dict] = []
    for product in catalog:
        product_id = getattr(product, "id", None)
        if product_id is not None and product_id in seen_ids:
            continue
        extra_entries.append(_serialize_product(product, lang, index_map))
    return extra_entries


def _continue_product_session(user_id: int, lang: str) -> Optional[dict]:
    session = user_product_sessions.get(user_id)
    if not session:
        return None

    page_size = session.get("page_size", PRODUCT_PAGE_SIZE)
    active_lang = session.get("lang") or lang or "ru"
    if active_lang not in SUPPORTED_LANGS:
        active_lang = "ru"

    products = session.get("products") or []
    total = len(products)

    fallback_used = False
    restarted = False

    if total == 0:
        products, used_catalog = _rehydrate_session_products(session, active_lang, page_size)
        total = len(products)
        if total == 0:
            message = NO_MORE_RESULTS_MESSAGES.get(active_lang, NO_MORE_RESULTS_MESSAGES["ru"])
            meta = {
                "continuation": True,
                "display_text": True,
                "page": session.get("page", 0),
                "total": total,
                "shown": total,
                "new_count": 0,
                "restarted": True,
            }
            if used_catalog:
                meta["fallback"] = True
            return {
                "text": message,
                "products": [],
                "meta": meta,
            }
        fallback_used = used_catalog
        restarted = True
        session["page"] = 0
        session["total"] = total

    page = session.get("page", 1)

    if page * page_size >= total:
        extra_entries = _fetch_additional_session_products(session, active_lang)
        if extra_entries:
            products.extend(extra_entries)
            session["products"] = products
            total = len(products)
            fallback_used = True
        else:
            if not products:
                message = NO_MORE_RESULTS_MESSAGES.get(active_lang, NO_MORE_RESULTS_MESSAGES["ru"])
                return {
                    "text": message,
                    "products": [],
                    "meta": {
                        "continuation": True,
                        "display_text": True,
                        "page": page,
                        "total": total,
                        "shown": total,
                        "new_count": 0,
                    },
                }
            session["page"] = 0
            page = 0
            restarted = True

    start = page * page_size
    end = min(total, (page + 1) * page_size)
    new_entries = products[start:end]
    previous_entries = [] if restarted else products[:start]
    previous_titles = _extract_titles(previous_entries)

    session["page"] = page + 1
    session["total"] = total

    summary_text = _build_continuation_text(active_lang, shown=end, total=total, new=len(new_entries), previous_titles=previous_titles)

    meta = {
        "continuation": True,
        "display_text": True,
        "page": session["page"],
        "total": total,
        "shown": end,
        "new_count": len(new_entries),
        "previous_count": len(previous_entries),
        "previous_titles": previous_titles,
    }
    if fallback_used:
        meta["fallback"] = True
    if restarted:
        meta["restarted"] = True

    return {
        "text": summary_text,
        "products": new_entries,
        "meta": meta,
    }


async def ask_openai_sync(user_id: int, text: str, bot: Bot = None, chat_id: int = None):
    text = (text or "").strip()
    if not text:
        return {"text": "❗ Пустой запрос. Расскажите, какое средство или задачу хотите разобрать."}

    if bot and chat_id:
        asyncio.create_task(show_typing(bot, chat_id, duration=5))

    lang = await detect_language(text)
    if lang not in SUPPORTED_LANGS:
        lang = "ru"
    health_guidance: Optional[str] = None
    if _is_health_query(text, lang):
        health_guidance = _build_health_response(text, lang)
    profile_updates = _extract_user_profile(text, lang)
    if profile_updates:
        _remember_user_profile(user_id, lang, profile_updates)
    user_profile = _get_user_profile(user_id)
    profile_hint_text = _format_profile_hint(user_profile, lang) if user_profile else None
    normalized_query = _normalize_query(text)
    simple_tokens = _tokenize_simple(text)

    if _is_availability_followup(normalized_query, simple_tokens, lang):
        followup_payload = _handle_availability_followup(user_id, lang)
        if followup_payload:
            return followup_payload

    if _is_low_information_query(text):
        _reset_product_session(user_id)
        message = _build_low_info_response(lang)
        combined = _combine_blocks(message, health_guidance)
        return {
            "text": combined or message,
            "products": [],
            "meta": {"display_text": True, "continuation": False, "low_info": True},
        }

    show_more_requested = _is_show_more_request(text, lang)
    if show_more_requested:
        continuation = _continue_product_session(user_id, lang)
        if continuation:
            return continuation
        message = NO_PREVIOUS_RESULTS_MESSAGES.get(lang, NO_PREVIOUS_RESULTS_MESSAGES["ru"])
        return {
            "text": message,
            "products": [],
            "meta": {"continuation": False, "display_text": True},
        }

    intent = _classify_intent(text, lang)

    if intent == "greeting":
        _reset_product_session(user_id)
        greeting_text = _build_greeting_response(lang)
        combined = _combine_blocks(greeting_text, health_guidance)
        return {
            "text": combined or greeting_text,
            "products": [],
            "meta": {"display_text": True, "continuation": False, "greeting": True},
        }

    if intent == "informational":
        _reset_product_session(user_id)
        info_text = _build_informational_answer(text, lang)
        _remember_user_topic(user_id, lang, text)

        products_payload, matched_products, price_limit, full_payload, mentioned_categories = search_products(
            text,
            lang,
            user_profile=user_profile,
        )
        if products_payload:
            _store_product_session(user_id, text, lang, full_payload, price_limit, user_profile)
            summary = build_summary_text(matched_products, lang, price_limit, total_count=len(full_payload))
            combined_text = _combine_blocks(profile_hint_text, info_text, health_guidance, summary)
            meta = {
                "display_text": True,
                "continuation": False,
                "informational": True,
                "page": 1,
                "total": len(full_payload),
            }
            if health_guidance:
                meta["health"] = True
            return {
                "text": combined_text,
                "products": products_payload,
                "meta": meta,
            }

        if mentioned_categories:
            unavailable_text = _build_category_unavailable_message(mentioned_categories, lang)
            combined_text = _combine_blocks(profile_hint_text, info_text, health_guidance, unavailable_text)
            meta = {"display_text": True, "continuation": False, "informational": True, "category_unavailable": True}
            if health_guidance:
                meta["health"] = True
            return {
                "text": combined_text,
                "products": [],
                "meta": meta,
            }

        return {
            "text": _combine_blocks(profile_hint_text, info_text, health_guidance) or info_text,
            "products": [],
            "meta": {"display_text": True, "continuation": False, "informational": True, **({"health": True} if health_guidance else {})},
        }

    care_payload = _build_care_response(text, lang, user_id=user_id)
    skip_products = False
    if isinstance(care_payload, dict):
        care_response = care_payload.get("text")
        skip_products = care_payload.get("skip_products", False)
    else:
        care_response = care_payload

    if skip_products:
        products_payload = []
        matched_products = []
        price_limit = None
        full_payload = []
        mentioned_categories = []
    else:
        products_payload, matched_products, price_limit, full_payload, mentioned_categories = search_products(
            text,
            lang,
            user_profile=user_profile,
        )

    analog_search_result = None

    if care_response:
        meta = {"display_text": True, "continuation": False, "care": True}
        text_blocks: List[str] = []
        if health_guidance:
            text_blocks.append(health_guidance)
            meta["health"] = True
        if profile_hint_text:
            text_blocks.append(profile_hint_text)
        if care_response:
            text_blocks.append(care_response)

        if skip_products:
            meta["informational"] = True
            meta["skip_products"] = True
            response_text = _combine_blocks(*text_blocks) or care_response or ""
            return {
                "text": response_text,
                "products": [],
                "meta": meta,
            }

        recommendation_text: Optional[str] = None
        summary_text: Optional[str] = None

        if products_payload:
            _store_product_session(user_id, text, lang, full_payload, price_limit, user_profile)
            _remember_user_topic(user_id, lang, text)
            meta.update({"page": 1, "total": len(full_payload)})
            recommendation_text = _generate_recommendation_message(text, lang, matched_products, user_profile)
            if recommendation_text:
                text_blocks.append(recommendation_text)
                meta["recommendation"] = True
            summary_text = build_summary_text(matched_products, lang, price_limit, total_count=len(full_payload))
            if summary_text:
                text_blocks.append(summary_text)
        else:
            _reset_product_session(user_id)
            if mentioned_categories:
                text_blocks.append(_build_category_unavailable_message(mentioned_categories, lang))

        response_text = _combine_blocks(*text_blocks) or care_response or ""
        return {
            "text": response_text,
            "products": products_payload,
            "meta": meta,
        }

    if products_payload:
        _store_product_session(user_id, text, lang, full_payload, price_limit, user_profile)
        _remember_user_topic(user_id, lang, text)
        recommendation_text = _generate_recommendation_message(text, lang, matched_products, user_profile)
        summary = build_summary_text(matched_products, lang, price_limit, total_count=len(full_payload))
        text_blocks = []
        if health_guidance:
            text_blocks.append(health_guidance)
        if profile_hint_text:
            text_blocks.append(profile_hint_text)
        if recommendation_text:
            text_blocks.append(recommendation_text)
        if summary:
            text_blocks.append(summary)
        combined_text = _combine_blocks(*text_blocks) or summary or recommendation_text or ""
        meta = {
            "page": 1,
            "total": len(full_payload),
            "display_text": True,
            "continuation": False,
        }
        if health_guidance:
            meta["health"] = True
        if recommendation_text:
            meta["recommendation"] = True
        return {
            "text": combined_text,
            "products": products_payload,
            "meta": meta,
        }

    analog_queries = _collect_analog_queries(text, lang)
    for alt_query in analog_queries:
        analog_payload, analog_matches, analog_price_limit, analog_full_payload, analog_categories = search_products(
            alt_query,
            lang,
            user_profile=user_profile,
        )
        if analog_payload:
            _store_product_session(user_id, alt_query, lang, analog_full_payload, analog_price_limit, user_profile)
            _remember_user_topic(user_id, lang, alt_query)
            intro_text = _build_analog_intro(lang, alt_query)
            summary_text = build_summary_text(analog_matches, lang, analog_price_limit, total_count=len(analog_full_payload))
            recommendation_text = _generate_recommendation_message(alt_query, lang, analog_matches, user_profile)
            text_blocks = []
            if health_guidance:
                text_blocks.append(health_guidance)
            if profile_hint_text:
                text_blocks.append(profile_hint_text)
            text_blocks.append(intro_text)
            if recommendation_text:
                text_blocks.append(recommendation_text)
            if summary_text:
                text_blocks.append(summary_text)
            combined_text = _combine_blocks(*text_blocks) or intro_text
            meta = {
                "page": 1,
                "total": len(analog_full_payload),
                "display_text": True,
                "continuation": False,
                "analog": True,
            }
            if health_guidance:
                meta["health"] = True
            if recommendation_text:
                meta["recommendation"] = True
            return {
                "text": combined_text,
                "products": analog_payload,
                "meta": meta,
            }
        if analog_categories:
            analog_search_result = analog_categories

    _reset_product_session(user_id)

    if any(keyword in normalized_query for keyword in CATALOG_KEYWORDS):
        catalog_summary = _build_material_catalog_summary(lang)
        if catalog_summary:
            combined_text = _combine_blocks(profile_hint_text, health_guidance, catalog_summary) or catalog_summary
            meta = {"display_text": True, "continuation": False, "catalog_overview": True}
            if health_guidance:
                meta["health"] = True
            return {
                "text": combined_text,
                "products": [],
                "meta": meta,
            }

    if mentioned_categories:
        unavailable_text = _build_category_unavailable_message(mentioned_categories, lang)
        combined_text = _combine_blocks(profile_hint_text, health_guidance, unavailable_text) or unavailable_text
        meta = {"display_text": True, "continuation": False, "category_unavailable": True}
        if health_guidance:
            meta["health"] = True
        return {
            "text": combined_text,
            "products": [],
            "meta": meta,
        }
    if analog_search_result:
        unavailable_text = _build_category_unavailable_message(analog_search_result, lang)
        combined_text = _combine_blocks(profile_hint_text, health_guidance, unavailable_text) or unavailable_text
        meta = {"display_text": True, "continuation": False, "category_unavailable": True, "analog": True}
        if health_guidance:
            meta["health"] = True
        return {
            "text": combined_text,
            "products": [],
            "meta": meta,
        }

    no_match_message = _build_no_match_response(lang, text)
    combined_text = _combine_blocks(profile_hint_text, health_guidance, no_match_message) or no_match_message
    meta = {"display_text": True, "continuation": False, "no_match": True}
    if health_guidance:
        meta["health"] = True
    return {
        "text": combined_text,
        "products": [],
        "meta": meta,
    }

GENERAL_CARE_KEYWORDS = {
    "ru": ("совет", "уход", "подскажи", "космет", "макияж"),
    "en": ("advice", "tips", "skincare", "beauty", "makeup"),
    "uz": ("maslahat", "parvarish", "teri", "kosmetika"),
    "kk": ("кеңес", "күтім", "тері", "beauty"),
}
CATALOG_KEYWORDS = {"космет", "beauty", "уход", "макияж", "парфюм", "skin", "hair", "spf", "аромат"}
ROUTINE_KEYWORDS = ("рутин", "routine", "уход", "режим", "programma", "ritual", "ритуал", "режим ухода")
