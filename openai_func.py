# import os
# import re
# import logging
# from collections import defaultdict
# from dotenv import load_dotenv
# from openai import OpenAI
# from db import Session, Flats as DBFlats

# from langdetect import detect, LangDetectException
# from deep_translator import GoogleTranslator
# from urllib.parse import urlsplit, urlunsplit, quote

# # === Инициализация ===
# load_dotenv()
# openai = OpenAI(api_key=os.getenv("API_KEY"))
# logger = logging.getLogger(__name__)

# # === Хранилища данных ===
# user_conversations = defaultdict(list)
# last_flats_cache = {}
# last_filters_cache = {}
# shown_flats_cache = defaultdict(set)
# selected_flat_cache = {}

# # === Поддерживаемые языки ===
# SUPPORTED_LANGS = {"ru", "uz", "en"}


# # === Утилита нормализации URL ===
# def normalize_url(url: str) -> str:
#     try:
#         parts = urlsplit(url)
#         if not parts.scheme:
#             return url
#         path = quote(parts.path, safe="/%")
#         query = quote(parts.query, safe="=&?")
#         return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))
#     except Exception:
#         return url


# # === Определение языка ===
# def detect_language(text: str) -> str:
#     if not text.strip():
#         return "ru"

#     prompt = f"""
#     Detect the language of the following text and respond ONLY with one of these ISO codes:
#     - ru (Russian)
#     - en (English)
#     - uz (Uzbek)
#     - kk (Kazakh)
#     If language is not one of them, respond with "unsupported".

#     Text: "{text}"
#     """

#     try:
#         response = openai.chat.completions.create(
#             model="gpt-5-nano",
#             messages=[{"role": "user", "content": prompt}],
#         )
#         lang_code = response.choices[0].message.content.strip().lower()
#         if lang_code not in {"ru", "en", "uz", "kk"}:
#             return "unsupported"
#         return lang_code
#     except Exception as e:
#         print(f"Ошибка GPT при определении языка: {e}")
#         return "ru"


# # === Перевод текста ===
# def translate_text_if_needed(text: str, target_lang: str) -> str:
#     if not text or target_lang not in SUPPORTED_LANGS or target_lang == "ru":
#         return text

#     # if protected_words is None:
#     #     protected_words = []

#     # # Помечаем защищенные слова специальным токеном
#     # placeholder_map = {word: f"[[[{i}]]]" for i, word in enumerate(protected_words)}
#     # for word, placeholder in placeholder_map.items():
#     #     text = re.sub(re.escape(word), placeholder, text, flags=re.IGNORECASE)

#     system_prompt = f"""

# Ты переводчик текста на {target_lang}.
# Не переводи и не меняй навзвании ЖК Компании и тд.
# Переводи остальной текст максимально точно и кратко.
# """

#     try:
#         response = client.chat.completions.create(
#             model="gpt-5-nano",
#             messages=[
#                 {"role": "system", "content": system_prompt},
#                 {"role": "user", "content": text}
#             ]
#         )

#         translated = response.choices[0].message.content.strip()

#         # # Восстанавливаем защищённые слова
#         # for word, placeholder in placeholder_map.items():
#         #     translated = translated.replace(placeholder, word)

#         return translated

#     except Exception as e:
#         print(f"Ошибка при переводе через GPT: {e}")
#         return text

# # === Парсинг фильтров ===
# def parse_filters_from_text(text: str) -> dict:
#     filters = {}
#     if not text:
#         return filters
#     t = text.lower()

#     # === Количество комнат ===
#     if m := re.search(r'(\d+)\s*(?:комн|комнат|xonali|room|otaq|xonasi)', t):
#         filters["rooms"] = int(m.group(1))
#     elif any(x in t for x in ["одн", "bir", "one"]):
#         filters["rooms"] = 1
#     elif any(x in t for x in ["двух", "ikki", "two", "2 "]):
#         filters["rooms"] = 2
#     elif any(x in t for x in ["трех", "uch", "three", "3 "]):
#         filters["rooms"] = 3

#     # === Этаж ===
#     if m := re.search(r'(\d+)\s*(?:этаж|qavat|floor)', t):
#         filters["stage"] = int(m.group(1))

#     # === Максимальная цена ===
#     if m := re.search(r'(?:до|gacha|up to)\s*([$]?\s*[0-9\s\.,kкK]+)', t):
#         s = m.group(1)
#         if s:
#             s = s.replace(' ', '').replace(',', '').replace('$', '')
#             if re.search(r'[kкK]', s):
#                 s = re.sub(r'[^\d]', '', s)
#                 if s:
#                     filters["price_max"] = int(s) * 1000
#             else:
#                 s = re.sub(r'[^\d]', '', s)
#                 if s:
#                     filters["price_max"] = int(s)

#     # === Сортировка по цене ===
#     if any(k in t for k in ["дешев", "arzon", "cheap"]):
#         filters["price_order"] = "min"
#     elif any(k in t for k in ["дорог", "qimmat", "expensive"]):
#         filters["price_order"] = "max"

#     # === Тип объекта: квартира, студия, магазин ===
#     if any(k in t for k in ["квартира", "apartment", "uy"]):
#         filters["type"] = "квартира"
#     elif any(k in t for k in ["студия", "studio"]):
#         filters["type"] = "студия"
#     elif any(k in t for k in ["магазин", "shop", "do‘kon"]):
#         filters["type"] = "магазин"

#     return filters


# # === Основная функция обработки ===
# def ask_openai_sync(user_id: int, text: str):
#     lang = detect_language(text)

#     if lang not in SUPPORTED_LANGS:
#         msg = (
#             "❗ Извините, поддерживаются только узбекский, русский и английский языки.\n\n"
#             "❗ Sorry, only Uzbek, Russian and English languages are supported.\n\n"
#             "❗ Iltimos, bot faqat o‘zbek, rus va ingliz tillarida ishlaydi."
#         )
#         user_conversations[user_id].append({"role": "assistant", "content": msg})
#         return {"text": msg}

#     text_lower = text.lower().strip()
#     user_conversations[user_id].append({"role": "user", "content": text})

#     filters = parse_filters_from_text(text_lower)

#     if m := re.match(r'/photo_(\d+)', text_lower):
#         flat_id = int(m.group(1))
#         res = get_flat_image_text(flat_id, lang)
#         user_conversations[user_id].append({"role": "assistant", "content": res["text"]})
#         return res

#     if not filters and not any(w in text_lower for w in ["квартир", "flat", "uy", "room", "xonali", "ищи", "найди", "show"]):
#         msg = {
#             "ru": "Пожалуйста, уточните хотя бы одно пожелание 💬\nНапример: «2 комнаты до 60 000 $» или «на среднем этаже».",
#             "uz": "Iltimos, kamida bitta talabni kiriting 💬\nMasalan: «2 xonali, narxi 60 000 $gacha» yoki «o‘rta qavat».",
#             "en": "Please specify at least one preference 💬\nFor example: '2 rooms up to $60,000' or 'middle floor'."
#         }[lang]
#         user_conversations[user_id].append({"role": "assistant", "content": msg})
#         return {"text": msg}

#     if filters:
#         last_filters_cache[user_id] = filters
#         shown_flats_cache[user_id].clear()
#         res = _get_flats_from_db(user_id, filters, lang)
#         _save_bot_messages(user_id, res)
#         return res

#     if any(w in text_lower for w in ["ещё", "еще", "yana", "more"]):
#         filters = last_filters_cache.get(user_id, {})
#         if not filters:
#             msg = translate_text_if_needed("Пожалуйста, уточните хотя бы одно пожелание 💬", lang)
#             user_conversations[user_id].append({"role": "assistant", "content": msg})
#             return {"text": msg}
#         res = _get_flats_from_db(user_id, filters, lang, skip_seen=True)
#         _save_bot_messages(user_id, res)
#         return res

#     res = _get_flats_from_db(user_id, last_filters_cache.get(user_id, {}), lang)
#     _save_bot_messages(user_id, res)
#     return res


# # === Сохраняем ответы GPT ===
# def _save_bot_messages(user_id: int, res: dict):
#     if "text" in res:
#         user_conversations[user_id].append({"role": "assistant", "content": res["text"]})
#     if "flats" in res:
#         for f in res["flats"]:
#             user_conversations[user_id].append({"role": "assistant", "content": f["text"]})


# # === Поиск квартир ===
# def _get_flats_from_db(user_id: int, filters: dict, lang: str, skip_seen: bool = False):
#     session = Session()
#     query = session.query(DBFlats)

#     if filters.get("rooms"):
#         query = query.filter(DBFlats.rooms == filters["rooms"])
#     if filters.get("stage"):
#         query = query.filter(DBFlats.stage == filters["stage"])
#     if filters.get("price_max"):
#         query = query.filter(DBFlats.price <= filters["price_max"])
#     if filters.get("price_order") == "min":
#         query = query.order_by(DBFlats.price.asc())
#     elif filters.get("price_order") == "max":
#         query = query.order_by(DBFlats.price.desc())

#     flats = query.all()
#     session.close()

#     if not flats:
#         msg = {
#             "ru": "К сожалению, квартиры с такими параметрами не найдены. 🏙",
#             "uz": "Afsuski, bunday parametrli kvartiralar topilmadi. 🏙",
#             "en": "Unfortunately, no apartments match these parameters. 🏙"
#         }[lang]
#         return {"text": msg}

#     seen = shown_flats_cache[user_id]
#     new_flats = [f for f in flats if f.number not in seen][:4]
#     if not new_flats:
#         shown_flats_cache[user_id].clear()
#         new_flats = flats[:4]

#     for f in new_flats:
#         seen.add(f.number)

#     results = []
#     for f in new_flats:
#         base_text = (
#             f"👤 {f.rooms} комнаты\n"
#             f"🏠 Квартира №{f.number}\n\n"
#             f"🏠 ЖК Royal Residence\n"
#             f"• Комнат: {f.rooms}\n"
#             f"• Этаж: {f.stage}\n"
#             f"• Площадь: {f.sq_m} м²\n"
#             f"• Цена: {f.price} $\n"
#             f"• Подъезд: {f.lobby}\n\n"
#             "С вами свяжется менеджер для уточнения деталей. 🏙"
#         )
#         user_text = translate_text_if_needed(base_text, lang)
#         photo_val = normalize_url(f.plan.strip()) if getattr(f, "plan", None) else None
#         results.append({"text": user_text, "photo": photo_val})

#     return {"flats": results}


# # === Фото квартиры ===
# def get_flat_image_text(flat_id: int, lang: str):
#     session = Session()
#     flat = session.query(DBFlats).filter(DBFlats.number == flat_id).first()
#     session.close()

#     if not flat:
#         msg = {
#             "ru": f"Квартира с номером {flat_id} не найдена.",
#             "uz": f"{flat_id}-raqamli kvartira topilmadi.",
#             "en": f"Apartment #{flat_id} not found."
#         }[lang]
#         return {"text": msg}

#     base_text = (
#         f"👤 {flat.rooms} комнаты\n"
#         f"🏠 Квартира №{flat.number}\n\n"
#         f"🏠 ЖК Royal Residence\n"
#         f"• Комнат: {flat.rooms}\n"
#         f"• Этаж: {flat.stage}\n"
#         f"• Площадь: {flat.sq_m} м²\n"
#         f"• Цена: {flat.price} $\n"
#         f"• Подъезд: {flat.lobby}\n\n"
#         "С вами свяжется менеджер для уточнения деталей. 🏙"
#     )

#     user_text = translate_text_if_needed(base_text, lang)
#     photo_val = normalize_url(flat.plan.strip()) if flat.plan else None
#     return {"text": user_text, "photo": photo_val}


# # === Формат диалога ===
# def get_formatted_dialog(user_id: int) -> str:
#     msgs = user_conversations.get(user_id, [])
#     lines = []
#     for m in msgs:
#         prefix = "👤" if m["role"] == "user" else "🤖"
#         lines.append(f"{prefix} {m['content'].strip()}")
#     return "\n".join(lines) if lines else "—"


# # === Очистка истории ===
# def clear_user_conversation(user_id: int):
#     user_conversations[user_id].clear()
#     last_flats_cache.pop(user_id, None)
#     last_filters_cache.pop(user_id, None)
#     shown_flats_cache.pop(user_id, None)
#     selected_flat_cache.pop(user_id, None)



# 
# import os
# import re
# import logging
# from collections import defaultdict
# from dotenv import load_dotenv
# from openai import OpenAI
# from db import Session, Flats as DBFlats
# from urllib.parse import urlsplit, urlunsplit, quote
# import json

# # === Инициализация ===
# load_dotenv()
# client = OpenAI(api_key=os.getenv("API_KEY"))
# logger = logging.getLogger(__name__)

# # === Кэши ===
# user_conversations = defaultdict(list)
# last_filters_cache = {}
# shown_flats_cache = defaultdict(set)

# SUPPORTED_LANGS = {"ru", "uz", "en", "kk"}


# def normalize_url(url: str) -> str:
#     try:
#         parts = urlsplit(url)
#         path = quote(parts.path, safe="/%") if parts.path else ""
#         query = quote(parts.query, safe="=&?") if parts.query else ""
#         return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))
#     except Exception:
#         return url


# def get_formatted_dialog(user_id: int) -> str:
#     msgs = user_conversations.get(user_id, [])
#     if not msgs:
#         return "—"
#     lines = []
#     for m in msgs:
#         prefix = "👤" if m["role"] == "user" else "🤖"
#         lines.append(f"{prefix} {m['content'].strip()}")
#     return "\n".join(lines)


# def ask_openai_sync(user_id: int, text: str):
#     """
#     Новый вариант функции:
#     1. GPT распознаёт язык, парсит фильтры и делает шаблон ответа.
#     2. SQLAlchemy берёт реальные объекты из базы и формирует текст для пользователя.
#     """

#     text = text.strip()
#     if not text:
#         return {"text": "❗ Пустой запрос"}

#     user_conversations[user_id].append({"role": "user", "content": text})

#     # --- Шаг 1: GPT парсер + переводчик ---
#     try:
#         gpt_prompt = f"""
#         Ты эксперт по недвижимости. 
#         Пользователь пишет текст запроса. 
#         Твоя задача:
#         1. Определи язык запроса (ru, en, uz, kk) и верни как "lang".
#         2. Определи фильтры для поиска квартир/студий/магазинов:
#            - type: "Квартира", "Студия", "Магазин" или пусто
#            - rooms: количество комнат, если указано
#            - stage: этаж, если указано
#            - price_max: максимальная цена, если указано
#            - price_order: "min" или "max" если есть слова дешево/дорого
#         3. Верни шаблон текста для ответа пользователю на том языке, на котором был запрос. 
#            Не переводи названия ЖК.
#         Формат JSON:
#         {{
#             "filters": {{
#                 "type": "...",
#                 "rooms": ...,
#                 "stage": ...,
#                 "price_max": ...,
#                 "price_order": "..."
#             }},
#             "lang": "...",
#             "template_text": "..."
#         }}
#         Текст запроса: "{text}"
#         """
#         response = client.chat.completions.create(
#             model="gpt-5-nano",
#             messages=[{"role": "user", "content": gpt_prompt}],
#         )
#         gpt_output = response.choices[0].message.content.strip()

#         # Пробуем распарсить JSON
#         try:
#             gpt_data = json.loads(gpt_output)
#         except Exception:
#             logger.warning(f"Не удалось распарсить JSON от GPT: {gpt_output}")
#             gpt_data = {
#                 "filters": {},
#                 "lang": "ru",
#                 "template_text": "Пожалуйста, уточните параметры поиска."
#             }

#         filters = gpt_data.get("filters", {})
#         lang = gpt_data.get("lang", "ru")
#         template_text = gpt_data.get("template_text", "")
#     except Exception as e:
#         logger.error(f"Ошибка GPT: {e}")
#         filters = {}
#         lang = "ru"
#         template_text = "Пожалуйста, уточните параметры поиска."

#     if not filters:
#         user_conversations[user_id].append({"role": "assistant", "content": template_text})
#         return {"text": template_text}

#     # --- Шаг 2: SQLAlchemy выборка ---
#     session = Session()
#     query = session.query(DBFlats)

#     if filters.get("type"):
#         query = query.filter(DBFlats.type == filters["type"])
#     if filters.get("rooms"):
#         query = query.filter(DBFlats.rooms == filters["rooms"])
#     if filters.get("stage"):
#         query = query.filter(DBFlats.stage == filters["stage"])
#     if filters.get("price_max"):
#         query = query.filter(DBFlats.price <= filters["price_max"])

#     if filters.get("price_order") == "min":
#         query = query.order_by(DBFlats.price.asc())
#     elif filters.get("price_order") == "max":
#         query = query.order_by(DBFlats.price.desc())

#     flats = query.all()
#     session.close()

#     if not flats:
#         msg = {
#             "ru": "К сожалению, объекты с такими параметрами не найдены. 🏙",
#             "uz": "Afsuski, bunday parametrli obyektlar topilmadi. 🏙",
#             "en": "Unfortunately, no properties match these parameters. 🏙",
#             "kk": "Өкінішке орай, мұндай параметрлерге сай нысандар табылмады. 🏙"
#         }.get(lang, "К сожалению, объекты не найдены.")
#         user_conversations[user_id].append({"role": "assistant", "content": msg})
#         return {"text": msg}

#     seen = shown_flats_cache[user_id]
#     new_flats = [f for f in flats if f.number not in seen][:4]
#     if not new_flats:
#         seen.clear()
#         new_flats = flats[:4]
#     for f in new_flats:
#         seen.add(f.number)

#     # --- Формируем финальный результат ---
#     results = []
#     for f in new_flats:
#         text_base = (
#             f"🏠 {f.type} №{f.number}\n"
#             f"• Комнат: {f.rooms}\n"
#             f"• Этаж: {f.stage}\n"
#             f"• Площадь: {f.sq_m} м²\n"
#             f"• Цена: {f.price} $\n"
#             f"• Подъезд: {f.lobby}\n"
#             "С вами свяжется менеджер для уточнения деталей. 🏙"
#         )
#         photo_val = normalize_url(f.plan.strip()) if getattr(f, "plan", None) else None
#         results.append({"text": text_base, "photo": photo_val})
#         user_conversations[user_id].append({"role": "assistant", "content": text_base})

#     # Сохраняем фильтры
#     last_filters_cache[user_id] = filters

#     return {"flats": results}


# # --- Очистка истории ---
# def clear_user(user_id: int):
#     user_conversations[user_id].clear()
#     last_filters_cache.pop(user_id, None)
#     shown_flats_cache.pop(user_id, None)


# import os
# import logging
# from collections import defaultdict
# from dotenv import load_dotenv
# from openai import OpenAI
# from db import Session, Flats as DBFlats
# from urllib.parse import urlsplit, urlunsplit, quote

# # === Инициализация ===
# load_dotenv()
# client = OpenAI(api_key=os.getenv("API_KEY"))
# logger = logging.getLogger(__name__)

# # === Кэши ===
# user_conversations = defaultdict(list)
# last_filters_cache = {}
# shown_flats_cache = defaultdict(set)

# SUPPORTED_LANGS = {"ru", "uz", "en", "kk"}


# def normalize_url(url: str) -> str:
#     try:
#         parts = urlsplit(url)
#         path = quote(parts.path, safe="/%") if parts.path else ""
#         query = quote(parts.query, safe="=&?") if parts.query else ""
#         return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))
#     except Exception:
#         return url


# def get_formatted_dialog(user_id: int) -> str:
#     msgs = user_conversations.get(user_id, [])
#     if not msgs:
#         return "—"
#     lines = []
#     for m in msgs:
#         prefix = "👤" if m["role"] == "user" else "🤖"
#         lines.append(f"{prefix} {m['content'].strip()}")
#     return "\n".join(lines)


# def detect_lang(text: str) -> str:
#     """Определяем язык запроса с помощью GPT, возвращаем 'ru', 'uz', 'en' или 'kk'."""
#     try:
#         gpt_prompt = f"""
# Определи язык этого текста: "{text}"
# Возможные варианты: "ru", "uz", "en", "kk".
# Ответь только код языка, без лишних слов.
# """
#         response = client.chat.completions.create(
#             model="gpt-5-nano",
#             messages=[{"role": "user", "content": gpt_prompt}],
#         )
#         lang = response.choices[0].message.content.strip().lower()
#         if lang not in SUPPORTED_LANGS:
#             lang = "ru"
#         return lang
#     except Exception as e:
#         logger.error(f"Ошибка GPT при определении языка: {e}")
#         return "ru"


# def ask_openai_sync(user_id: int, text: str):
#     """
#     Основная функция:
#     1. GPT определяет язык.
#     2. SQLAlchemy выбирает объекты из базы.
#     3. Формируется текст ответа для пользователя на его языке.
#     """
#     text = text.strip()
#     if not text:
#         return {"text": "❗ Пустой запрос"}

#     user_conversations[user_id].append({"role": "user", "content": text})

#     # --- Определяем язык ---
#     lang = detect_lang(text)

#     # --- SQLAlchemy выборка ---
#     session = Session()
#     query = session.query(DBFlats)

#     # Можно добавить локальный парсер для фильтров здесь (type, rooms, price и т.д.)
#     # Например, простой пример: ищем квартиры по ключевым словам
#     if "2 хонали" in text or "2 комнат" in text:
#         query = query.filter(DBFlats.rooms == 2)
#     if "манга" in text or "квартира" in text:
#         query = query.filter(DBFlats.type == "Квартира")

#     flats = query.all()
#     session.close()

#     if not flats:
#         msg = {
#             "ru": "К сожалению, объекты с такими параметрами не найдены. 🏙",
#             "uz": "Afsuski, bunday parametrli obyektlar topilmadi. 🏙",
#             "en": "Unfortunately, no properties match these parameters. 🏙",
#             "kk": "Өкінішке орай, мұндай параметрлерге сай нысандар табылмады. 🏙"
#         }.get(lang, "К сожалению, объекты не найдены.")
#         user_conversations[user_id].append({"role": "assistant", "content": msg})
#         return {"text": msg}

#     # --- Фильтрация уже показанных объектов ---
#     seen = shown_flats_cache[user_id]
#     new_flats = [f for f in flats if f.number not in seen][:4]
#     if not new_flats:
#         seen.clear()
#         new_flats = flats[:4]
#     for f in new_flats:
#         seen.add(f.number)

#     # --- Формируем финальный результат ---
#     results = []
#     for f in new_flats:
#         text_base = (
#             f"🏠 {f.type} №{f.number}\n"
#             f"• Комнат: {f.rooms}\n"
#             f"• Этаж: {f.stage}\n"
#             f"• Площадь: {f.sq_m} м²\n"
#             f"• Цена: {f.price} $\n"
#             f"• Подъезд: {f.lobby}\n"
#             "С вами свяжется менеджер для уточнения деталей. 🏙"
#         )
#         photo_val = normalize_url(f.plan.strip()) if getattr(f, "plan", None) else None
#         results.append({"text": text_base, "photo": photo_val})
#         user_conversations[user_id].append({"role": "assistant", "content": text_base})

#     last_filters_cache[user_id] = {}  # фильтры можно расширять позже

#     return {"flats": results}


# # --- Очистка истории ---
# def clear_user(user_id: int):
#     user_conversations[user_id].clear()
#     last_filters_cache.pop(user_id, None)
#     shown_flats_cache.pop(user_id, None)


# import os
# import logging
# import asyncio
# from collections import defaultdict
# from dotenv import load_dotenv
# from openai import OpenAI
# from db import Session, Flats as DBFlats
# from urllib.parse import urlsplit, urlunsplit, quote
# import json
# from datetime import datetime, timedelta

# # === Инициализация ===
# load_dotenv()
# client = OpenAI(api_key=os.getenv("API_KEY"))
# logger = logging.getLogger(__name__)

# # === Кэши ===
# user_conversations = defaultdict(list)
# last_filters_cache = {}
# shown_flats_cache = defaultdict(set)
# gpt_cache = {}  # user_id: {"data": {...}, "expires": datetime}

# SUPPORTED_LANGS = {"ru", "uz", "en", "kk"}
# GPT_CACHE_TTL = timedelta(minutes=5)  # кэш GPT на 5 минут

# # === Утилиты ===
# def normalize_url(url: str) -> str:
#     try:
#         parts = urlsplit(url)
#         path = quote(parts.path, safe="/%") if parts.path else ""
#         query = quote(parts.query, safe="=&?") if parts.query else ""
#         return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))
#     except Exception:
#         return url

# def get_formatted_dialog(user_id: int) -> str:
#     msgs = user_conversations.get(user_id, [])
#     if not msgs:
#         return "—"
#     lines = []
#     for m in msgs:
#         prefix = "👤" if m["role"] == "user" else "🤖"
#         lines.append(f"{prefix} {m['content'].strip()}")
#     return "\n".join(lines)

# # === Основная функция ===
# async def ask_openai_sync(user_id: int, text: str):
#     text = text.strip()
#     if not text:
#         return {"text": "❗ Пустой запрос"}

#     user_conversations[user_id].append({"role": "user", "content": text})

#     now = datetime.utcnow()
#     cached = gpt_cache.get(user_id)
#     if cached and cached["expires"] > now:
#         gpt_data = cached["data"]
#     else:
#         try:
#             gpt_prompt = f"""
# Ты эксперт по недвижимости и JSON-парсингу. 
# Строго возвращай JSON с фильтрами, без текста и объяснений.

# 1. Определи язык запроса (ru, en, uz, kk) -> "lang".
# 2. Определи фильтры для поиска объектов:
#    - type: "Квартира", "Студия", "Магазин" или null
#    - rooms: количество комнат (int) или null
#    - stage: этаж (int) или null
#    - price_max: максимальная цена (int) или null
#    - price_order: "min" или "max" или null

# Возвращай строго JSON:

# {{
#   "filters": {{
#     "type": "...",
#     "rooms": ...,
#     "stage": ...,
#     "price_max": ...,
#     "price_order": "..."
#   }},
#   "lang": "..."
# }}

# Пример: "2-комнатная квартира до 50000, дешево" -> {{
#   "filters": {{
#     "type": "Квартира",
#     "rooms": 2,
#     "stage": null,
#     "price_max": 50000,
#     "price_order": "min"
#   }},
#   "lang": "ru"
# }}

# Запрос пользователя: "{text}"
# """
#             response = await client.chat.completions.acreate(
#                 model="gpt-5-nano",
#                 messages=[{"role": "user", "content": gpt_prompt}],
#             )
#             gpt_output = response.choices[0].message.content.strip()
#             gpt_data = json.loads(gpt_output)

#             # нормализация типов
#             filters = gpt_data.get("filters", {})
#             filters["rooms"] = int(filters["rooms"]) if filters.get("rooms") is not None else None
#             filters["stage"] = int(filters["stage"]) if filters.get("stage") is not None else None
#             filters["price_max"] = int(filters["price_max"]) if filters.get("price_max") is not None else None
#             filters["type"] = filters.get("type") if filters.get("type") in {"Квартира","Студия","Магазин"} else None
#             filters["price_order"] = filters.get("price_order") if filters.get("price_order") in {"min","max"} else None
#             gpt_data["filters"] = filters
#             gpt_cache[user_id] = {"data": gpt_data, "expires": now + GPT_CACHE_TTL}
#         except Exception as e:
#             logger.error(f"Ошибка GPT или JSON: {e}")
#             gpt_data = {"filters": {}, "lang": "ru"}

#     filters = gpt_data.get("filters", {})
#     lang = gpt_data.get("lang", "ru")

#     if not any(filters.values()):
#         msg = {
#             "ru": "Пожалуйста, уточните хотя бы одно пожелание 💬",
#             "uz": "Iltimos, kamida bitta talabni kiriting 💬",
#             "en": "Please specify at least one preference 💬",
#             "kk": "Кемінде бір параметрді көрсетіңіз 💬"
#         }.get(lang, "Пожалуйста, уточните параметры поиска.")
#         user_conversations[user_id].append({"role": "assistant", "content": msg})
#         return {"text": msg}

#     # --- SQLAlchemy выборка ---
#     session = Session()
#     query = session.query(DBFlats)

#     if filters.get("type"):
#         query = query.filter(DBFlats.type == filters["type"])
#     if filters.get("rooms"):
#         query = query.filter(DBFlats.rooms == filters["rooms"])
#     if filters.get("stage"):
#         query = query.filter(DBFlats.stage == filters["stage"])
#     if filters.get("price_max"):
#         query = query.filter(DBFlats.price <= filters["price_max"])
#     if filters.get("price_order") == "min":
#         query = query.order_by(DBFlats.price.asc())
#     elif filters.get("price_order") == "max":
#         query = query.order_by(DBFlats.price.desc())

#     flats = query.limit(50).all()
#     session.close()

#     if not flats:
#         msg = {
#             "ru": "К сожалению, объекты с такими параметрами не найдены. 🏙",
#             "uz": "Afsuski, bunday parametrli obyektlar topilmadi. 🏙",
#             "en": "Unfortunately, no properties match these parameters. 🏙",
#             "kk": "Өкінішке орай, мұндай параметрлерге сай нысандар табылмады. 🏙"
#         }.get(lang, "К сожалению, объекты не найдены.")
#         user_conversations[user_id].append({"role": "assistant", "content": msg})
#         return {"text": msg}

#     # --- Фильтрация уже показанных объектов ---
#     seen = shown_flats_cache[user_id]
#     new_flats = [f for f in flats if f.number not in seen][:4]
#     if not new_flats:
#         seen.clear()
#         new_flats = flats[:4]
#     for f in new_flats:
#         seen.add(f.number)

#     # --- Формируем результат ---
#     results = []
#     for f in new_flats:
#         text_base = (
#             f"🏠 {f.type} №{f.number}\n"
#             f"• Комнат: {f.rooms}\n"
#             f"• Этаж: {f.stage}\n"
#             f"• Площадь: {f.sq_m} м²\n"
#             f"• Цена: {f.price} $\n"
#             f"• Подъезд: {f.lobby}\n"
#             "С вами свяжется менеджер для уточнения деталей. 🏙"
#         )
#         photo_val = normalize_url(f.plan.strip()) if getattr(f, "plan", None) else None
#         results.append({"text": text_base, "photo": photo_val})
#         user_conversations[user_id].append({"role": "assistant", "content": text_base})

#     last_filters_cache[user_id] = filters
#     return {"flats": results}

# # --- Очистка истории ---
# def clear_user(user_id: int):
#     user_conversations[user_id].clear()
#     last_filters_cache.pop(user_id, None)
#     shown_flats_cache.pop(user_id, None)
#     gpt_cache.pop(user_id, None)

# import os
# import json
# import logging
# import re
# import asyncio
# from collections import defaultdict
# from urllib.parse import urlsplit, urlunsplit, quote
# from dotenv import load_dotenv
# from aiogram import Bot
# from openai import OpenAI 
# from db import Session, Flats as DBFlats

# # === Инициализация ===
# load_dotenv()
# client = OpenAI(api_key=os.getenv("API_KEY"))
# logger = logging.getLogger(__name__)

# # === Кэши ===
# user_conversations = defaultdict(list)
# last_filters_cache = {}
# shown_flats_cache = defaultdict(set)
# SUPPORTED_LANGS = {"ru", "uz", "en", "kk"}


# # === Резервный парсер ===
# def fallback_parse_filters(text: str) -> dict:
#     filters = {}

#     if match := re.search(r'(\d+)\s*комнат', text, re.IGNORECASE):
#         filters["rooms"] = int(match.group(1))
#     if match := re.search(r'(\d+)\s*(?:этаж|этаже)', text, re.IGNORECASE):
#         filters["stage"] = int(match.group(1))
#     if match := re.search(r'(\d+[.,]?\d*)\s*(?:\$|доллар|тыс)', text, re.IGNORECASE):
#         filters["price_max"] = float(match.group(1).replace(',', '.'))
#     if 'магазин' in text.lower():
#         filters["type"] = "Магазин"
#     elif 'студ' in text.lower():
#         filters["type"] = "Студия"
#     else:
#         filters["type"] = "Квартира"

#     return filters

# # === Утилита для URL ===
# def normalize_url(url: str) -> str:
#     try:
#         parts = urlsplit(url)
#         path = quote(parts.path, safe="/%") if parts.path else ""
#         query = quote(parts.query, safe="=&?") if parts.query else ""
#         return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))
#     except Exception:
#         return url


# # === Печатает... ===
# async def show_typing(bot: Bot, chat_id: int, duration: int = 5):
#     try:
#         end_time = asyncio.get_event_loop().time() + duration
#         while asyncio.get_event_loop().time() < end_time:
#             await bot.send_chat_action(chat_id, "typing")
#             await asyncio.sleep(4)
#     except Exception:
#         pass


# # === Определение языка ===
# async def detect_language(text: str) -> str:
#     try:
#         prompt = f"""
# Detect the language of this text and respond ONLY with one of:
# ru, en, uz, kk.
# Text: "{text}"
# """
#         resp = client.responses.create(
#             model="gpt-5-nano",
#             instructions=[prompt],
#             input=text,
#         )
#         print('\n \n \n',"GPT LANG RAW:", resp , '\n \n \n')
#         lang = resp.output_text.strip().lower()
#         return lang if lang in SUPPORTED_LANGS else "ru"
#     except Exception:
#         return "ru"


# # === Извлечение фильтров ===
# async def extract_filters_with_gpt(text: str) -> dict:
#     try:
#         prompt = f"""
# You are a real estate filter extractor.
# Extract parameters from the user's request and return ONLY valid JSON.

# Fields:
# - type: "Квартира" | "Студия" | "Магазин"
# - rooms: integer
# - stage: integer
# - price_max: integer
# - price_order: "min" | "max"

# User request: "{text}"

# Respond ONLY with JSON, no explanation.
# Example:
# {{"type": "Квартира", "rooms": 2, "price_max": 50000}}
# """
#         resp = client.responses.create(
#             model="gpt-5-nano",
#             instructions=[prompt],
#             input=text,
#         )
#         raw = resp.output_text.strip()
#         print('\n \n \n',"GPT RAW:", raw , '\n \n \n')
#         data = json.loads(raw)
#         if isinstance(data, dict):
#             logger.info(f"✅ GPT parsed filters: {data}")
#             return data
#         return {}
#     except Exception as e:
#         logger.warning(f"⚠️ Ошибка парсинга фильтров GPT: {e}")
#         return {}


# # === Основная функция ===
# async def ask_openai_sync(user_id: int, text: str, bot: Bot = None, chat_id: int = None):
#     print("\n\nUSER MESSAGE:", text, "\n\n")
#     text = text.strip()
#     if not text:
#         return {"text": "❗ Пустой запрос"}

#     if bot and chat_id:
#         asyncio.create_task(show_typing(bot, chat_id, duration=5))

#     # --- Язык
#     lang = await detect_language(text)
#     user_conversations[user_id].append(text)

#     # --- Фильтры GPT
#     filters = await extract_filters_with_gpt(text)
#     if not filters:
#         filters = fallback_parse_filters(text)
#         if filters:
#             logger.info(f"⚙️ GPT не вернул фильтры, fallback: {filters}")

#     if not filters:
#         msg = {
#             "ru": "Пожалуйста, уточните хотя бы одно пожелание 💬",
#             "uz": "Iltimos, kamida bitta talabni kiriting 💬",
#             "en": "Please specify at least one preference 💬",
#             "kk": "Кем дегенде бір қалауыңызды көрсетіңіз 💬",
#         }[lang]
#         return {"text": msg}

#     last_filters_cache[user_id] = filters
#     shown_flats_cache[user_id].clear()

#     if bot and chat_id:
#         asyncio.create_task(show_typing(bot, chat_id, duration=5))

#     # --- Поиск в БД
#     session = Session()
#     query = session.query(DBFlats)

#     if filters.get("type"):
#         query = query.filter(DBFlats.type == filters["type"])
#     if filters.get("rooms"):
#         query = query.filter(DBFlats.rooms == filters["rooms"])
#     if filters.get("stage"):
#         query = query.filter(DBFlats.stage == filters["stage"])
#     if filters.get("price_max"):
#         query = query.filter(DBFlats.price <= filters["price_max"])
#     if filters.get("price_order") == "min":
#         query = query.order_by(DBFlats.price.asc())
#     elif filters.get("price_order") == "max":
#         query = query.order_by(DBFlats.price.desc())

#     flats = query.filter(DBFlats.status == "Свободно").all()
#     session.close()

#     if not flats:
#         msg = {
#             "ru": "К сожалению, объекты с такими параметрами не найдены. 🏙",
#             "uz": "Afsuski, bunday parametrli obyektlar topilmadi. 🏙",
#             "en": "Unfortunately, no properties match these parameters. 🏙",
#             "kk": "Өкінішке орай, мұндай параметрлермен нысандар табылмады. 🏙",
#         }[lang]
#         return {"text": msg}

#     seen = shown_flats_cache[user_id]
#     new_flats = [f for f in flats if f.number not in seen][:4]
#     if not new_flats:
#         seen.clear()
#         new_flats = flats[:4]
#     for f in new_flats:
#         seen.add(f.number)

#     results = []
#     for f in new_flats:
#         text_base = (
#             f"🏠 {f.type} №{f.number}\n"
#             f"• Комнат: {f.rooms}\n"
#             f"• Этаж: {f.stage}\n"
#             f"• Площадь: {f.sq_m} м²\n"
#             f"• Цена: {f.price} $\n"
#             f"• Подъезд: {f.lobby}\n"
#             f"{f.description}\n\n"
#             "С вами свяжется менеджер для уточнения деталей. 🏙"
#         )

#         # Перевод описания, если язык не русский
#         if lang != "ru":
#             try:
#                 translation = client.responses.create(
#                 model="gpt-5-nano",
#             instructions=[f"Переведи текст на {lang}, не изменяя числа и названия ЖК.",text_base],
#             input=text,
#                 )
#                 text_base = translation.output_text.strip()
#             except Exception:
#                 pass

#         photo_val = normalize_url(f.plan.strip()) if getattr(f, "plan", None) else None
#         results.append({"text": text_base, "photo": photo_val})

#     return {"flats": results}


# # === Очистка истории ===
# def clear_user(user_id: int):
#     user_conversations[user_id].clear()
#     last_filters_cache.pop(user_id, None)
#     shown_flats_cache.pop(user_id, None)

# import os
# import json
# import logging
# import re
# import asyncio
# from collections import defaultdict
# from urllib.parse import urlsplit, urlunsplit, quote
# from dotenv import load_dotenv
# from aiogram import Bot
# from openai import OpenAI
# from db import Session, Flats as DBFlats

# # === Инициализация ===
# load_dotenv()
# client = OpenAI(api_key=os.getenv("API_KEY"))
# logger = logging.getLogger(__name__)

# # === Кэши ===
# user_conversations = defaultdict(list)
# last_filters_cache = {}
# shown_flats_cache = defaultdict(set)
# SUPPORTED_LANGS = {"ru", "uz", "en", "kk"}


# # === Резервный парсер ===
# def fallback_parse_filters(text: str) -> dict:
#     filters = {}

#     if match := re.search(r'(\d+)\s*комнат', text, re.IGNORECASE):
#         filters["rooms"] = int(match.group(1))
#     if match := re.search(r'(\d+)\s*(?:этаж|этаже)', text, re.IGNORECASE):
#         filters["stage"] = int(match.group(1))
#     if match := re.search(r'(\d+[.,]?\d*)\s*(?:\$|доллар|тыс)', text, re.IGNORECASE):
#         filters["price_max"] = float(match.group(1).replace(',', '.'))
#     if 'магазин' in text.lower():
#         filters["type"] = "Магазин"
#     elif 'студ' in text.lower():
#         filters["type"] = "Студия"
#     else:
#         filters["type"] = "Квартира"

#     return filters


# # === Утилита для URL ===
# def normalize_url(url: str) -> str:
#     try:
#         parts = urlsplit(url)
#         path = quote(parts.path, safe="/%") if parts.path else ""
#         query = quote(parts.query, safe="=&?") if parts.query else ""
#         return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))
#     except Exception:
#         return url


# # === Печатает... ===
# async def show_typing(bot: Bot, chat_id: int, duration: int = 5):
#     try:
#         end_time = asyncio.get_event_loop().time() + duration
#         while asyncio.get_event_loop().time() < end_time:
#             await bot.send_chat_action(chat_id, "typing")
#             await asyncio.sleep(4)
#     except Exception:
#         pass


# # === Определение языка ===
# async def detect_language(text: str) -> str:
#     try:
#         messages = [
#             {"role": "system", "content": "Respond ONLY with ru, en, uz, or kk."},
#             {"role": "user", "content": f"Detect the language of this text: {text}"}
#         ]
#         resp = client.chat.completions.create(
#             model="gpt-4.1-mini",
#             messages=messages,
#             max_tokens=5
#         )
#         lang = resp.choices[0].message.content.strip().lower()
#         return lang if lang in SUPPORTED_LANGS else "ru"
#     except Exception:
#         return "ru"


# # === Извлечение фильтров ===
# async def extract_filters_with_gpt(text: str) -> dict:
#     try:
#         messages = [
#             {"role": "system", "content": (
#                 "You are a real estate filter extractor. "
#                 "Extract parameters from the user's request and return ONLY valid JSON with these fields: "
#                 "{type, rooms, stage, price_max, price_order}."
#             )},
#             {"role": "user", "content": text}
#         ]
#         resp = client.chat.completions.create(
#             model="gpt-4.1-mini",
#             messages=messages,
#             max_tokens=200
#         )
#         raw = resp.choices[0].message.content.strip()
#         print("\n\nGPT RAW:", raw, "\n\n")
#         data = json.loads(raw)
#         if isinstance(data, dict):
#             logger.info(f"✅ GPT parsed filters: {data}")
#             return data
#         return {}
#     except Exception as e:
#         logger.warning(f"⚠️ Ошибка парсинга фильтров GPT: {e}")
#         return {}


# # === Основная функция ===
# async def ask_openai_sync(user_id: int, text: str, bot: Bot = None, chat_id: int = None):
#     print("\n\nUSER MESSAGE:", text, "\n\n")
#     text = text.strip()
#     if not text:
#         return {"text": "❗ Пустой запрос"}

#     if bot and chat_id:
#         asyncio.create_task(show_typing(bot, chat_id, duration=5))

#     lang = await detect_language(text)
#     user_conversations[user_id].append(text)

#     filters = await extract_filters_with_gpt(text)
#     if not filters:
#         filters = fallback_parse_filters(text)
#         if filters:
#             logger.info(f"⚙️ GPT не вернул фильтры, fallback: {filters}")

#     if not filters:
#         msg = {
#             "ru": "Пожалуйста, уточните хотя бы одно пожелание 💬",
#             "uz": "Iltimos, kamida bitta talabni kiriting 💬",
#             "en": "Please specify at least one preference 💬",
#             "kk": "Кем дегенде бір қалауыңызды көрсетіңіз 💬",
#         }[lang]
#         return {"text": msg}

#     last_filters_cache[user_id] = filters
#     shown_flats_cache[user_id].clear()

#     if bot and chat_id:
#         asyncio.create_task(show_typing(bot, chat_id, duration=5))

#     # === Поиск в БД ===
#     session = Session()
#     query = session.query(DBFlats)

#     if filters.get("type"):
#         query = query.filter(DBFlats.type == filters["type"])
#     if filters.get("rooms"):
#         query = query.filter(DBFlats.rooms == filters["rooms"])
#     if filters.get("stage"):
#         query = query.filter(DBFlats.stage == filters["stage"])
#     if filters.get("price_max"):
#         query = query.filter(DBFlats.price <= filters["price_max"])
#     if filters.get("price_order") == "min":
#         query = query.order_by(DBFlats.price.asc())
#     elif filters.get("price_order") == "max":
#         query = query.order_by(DBFlats.price.desc())

#     flats = query.filter(DBFlats.status == "Свободно").all()
#     session.close()

#     if not flats:
#         msg = {
#             "ru": "К сожалению, объекты с такими параметрами не найдены. 🏙",
#             "uz": "Afsuski, bunday parametrli obyektlar topilmadi. 🏙",
#             "en": "Unfortunately, no properties match these parameters. 🏙",
#             "kk": "Өкінішке орай, мұндай параметрлермен нысандар табылмады. 🏙",
#         }[lang]
#         return {"text": msg}

#     seen = shown_flats_cache[user_id]
#     new_flats = [f for f in flats if f.number not in seen][:4]
#     if not new_flats:
#         seen.clear()
#         new_flats = flats[:4]
#     for f in new_flats:
#         seen.add(f.number)

#     results = []
#     for f in new_flats:
#         text_base = (
#             f"🏠 {f.type} №{f.number}\n"
#             f"• Комнат: {f.rooms}\n"
#             f"• Этаж: {f.stage}\n"
#             f"• Площадь: {f.sq_m} м²\n"
#             f"• Цена: {f.price} $\n"
#             f"• Подъезд: {f.lobby}\n"
#             f"{f.description}\n\n"
#             "С вами свяжется менеджер для уточнения деталей. 🏙"
#         )

#         if lang != "ru":
#             try:
#                 messages = [
#                     {"role": "system", "content": f"Translate the text to {lang}, keep numbers and names unchanged."},
#                     {"role": "user", "content": text_base}
#                 ]
#                 translation = client.chat.completions.create(
#                     model="gpt-4.1-mini",
#                     messages=messages
#                 )
#                 text_base = translation.choices[0].message.content.strip()
#             except Exception:
#                 pass

#         photo_val = normalize_url(f.plan.strip()) if getattr(f, "plan", None) else None
#         results.append({"text": text_base, "photo": photo_val})

#     return {"flats": results}


# # === Очистка истории ===
# def clear_user(user_id: int):
#     user_conversations[user_id].clear()
#     last_filters_cache.pop(user_id, None)
#     shown_flats_cache.pop(user_id, None)


import os
import json
import logging
import re
import asyncio
from collections import defaultdict
from urllib.parse import urlsplit, urlunsplit, quote
from dotenv import load_dotenv
from aiogram import Bot
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

    # 1–5 комнат
    if match := re.search(r'(\d+)\s*[- ]?\s*комнат', text, re.IGNORECASE):
        filters["rooms"] = int(match.group(1))

    # этаж
    if match := re.search(r'(\d+)\s*(?:этаж|этаже)', text, re.IGNORECASE):
        filters["stage"] = int(match.group(1))

    # цена до / максимум
    if match := re.search(r'(\d+[.,]?\d*)\s*(?:\$|доллар|тыс|тысяч)', text, re.IGNORECASE):
        price = float(match.group(1).replace(',', '.'))
        if 'тыс' in text.lower():
            price *= 1000
        filters["price_max"] = int(price)

    # тип
    low = text.lower()
    if 'магазин' in low:
        filters["type"] = "Магазин"
    elif 'студ' in low or '1 комнат' in low:
        filters["type"] = "Студия"
    else:
        filters["type"] = "Квартира"

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
    try:
        end_time = asyncio.get_event_loop().time() + duration
        while asyncio.get_event_loop().time() < end_time:
            await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(4)
    except Exception:
        pass


# === ОПРЕДЕЛЕНИЕ ЯЗЫКА ===
async def detect_language(text: str) -> str:
    try:
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": "Respond ONLY with one code: ru, en, uz, kk."},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=5,
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
    """
    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "Ты парсер фильтров недвижимости. "
                    "Верни только JSON без текста и комментариев. "
                    "Если данных нет — верни '{}'. "
                    "Поля: type (Квартира|Студия|Магазин), rooms (int), stage (int), "
                    "price_max (int), price_order (min|max)."
                ),
            },
            {"role": "user", "content": text},
        ]

        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=messages,
            temperature=0.1,
            max_tokens=200,
        )

        raw = resp.choices[0].message.content.strip()
        print("\n[GPT RAW FILTERS]:", raw, "\n")

        # убираем markdown-мусор ```json ```
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()

        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("GPT ответил не JSON")

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

    if filters.get("type"):
        query = query.filter(DBFlats.type == filters["type"])
    if filters.get("rooms"):
        query = query.filter(DBFlats.rooms == filters["rooms"])
    if filters.get("stage"):
        query = query.filter(DBFlats.stage == filters["stage"])
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
                    model="gpt-4.1-mini",
                    messages=[
                        {
                            "role": "system",
                            "content": f"Translate to {lang}, but keep numbers and building names unchanged.",
                        },
                        {"role": "user", "content": text_base},
                    ],
                    temperature=0,
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
