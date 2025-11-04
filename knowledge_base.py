import re
from typing import Dict, List, Optional, Set


LANG_LABELS: Dict[str, Dict[str, str]] = {
    "ru": {
        "pros": "Плюсы:",
        "cons": "Минусы:",
        "tips": "Советы:",
        "single_tip": "Совет: {}",
        "bullet": "• {}",
    },
    "en": {
        "pros": "Pros:",
        "cons": "Cons:",
        "tips": "Tips:",
        "single_tip": "Tip: {}",
        "bullet": "- {}",
    },
    "uz": {
        "pros": "Afzalliklar:",
        "cons": "Kamchiliklar:",
        "tips": "Maslahatlar:",
        "single_tip": "Maslahat: {}",
        "bullet": "- {}",
    },
    "kk": {
        "pros": "Артықшылықтары:",
        "cons": "Кемшіліктері:",
        "tips": "Кеңестер:",
        "single_tip": "Кеңес: {}",
        "bullet": "- {}",
    },
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower()).replace("ё", "е")


def _tokenize_normalized(normalized_text: str) -> List[str]:
    return [token for token in re.findall(r"[a-zа-я0-9]+", normalized_text) if token]


def _get_labels(lang: str) -> Dict[str, str]:
    return LANG_LABELS.get(lang, LANG_LABELS["ru"])


def _format_entry(entry: dict, lang: str) -> Optional[str]:
    MAX_SECTION_ITEMS = 3
    content_map: Dict[str, Dict[str, List[str] | str]] = entry.get("content", {})
    content = content_map.get(lang) or content_map.get("ru")
    if not content:
        return None

    labels = _get_labels(lang)
    bullet = labels["bullet"]

    title = content.get("title")
    description = content.get("description")
    pros = content.get("pros", [])
    cons = content.get("cons", [])
    tips = content.get("tips", [])
    closing = content.get("closing")

    lines: List[str] = []
    if title:
        lines.append(title)
    if description:
        lines.append(description)

    if pros:
        lines.append("")
        lines.append(labels["pros"])
        for item in pros[:MAX_SECTION_ITEMS]:
            lines.append(bullet.format(item))
        if len(pros) > MAX_SECTION_ITEMS:
            lines.append(bullet.format("…"))

    if cons:
        lines.append("")
        lines.append(labels["cons"])
        for item in cons[:MAX_SECTION_ITEMS]:
            lines.append(bullet.format(item))
        if len(cons) > MAX_SECTION_ITEMS:
            lines.append(bullet.format("…"))

    if tips:
        lines.append("")
        if len(tips) == 1:
            lines.append(labels["single_tip"].format(tips[0]))
        else:
            lines.append(labels["tips"])
            for tip in tips[:MAX_SECTION_ITEMS]:
                lines.append(bullet.format(tip))
            if len(tips) > MAX_SECTION_ITEMS:
                lines.append(bullet.format("…"))

    if closing:
        lines.append("")
        lines.append(closing)

    return "\n".join(lines).strip()


MATERIAL_GUIDE: List[dict] = [
    {
        "id": "skincare_routine",
        "keywords": {"уход", "кожа", "skincare", "крем", "сыворотка"},
        "phrases": {"уход за кожей", "крем для лица", "сыворотка"},
        "content": {
            "ru": {
                "title": "Уход за кожей — основа сияния",
                "description": "Система очищения, тонизирования и увлажнения, которая поддерживает здоровье и гладкость кожи.",
                "pros": [
                    "Даёт долгосрочный эффект — кожа становится ровной и упругой",
                    "Можно адаптировать под любой тип кожи и сезон",
                    "Подготавливает лицо к макияжу и усиливает эффективность активов",
                ],
                "cons": [
                    "Требует регулярности утром и вечером",
                    "Некоторые активы могут вызывать чувствительность при неправильном сочетании",
                ],
                "tips": [
                    "Очищайте кожу дважды в день мягким средством без сульфатов",
                    "Наносите SPF каждое утро, даже в пасмурную погоду",
                    "Вводите кислоты и ретинол постепенно, отслеживая реакцию кожи",
                ],
                "closing": "Опишите тип и состояние кожи — подберу персональную схему ухода.",
            },
            "uz": {
                "title": "Teri parvarishi — yaltirash asosi",
                "description": "Tozalash, tonlash va namlash tizimi terini sog‘lom va silliq saqlaydi.",
                "pros": [
                    "Uzoq muddatli natija — teri tekis va tarang bo‘ladi",
                    "Har qanday teri turi va mavsumga moslashtirish mumkin",
                    "Makiya uchun bazani tayyorlaydi va faol moddalarning samaradorligini oshiradi",
                ],
                "cons": [
                    "Tong va kechqurun muntazam vaqt ajratishni talab qiladi",
                    "Noto‘g‘ri kombinatsiyada ayrim faol moddalari sezgirlik keltirib chiqarishi mumkin",
                ],
                "tips": [
                    "Terini kuniga ikki marta sulfatsiz yumshoq vosita bilan tozalang",
                    "Bulutli havoda ham ertalab SPF qo‘llang",
                    "Kislotalar va retinolni asta-sekin kiriting, teri reaksiyasini kuzating",
                ],
                "closing": "Teri turi va holatini yozing — mos parvarish sxemasini tavsiya qilaman.",
            },
            "en": {
                "title": "Skincare routine — glow from the basics",
                "description": "A cleanse-tone-hydrate system that keeps skin balanced, smooth, and resilient.",
                "pros": [
                    "Delivers lasting results with improved texture and firmness",
                    "Easy to adapt to any skin type, season, or concern",
                    "Preps skin for makeup and boosts active ingredients",
                ],
                "cons": [
                    "Needs consistency twice a day",
                    "Active ingredients may trigger sensitivity if layered incorrectly",
                ],
                "tips": [
                    "Cleanse morning and night with a gentle, sulfate-free formula",
                    "Apply SPF every morning regardless of the weather",
                    "Introduce acids or retinol slowly and monitor your skin response",
                ],
                "closing": "Share your skin type and concerns — I’ll build a tailored routine.",
            },
        },
    },
    {
        "id": "makeup_essentials",
        "keywords": {"макияж", "makeup", "тональный", "помада", "румяна", "тен"},
        "phrases": {"подбор макияжа", "тональный крем", "палетка теней"},
        "content": {
            "ru": {
                "title": "Макияж — выразительный акцент",
                "description": "Комбинация тона, глаз и губ создаёт стиль от nude до вечернего.",
                "pros": [
                    "Позволяет подчеркнуть черты и скорректировать рельеф",
                    "Большой выбор текстур: от матовых до сияющих",
                    "Совмещается с уходом — есть формулы с SPF и активами",
                ],
                "cons": [
                    "Нужна подготовка кожи, чтобы макияж лежал ровно",
                    "Важно подобрать оттенки под подтон кожи и освещение",
                ],
                "tips": [
                    "Используйте праймер или увлажняющий крем перед тональной основой",
                    "Комплектуйте палетку по правилу: базовый тон, акцент и хайлайтер",
                    "Для стойкости фиксируйте макияж тонкой вуалью пудры или спреем",
                ],
                "closing": "Расскажите о событии и желаемом эффекте — соберу набор макияжа.",
            },
            "uz": {
                "title": "Makiyaj — obrazdagi aksent",
                "description": "Ton, ko‘z va lab kombinatsiyasi kundalikdan kechki ko‘rinishgacha yaratadi.",
                "pros": [
                    "Yuz chiziklarini ta’kidlaydi va relyefni silliqlaydi",
                    "Matdan yaltiroqgacha turli teksturalar mavjud",
                    "Parvarish bilan uyg‘unlashadi — SPF yoki faol moddali formulalar bor",
                ],
                "cons": [
                    "Makiya tekis turishi uchun terini tayyorlash lozim",
                    "Soyalarni teri tag rangiga mos tanlash muhim",
                ],
                "tips": [
                    "Tonal kremdan oldin primer yoki namlantiruvchi krem qo‘llang",
                    "Paletkani baza, aksent va yorituvchi qoidasi bilan tuzing",
                    "Barqarorlik uchun ohirida yupqa pudra yoki fiksatsiya spreyini seping",
                ],
                "closing": "Tadbir va kerakli effektni yozing — mos makiyaj to‘plamini tanlayman.",
            },
            "en": {
                "title": "Makeup essentials — create your statement",
                "description": "Balancing complexion, eyes, and lips lets you shift from fresh nude to bold evening looks.",
                "pros": [
                    "Highlights facial features and smooths out complexion",
                    "Wide variety of finishes from matte to glass-skin glow",
                    "Can include skincare benefits like SPF, peptides, or hydration",
                ],
                "cons": [
                    "Requires skin prep to avoid texture and patchiness",
                    "Shade matching is critical for undertone and lighting",
                ],
                "tips": [
                    "Prime or moisturize before foundation for smoother blending",
                    "Build eye palettes with a base, depth shade, and highlight",
                    "Lock the look with a light dusting of powder or setting spray",
                ],
                "closing": "Tell me about the occasion and desired mood — I’ll assemble a makeup set.",
            },
        },
    },
    {
        "id": "haircare_ritual",
        "keywords": {"волос", "hair", "шампунь", "кондиционер", "маска"},
        "phrases": {"уход за волосами", "маска для волос", "стайлинг"},
        "content": {
            "ru": {
                "title": "Уход за волосами — сила и блеск",
                "description": "Правильный шампунь, кондиционер и маски поддерживают плотность, объём и гладкость волос.",
                "pros": [
                    "Можно выстраивать решения под кожу головы и длину",
                    "Салонные активы доступны в домашнем формате",
                    "Сокращает ломкость и усиливает блеск при регулярном применении",
                ],
                "cons": [
                    "Часто нужен комбинированный подход — разные продукты для корней и длины",
                    "Термоинструменты без защиты снижают эффект ухода",
                ],
                "tips": [
                    "Делайте пилинг кожи головы раз в 1–2 недели для объёма",
                    "Используйте термозащиту перед феном или стайлером",
                    "Наносите маски на полотенцесушёные волосы и выдерживайте 5–10 минут",
                ],
                "closing": "Опишите тип волос, окрашивание и цели — предложу ритуал ухода и стайлинг.",
            },
            "uz": {
                "title": "Soch parvarishi — mustahkamlik va jilva",
                "description": "To‘g‘ri shampun, konditsioner va niqoblar sochning qalinligi va silliqligini saqlaydi.",
                "pros": [
                    "Bosh terisi va uzunlik uchun alohida yechim tuzish mumkin",
                    "Salon darajasidagi faol moddalardan uy sharoitida ham foydalaniladi",
                    "Muntazam qo‘llaganda sinishni kamaytirib, jilo beradi",
                ],
                "cons": [
                    "Ko‘pincha ildiz va uzunlik uchun turli vositalar kerak bo‘ladi",
                    "Issiq asboblar himoyasiz ishlatilsa, parvarish natijasi kamayadi",
                ],
                "tips": [
                    "Hajm uchun bosh terisini 1–2 haftada bir pilling qiling",
                    "Fen yoki stylerdan oldin har doim termohimoya seping",
                    "Niqoblarni sochiq bilan quritilgan sochga 5–10 daqiqa ushlang",
                ],
                "closing": "Soch turi, bo‘yoq va maqsadlaringizni yozing — mos parvarish va stilingni tavsiya qilaman.",
            },
            "en": {
                "title": "Haircare ritual — strength with shine",
                "description": "Targeted shampoo, conditioner, and masks maintain density, smoothness, and bounce.",
                "pros": [
                    "Solutions can be tailored to scalp condition and hair length",
                    "Salon-grade actives now accessible for at-home use",
                    "Reduces breakage and boosts shine with consistent care",
                ],
                "cons": [
                    "Often requires different formulas for roots versus lengths",
                    "Heat styling without protection undermines results",
                ],
                "tips": [
                    "Use a scalp scrub every 1–2 weeks to refresh roots",
                    "Apply heat protectant before blow-drying or styling tools",
                    "Mask on towel-dried hair for 5–10 minutes for deeper penetration",
                ],
                "closing": "Share hair type, color history, and goals — I’ll design a care and styling ritual.",
            },
        },
    },
    {
        "id": "fragrance_layering",
        "keywords": {"аромат", "парфюм", "духи", "fragrance", "атыр"},
        "phrases": {"подбор аромата", "слоение ароматов"},
        "content": {
            "ru": {
                "title": "Парфюмерия — настроение в одном флаконе",
                "description": "Правильно подобранный аромат подчеркивает индивидуальность и завершает образ.",
                "pros": [
                    "Слоение и сочетание создают уникальный шлейф",
                    "Можно выбрать концентрацию от лёгкого mist до extrait",
                    "Ароматы влияют на эмоции и помогают закрепить воспоминания",
                ],
                "cons": [
                    "Нужно учитывать пирамиду нот и стойкость на коже",
                    "Чрезмерное нанесение может быть навязчивым для окружающих",
                ],
                "tips": [
                    "Тестируйте ароматы на коже и наблюдайте раскрытие в течение дня",
                    "Для стойкости наносите на увлажнённую кожу и точки пульса",
                    "Комбинируйте парфюмы одной семьи или связывайте общей нотой",
                ],
                "closing": "Расскажите о любимых нотах и ситуациях — помогу подобрать аромат или layering.",
            },
            "uz": {
                "title": "Atir — kayfiyatni ifodalovchi aksent",
                "description": "To‘g‘ri tanlangan hid individual uslubni ta’kidlab, obrazni yakunlaydi.",
                "pros": [
                    "Birlashtirish va qatlamlash noyob iz qoldiradi",
                    "Engil tumancha yoki intensiv extraitdan mosini tanlash mumkin",
                    "Aromatlar his-tuyg‘ularga ta’sir qiladi va xotiralarni mustahkamlaydi",
                ],
                "cons": [
                    "Notalar piramidasi va teridagi turg‘unlikni hisobga olish kerak",
                    "Haddan tashqari sepish atrofdagilar uchun og‘ir bo‘lishi mumkin",
                ],
                "tips": [
                    "Atirni terida sinab, kun davomida qanday ochilishini kuzating",
                    "Barqarorlik uchun namlangan teriga va puls nuqtalariga seping",
                    "Bir oilaga mansub yoki umumiy notali atirlarni qatlamlang",
                ],
                "closing": "Yoqtirgan notalar va vaziyatlarni yozing — mos atir yoki layering taklif qilaman.",
            },
            "en": {
                "title": "Fragrance layering — signature aura",
                "description": "A well-chosen scent highlights personality and finalizes the look.",
                "pros": [
                    "Layering different scents builds a unique trail",
                    "Choose concentrations from airy mist to intense extrait",
                    "Fragrances influence mood and anchor memories",
                ],
                "cons": [
                    "Must consider note pyramid and longevity on skin",
                    "Overapplication may feel overwhelming to others",
                ],
                "tips": [
                    "Test scents on skin and track the dry-down through the day",
                    "Apply to moisturized skin and pulse points for projection",
                    "Layer within the same fragrance family or around a shared note",
                ],
                "closing": "Describe your favorite notes and occasions — I’ll suggest a scent or layering idea.",
            },
        },
    },
]


_INDEX_CACHE: Dict[str, dict] = {}


def _build_index() -> Dict[str, Set[str]]:
    index: Dict[str, Set[str]] = {}
    for entry in MATERIAL_GUIDE:
        keywords = entry.get("keywords", set()) | entry.get("phrases", set())
        for lang_content in entry.get("content", {}).values():
            text_parts: List[str] = []
            for key in ("title", "description", "pros", "cons", "tips", "closing"):
                value = lang_content.get(key)
                if isinstance(value, list):
                    text_parts.extend(value)
                elif isinstance(value, str):
                    text_parts.append(value)
            keywords |= set(_tokenize_normalized(_normalize(" ".join(text_parts))))
        for keyword in keywords:
            normalized = _normalize(keyword)
            index.setdefault(normalized, set()).add(entry["id"])
    return index


def _ensure_index() -> Dict[str, Set[str]]:
    if not _INDEX_CACHE:
        _INDEX_CACHE.update(_build_index())
    return _INDEX_CACHE


def get_material_reference(query: str, lang: str) -> Optional[str]:
    if not query:
        return None
    normalized = _normalize(query)
    tokens = _tokenize_normalized(normalized)
    index = _ensure_index()

    matched_ids: Set[str] = set()
    for token in tokens:
        ids = index.get(token)
        if ids:
            matched_ids.update(ids)

    if not matched_ids:
        return None

    entries = [entry for entry in MATERIAL_GUIDE if entry.get("id") in matched_ids]
    formatted = [
        _format_entry(entry, lang)
        for entry in entries
    ]
    formatted = [item for item in formatted if item]

    if not formatted:
        return None

    return "\n\n".join(formatted)
