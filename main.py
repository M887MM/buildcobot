import asyncio
import csv
import html
import logging
import os
import inspect
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from aiogram import Bot, Dispatcher, exceptions, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramMigrateToChat
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputMediaPhoto, Message)
from dotenv import load_dotenv

from db import Product as DBProduct, Session
from text_utils import normalize_text
import openai_func
from sqlalchemy.orm import selectinload

# ====== Настройки ======
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")
try:
    GROUP_ID = int(GROUP_ID) if GROUP_ID else None
except Exception:
    GROUP_ID = None

if not BOT_TOKEN:
    logging.error("BOT_TOKEN не задан в окружении. Прерван запуск.")
    raise SystemExit("BOT_TOKEN is required")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Глобальная витрина товаров
Products: dict[int, dict] = {}

WELCOME_PHOTO_URL = (
    "https://images.unsplash.com/photo-1522335789203-aabd1fc54bc9"
    "?auto=format&fit=crop&w=800&q=80"
)
WELCOME_MESSAGE = (
    "💄 Добро пожаловать в LuxeBeauty!\n\n"
    "Чем помочь сегодня? Подберите уход, макияж, подарки или узнайте о доставке."
)

GOODS_OVERVIEW_TEXT = (
    "🛍️ Направления бутика LuxeBeauty:\n"
    "1. ✨ Уход за кожей лица и тела\n"
    "2. 💋 Макияж и аксессуары\n"
    "3. 🌸 Парфюмерия для неё и для него\n"
    "4. 💆‍♀️ Уход за волосами и стайлинг\n"
    "5. 🎁 Подарочные наборы и бьюти-боксы\n"
    "6. 🧴 Spa- и home-care коллекции\n\n"
    "Напишите, что ищете — предложу подбор или откройте каталог по кнопке ниже."
)


@contextmanager
def get_session():
    """Контекстный менеджер для безопасной работы с сессией SQLAlchemy."""
    session = Session()
    try:
        yield session
    finally:
        session.close()


async def load_products():
    """Загружает товары из базы данных и наполняет глобальный каталог."""
    global Products
    Products = {}
    placeholder_photo = "https://via.placeholder.com/600x400.png?text=No+Image"
    try:
        with get_session() as session:
            db_items = (
                session.query(DBProduct)
                .options(selectinload(DBProduct.category_obj))
                .order_by(DBProduct.name.asc())
                .all()
            )
            for idx, item in enumerate(db_items, start=1):
                Products[idx] = build_product_entry(item, idx, placeholder_photo)
    except Exception as exc:
        logging.exception("Ошибка загрузки товаров из БД: %s", exc)
        return
    logging.info("Загружено товаров: %d", len(Products))


def build_product_entry(item, idx: int, placeholder_photo: str) -> dict:
    pictures = list(getattr(item, "picture", []) or [])
    normalized_pictures = [
        normalize_text(photo) or placeholder_photo for photo in pictures if photo
    ]
    photo = normalized_pictures[0] if normalized_pictures else placeholder_photo
    return {
        "id": getattr(item, "id", idx),
        "name": normalize_text(getattr(item, "name", None)) or f"Товар #{idx}",
        "category": normalize_text(getattr(item, "category", None)) or "Категория не указана",
        "price": float(getattr(item, "price", 0) or 0),
        "old_price": getattr(item, "old_price", None),
        "description": normalize_text(getattr(item, "description", None)) or "",
        "tags": normalize_text(getattr(item, "tags", None)) or "",
        "pictures": normalized_pictures or [placeholder_photo],
        "photo": photo,
        "cached_file_id": None,
    }


async def send_welcome(chat_id: int):
    try:
        await bot.send_photo(
            chat_id,
            WELCOME_PHOTO_URL,
            caption=WELCOME_MESSAGE,
            reply_markup=start_keyboard(),
        )
    except Exception:
        await bot.send_message(chat_id, WELCOME_MESSAGE, reply_markup=start_keyboard())


# ====== FSM ======
class OrderState(StatesGroup):
    waiting_quantity = State()
    waiting_comment = State()
    sending_phone = State()

# ====== Хранилища в памяти ======
user_selection = {}         # per user: product, phone, username, name, quantity, comment, ...
manager_message_ids = {}

# ====== Кнопки и клавиатуры ======
def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🛍️ Товары", callback_data="start_products"),
                InlineKeyboardButton(text="🔥 Акции", callback_data="start_promos"),
            ],
            [
                InlineKeyboardButton(text="📂 Каталог", url="https://evrostroynks.uz"),
                InlineKeyboardButton(text="📍 Локация", callback_data="show_location"),
            ],
        ]
    )


def goods_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📂 Каталог", url="https://luxebeauty.uz"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад к меню", callback_data="back_to_menu"),
            ],
        ]
    )

# ====== Утилиты ======
def build_manager_message(user_id: int) -> str:
    sel = user_selection.get(user_id, {})
    name = sel.get("name") or "Неизвестно"
    phone = sel.get("phone") or "Не указан"
    product_idx = sel.get("product")
    quantity = sel.get("quantity")
    comment = sel.get("comment") or "—"

    # Диалог из openai_func.user_conversations
    try:
        conv = openai_func.user_conversations.get(user_id, [])
        if conv:
            dialog_lines = []
            for msg in conv:
                if isinstance(msg, dict):
                    role = msg.get("role", "user")
                    content = msg.get("content", "").strip()
                else:
                    # если просто строка, считаем сообщение клиента
                    role = "user"
                    content = str(msg).strip()

                if not content:
                    continue

                if role == "user":
                    dialog_lines.append(f"👤 Клиент: {content}")
                elif role == "assistant":
                    dialog_lines.append(f"🤖 Бот: {content}")
                else:
                    dialog_lines.append(f"{role}: {content}")

            # убираем дубликаты подряд идущих сообщений (если вдруг есть)
            dedup_dialog = []
            for line in dialog_lines:
                if not dedup_dialog or line != dedup_dialog[-1]:
                    dedup_dialog.append(line)

            dialog = "\n\n".join(dedup_dialog)
        else:
            dialog = "—"
    except Exception:
        dialog = "—"

    if len(dialog) > 3500:
        dialog = dialog[:3500].rstrip()
        dialog += "\n… (диалог сокращён)"

    dialog = html.escape(dialog)

    text = (
        f"👤 Имя: {name}\n"
        f"📞 Телефон: {phone}\n"
        f"📝 Комментарий: {comment}\n\n"
        f"💬 Диалог:\n"
        f"<pre>{dialog}</pre>"
    )

    text = text.replace("Shum", "")
    return text


def _extract_migrated_chat_id(error: TelegramMigrateToChat) -> Optional[int]:
    for attr_name in ("chat_id", "new_chat_id", "migrate_to_chat_id"):
        value = getattr(error, attr_name, None)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    params = getattr(error, "parameters", None)
    if params:
        value = getattr(params, "migrate_to_chat_id", None)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return None


def _apply_group_migration(new_chat_id: int):
    global GROUP_ID
    if GROUP_ID == new_chat_id:
        return
    logging.warning(
        "Группа менеджеров мигрировала в supergroup %s (старое значение %s). "
        "Обновите переменную окружения GROUP_ID.",
        new_chat_id,
        GROUP_ID,
    )
    GROUP_ID = new_chat_id
    os.environ["GROUP_ID"] = str(new_chat_id)
    manager_message_ids.clear()


async def _send_html_to_group(text: str) -> Optional[Message]:
    global GROUP_ID
    if not GROUP_ID:
        return None
    try:
        return await bot.send_message(GROUP_ID, text, parse_mode=ParseMode.HTML)
    except TelegramMigrateToChat as migrate_exc:
        new_chat_id = _extract_migrated_chat_id(migrate_exc)
        if new_chat_id is None:
            logging.exception("Не удалось обработать миграцию чата менеджеров.")
            return None
        _apply_group_migration(new_chat_id)
        try:
            return await bot.send_message(GROUP_ID, text, parse_mode=ParseMode.HTML)
        except Exception:
            logging.exception("Не удалось отправить сообщение в новую группу менеджеров.")
            return None
    except Exception:
        logging.exception("Не удалось отправить сообщение менеджерам.")
        return None


# ====== Менеджерское сообщение ======
async def send_or_update_manager_message(user_id: int):
    if not GROUP_ID:
        return
    text = build_manager_message(user_id)
    mid = manager_message_ids.get(user_id)
    if mid:
        try:
            await bot.edit_message_text(chat_id=GROUP_ID, message_id=mid, text=text, parse_mode=ParseMode.HTML)
            return
        except TelegramMigrateToChat as migrate_exc:
            new_chat_id = _extract_migrated_chat_id(migrate_exc)
            if new_chat_id is not None:
                _apply_group_migration(new_chat_id)
            manager_message_ids.pop(user_id, None)
        except Exception:
            logging.exception("Не удалось обновить менеджерское сообщение, отправляю новое.")
            manager_message_ids.pop(user_id, None)
    message = await _send_html_to_group(text)
    if message:
        manager_message_ids[user_id] = message.message_id
    else:
        logging.error("Не удалось отправить менеджерское сообщение.")


# ====== CSV ======
def persist_contact_to_csv(user_id: int, filename: str = "contacts.csv"):
    sel = user_selection.get(user_id, {})
    if not sel.get("phone"):
        return

    product_idx = sel.get("product")
    product_name = ""
    category_name = ""
    if product_idx and Products.get(product_idx):
        data = Products[product_idx]
        product_name = normalize_text(data.get("name")) or ""
        category_name = normalize_text(data.get("category")) or ""

    quantity = sel.get("quantity") or ""
    comment = sel.get("comment") or ""

    row = {
        "timestamp": datetime.utcnow().isoformat(),
        "user_id": user_id,
        "username": sel.get("username") or "",
        "name": sel.get("name") or "",
        "phone": sel.get("phone") or "",
        "product": product_name,
        "category": category_name,
        "quantity": quantity,
        "comment": comment,
    }
    file_exists = os.path.exists(filename)
    try:
        with open(filename, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
    except Exception as e:
        logging.exception("Ошибка сохранения контакта в CSV: %s", e)

# ====== Отложенная отправка ======
async def delayed_send_contact_to_managers(user_id: int, delay_seconds: int = 1 * 60):
    try:
        logging.info(f"[DELAY TASK] Задача запущена для user_id={user_id}, ждём {delay_seconds} сек...")
        await asyncio.sleep(delay_seconds)
        if not GROUP_ID:
            logging.warning("[DELAY TASK] GROUP_ID не задан, пропуск.")
            return
        sel = user_selection.get(user_id, {})
        phone = sel.get("phone")
        if not phone:
            logging.warning(f"[DELAY TASK] У пользователя {user_id} нет телефона, пропуск.")
            return

        manager_text = build_manager_message(user_id)
        full_text = "⏰ Повторное уведомление (через отложенное время):\n" + manager_text

        message = await _send_html_to_group(full_text)
        if message:
            logging.info(f"[DELAY TASK] Отложенное сообщение успешно отправлено в группу {GROUP_ID} для user_id={user_id}")
        else:
            logging.error("[DELAY TASK] Не удалось отправить отложенное уведомление менеджерам.")

        try:
            await send_or_update_manager_message(user_id)
        except Exception:
            pass
    except asyncio.CancelledError:
        return
    except Exception:
        logging.exception("Ошибка в delayed_send_contact_to_managers")


# ====== Запрос контакта ======
async def request_contact_prompt(message_or_obj, user_id):
    sel = user_selection.setdefault(user_id, {})
    if sel.get("phone_prompted") or sel.get("phone"):
        return
    sel["phone_prompted"] = True

    kb = types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="Отправить контакт", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    try:
        await message_or_obj.answer(
            "📲 Пожалуйста, отправьте ваш номер телефона (или нажмите 'Отправить контакт'):",
            reply_markup=kb
        )
    except Exception:
        try:
            chat_id = getattr(message_or_obj, "chat", None)
            if chat_id:
                chat_id = message_or_obj.chat.id
                await bot.send_message(
                    chat_id,
                    "📲 Пожалуйста, отправьте ваш номер телефона (или нажмите 'Отправить контакт'):",
                    reply_markup=kb
                )
        except Exception:
            logging.exception("Не удалось отправить запрос контакта.")


# ====== Helpers для безопасной отправки/редактирования фото и хранения display msg ======
def is_url(s: str) -> bool:
    return isinstance(s, str) and s.startswith(("http://", "https://"))

def _sanitize_media_value(value: str) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip().replace("\r", "").replace("\n", "")
    return cleaned

def _normalize_media_url(url: str) -> str:
    cleaned = _sanitize_media_value(url)
    if not cleaned:
        return cleaned
    try:
        parts = urlsplit(cleaned)
        path = quote(parts.path or "", safe="/%")
        query = quote(parts.query or "", safe="=&?")
        return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))
    except Exception:
        return cleaned

def _shorten(text: str, limit: int = 120) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."

def _describe_media_source(value) -> str:
    if isinstance(value, str):
        return _shorten(_sanitize_media_value(value))
    if isinstance(value, dict):
        return f"dict_keys={list(value.keys())}"
    return repr(value)

def prepare_photo_for_send(photo_value):
    """
    Возвращает:
      - ('url', url)
      - ('file', InputFile, path)
      - ('file_id', file_id)
      - None
    """
    if not photo_value:
        return None
    # already a file_id saved as string?
    if isinstance(photo_value, str):
        cleaned = _sanitize_media_value(photo_value)
        if not cleaned:
            return None
        if is_url(cleaned):
            return ('url', _normalize_media_url(cleaned))
        # локальный путь: делаем абсолютный путь
        p = os.path.expanduser(cleaned)
        p = os.path.abspath(p)
        if os.path.exists(p) and os.path.isfile(p):
            try:
                return ('file', types.InputFile(p), p)
            except Exception:
                logging.exception("Не удалось создать InputFile для %s", p)
                return None
        # может это уже file_id (не очень вероятно, но поддержим)
        if cleaned.isdigit() or '/' not in cleaned and len(cleaned) > 30:
            # heuristics: long token without slash could be file_id
            return ('file_id', cleaned)
        return None
    # If it's already aiogram InputFile
    if isinstance(photo_value, types.InputFile):
        return ('file', photo_value, None)
    return None

async def safe_send_and_store(chat_obj, user_id: int, photo_value, caption=None, reply_markup=None, product_index: int = None):
    chat_id = getattr(chat_obj, "chat", None)
    if chat_id:
        chat_id = chat_obj.chat.id
    else:
        chat_id = user_selection.get(user_id, {}).get("display_chat_id")
    if not chat_id:
        logging.warning("safe_send_and_store: нет chat_id, пропуск отправки")
        return None

    prepared = prepare_photo_for_send(photo_value)
    try:
        if prepared is None:
            logging.info("Отправка текстового сообщения. (печатает...)")
            # показать "печатает..."
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass
            sent = await bot.send_message(chat_id, text=caption or "(нет контента)", reply_markup=reply_markup)
        elif prepared[0] == "url" or prepared[0] == "file_id":
            logging.info("Отправка фото по URL/file_id. (печатает...)")
            try:
                await bot.send_chat_action(chat_id=chat_id, action="upload_photo")
            except Exception:
                pass
            sent = await bot.send_photo(chat_id, photo=prepared[1], caption=caption, reply_markup=reply_markup)
        elif prepared[0] == "file":
            logging.info("Отправка локального файла как фото. (печатает...)")
            try:
                await bot.send_chat_action(chat_id=chat_id, action="upload_photo")
            except Exception:
                pass
            sent = await bot.send_photo(chat_id, photo=prepared[1], caption=caption, reply_markup=reply_markup)
            # кэшируем file_id
            if product_index and getattr(sent, "photo", None):
                file_id = sent.photo[-1].file_id
                Products[product_index]["cached_file_id"] = file_id
                logging.info("Cached file_id for product %s", product_index)
        else:
            logging.info("Отправка fallback текста. (печатает...)")
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass
            sent = await bot.send_message(chat_id, text=caption or "(нет контента)", reply_markup=reply_markup)

        # сохраняем chat_id и message_id
        user_sel = user_selection.setdefault(user_id, {})
        user_sel["display_chat_id"] = sent.chat.id
        user_sel["display_msg_id"] = sent.message_id

        return sent
    except Exception:
        logging.exception("safe_send_and_store: ошибка при отправке")
        return None

async def try_edit_display_message(user_id: int, photo_value, caption=None, reply_markup=None, product_index: int = None):
    sel = user_selection.get(user_id, {})
    chat_id = sel.get("display_chat_id")
    msg_id = sel.get("display_msg_id")
    if not chat_id or not msg_id:
        return False

    prepared = prepare_photo_for_send(photo_value)
    try:
        if prepared is None:
            # только текст
            try:
                logging.info("Редактируем caption/text (печатает...)")
                # показать "печатает..."
                try:
                    await bot.send_chat_action(chat_id=chat_id, action="typing")
                except Exception:
                    pass
                await bot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=caption, reply_markup=reply_markup)
                return True
            except Exception:
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=caption, reply_markup=reply_markup)
                    return True
                except Exception:
                    return False

        kind = prepared[0]
        if kind in ("url", "file_id"):
            logging.info("Редактируем media на URL/file_id (печатает...)")
            media = InputMediaPhoto(media=prepared[1], caption=caption)
            await bot.edit_message_media(chat_id=chat_id, message_id=msg_id, media=media, reply_markup=reply_markup)
            return True

        if kind == "file":
            cached = Products.get(product_index, {}).get("cached_file_id") if product_index else None
            if cached:
                logging.info("Редактируем media используя закэшированный file_id (печатает...)")
                media = InputMediaPhoto(media=cached, caption=caption)
                await bot.edit_message_media(chat_id=chat_id, message_id=msg_id, media=media, reply_markup=reply_markup)
                return True
            else:
                try:
                    logging.info("Попытка редактирования caption для локального файла (печатает...)")
                    await bot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=caption, reply_markup=reply_markup)
                    return True
                except Exception:
                    return False

        return False
    except Exception:
        logging.exception("try_edit_display_message failed")
        return False


# ====== Хэндлеры ======
@dp.message(Command(commands=["start"]))
async def cmd_start(message: Message):
    if not Products:
        await message.answer("Каталог пока пуст. Пожалуйста, попробуйте позже.")
        return

    user_id = message.from_user.id
    sel = user_selection.setdefault(user_id, {})
    sel["at_menu"] = True
    index = sel.get("product", 1)
    if index not in Products:
        index = 1
    sel["display_chat_id"] = message.chat.id
    sel["product"] = index
    sel.pop("display_msg_id", None)

    await send_welcome(message.chat.id)


@dp.callback_query(lambda c: c.data and c.data.startswith("product_"))
async def cb_switch_product(cb: CallbackQuery):
    await send_product_overview(cb.message, cb.from_user.id)
    await cb.answer()

async def send_product_overview(message_obj: Message, user_id: int):
    sel = user_selection.setdefault(user_id, {})
    sel["at_menu"] = False
    sel["display_msg_id"] = None
    await message_obj.answer(GOODS_OVERVIEW_TEXT, reply_markup=goods_keyboard())


@dp.callback_query(lambda c: c.data == "start_products")
async def cb_start_products(cb: CallbackQuery):
    await send_product_overview(cb.message, cb.from_user.id)
    await cb.answer()


@dp.callback_query(lambda c: c.data == "start_promos")
async def cb_start_promos(cb: CallbackQuery):
    promo_text = (
        "🔥 Акции LuxeBeauty:\n"
        "• Скидка 15% на наборы ухода при покупке от двух позиций\n"
        "• Подарок — мини-парфюм при заказе от 500 000 сум\n"
        "• Бесплатная экспресс-доставка по Ташкенту от 300 000 сум\n\n"
        "Напишите, чтобы забронировать акционные товары!"
    )
    await cb.message.answer(promo_text)
    sel = user_selection.setdefault(cb.from_user.id, {})
    sel["at_menu"] = False
    await cb.answer()


@dp.callback_query(lambda c: c.data == "back_to_menu")
async def cb_back_to_menu(cb: CallbackQuery):
    user_id = cb.from_user.id
    sel = user_selection.setdefault(user_id, {})
    if sel.get("at_menu"):
        await cb.answer()
        return
    sel.pop("display_msg_id", None)
    sel["at_menu"] = True
    await send_welcome(cb.message.chat.id)
    await cb.answer()


@dp.callback_query(lambda c: c.data == "show_location")
async def cb_show_location(cb: CallbackQuery):
    location_text = (
        "📍 Наш шоурум:\n"
        "г. Ташкент, ТРЦ Riviera Plaza, 2 этаж\n"
        "🕘 Ежедневно: 10:00–22:00\n"
        "☎️ +998 90 555 44 33\n"
        "🌐 https://maps.google.com/maps?q=41.327546,69.281541&ll=41.327546,69.281541&z=16"
    )
    try:
        await cb.message.answer_location(latitude=41.327546, longitude=69.281541)
    except Exception:
        pass
    await cb.message.answer(location_text)
    await cb.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("order_"))
async def cb_order(cb: CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    try:
        idx = int(cb.data.split("_")[1])
    except Exception:
        idx = 1

    if idx not in Products:
        await cb.answer("Товар не найден")
        return

    sel = user_selection.setdefault(user_id, {})
    sel["product"] = idx

    if not sel.get("phone"):
        await request_contact_prompt(cb.message, user_id)
        await state.set_state(OrderState.sending_phone)
        await cb.answer()
        return

    await cb.message.answer(
        f"📦 Укажите количество для «{Products[idx].get('name', 'товар')}». "
        "Можно указать дробное значение и единицу измерения в сообщении, если это важно."
    )
    await state.set_state(OrderState.waiting_quantity)
    await cb.answer()


@dp.callback_query(lambda c: c.data == "request_phone")
async def cb_request_phone(cb: CallbackQuery, state: FSMContext):
    await request_contact_prompt(cb.message, cb.from_user.id)
    await state.set_state(OrderState.sending_phone)
    await cb.answer()


@dp.message(StateFilter(OrderState.waiting_quantity))
async def handle_quantity(message: Message, state: FSMContext):
    text = (message.text or "").replace(",", ".").strip()
    try:
        quantity_value = float(text)
        if quantity_value <= 0:
            raise ValueError
    except Exception:
        await message.answer("❌ Введите положительное число. Примеры: 10 или 7.5")
        return

    if quantity_value.is_integer():
        quantity_str = str(int(quantity_value))
    else:
        quantity_str = str(quantity_value)

    sel = user_selection.setdefault(message.from_user.id, {})
    sel["quantity"] = quantity_str

    await message.answer("📝 Добавьте комментарий к заказу или напишите «нет».")
    await state.set_state(OrderState.waiting_comment)


@dp.message(StateFilter(OrderState.waiting_comment))
async def handle_comment(message: Message, state: FSMContext):
    comment = (message.text or "").strip()
    if comment.lower() in {"нет", "no", "-", "без комментариев"}:
        comment = ""

    sel = user_selection.setdefault(message.from_user.id, {})
    sel["comment"] = comment

    persist_contact_to_csv(message.from_user.id)

    if GROUP_ID:
        await send_or_update_manager_message(message.from_user.id)

    await message.answer(
        "✅ Спасибо! Заявка отправлена менеджеру. Мы свяжемся с вами в ближайшее время.",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    await state.clear()


@dp.message(StateFilter(OrderState.sending_phone))
async def handle_phone(message: Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else (message.text or "").strip()

    phone = "".join(filter(str.isdigit, phone))
    if not (phone.isdigit() and 9 <= len(phone) <= 15):
        await message.answer("❌ Введите корректный номер телефона — только цифры, длиной от 9 до 15.")
        return

    sel = user_selection.setdefault(message.from_user.id, {})
    sel["phone"] = phone
    sel["username"] = message.from_user.username or ""
    sel["name"] = message.from_user.full_name or f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip()
    sel["phone_prompted"] = False

    persist_contact_to_csv(message.from_user.id)

    try:
        asyncio.create_task(delayed_send_contact_to_managers(message.from_user.id, delay_seconds=15 * 60))
    except Exception:
        logging.exception("Не удалось создать фоновую задачу для отложенной отправки контакта.")

    if sel.get("product") and GROUP_ID:
        await send_or_update_manager_message(message.from_user.id)

    await message.answer("✅ Спасибо! Номер сохранён. Теперь доступны все действия.", reply_markup=types.ReplyKeyboardRemove())
    await message.answer(
        "🇷🇺 Добро пожаловать в LuxeBeauty — бутик косметики и ухода.\n"
        "Я помогу подобрать уход за кожей, макияж, парфюмерию и подарочные наборы. Напишите о типе кожи, поводе или бюджете — подберу лучшие варианты.\n\n"
        "🇺🇿 LuxeBeauty go'zallik va parvarish butikiga xush kelibsiz.\n"
        "Men sizga teri parvarishi, makiyaj, atirlar va sovg'a to'plamlarini tanlashda yordam beraman. Teri turi, holat yoki byudjetni yozing — mos mahsulotlarni tavsiya qilaman.\n"
    )

    await state.clear()


@dp.message()
async def handle_question(message: Message, state: FSMContext):
    user_id = message.from_user.id

    if message.contact:
        await handle_phone(message, state)
        return

    sel = user_selection.setdefault(user_id, {})

    if not sel.get("phone"):
        await message.answer(
            "📲 Пожалуйста, сначала отправьте номер телефона, чтобы продолжить консультацию.",
            reply_markup=types.ReplyKeyboardMarkup(
                keyboard=[[types.KeyboardButton(text="Отправить контакт", request_contact=True)]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )
        await state.set_state(OrderState.sending_phone)
        return

    question = (message.text or "").strip()
    if not question:
        await message.answer("⚠️ Пустой вопрос. Напишите, какое средство или задачу нужно решить.")
        return

    try:
        logging.info("Пользователь запросил подбор косметики: %s", question)

        try:
            await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        except Exception:
            pass

        try:
            if not hasattr(openai_func, "user_conversations"):
                openai_func.user_conversations = defaultdict(list)
            openai_func.user_conversations[user_id].append(
                {"role": "user", "content": question}
            )
        except Exception:
            logging.exception("Не удалось сохранить сообщение пользователя в user_conversations.")

        response = await openai_func.ask_openai_sync(user_id, message.text, bot=bot, chat_id=message.chat.id)

        try:
            if isinstance(response, dict):
                meta = response.get("meta") or {}
                if meta.get("display_text") and response.get("text"):
                    openai_func.user_conversations[user_id].append(
                        {"role": "assistant", "content": response["text"]}
                    )

                if "products" in response:
                    for product in response["products"]:
                        content = product.get("text") or ""
                        if content:
                            openai_func.user_conversations[user_id].append(
                                {"role": "assistant", "content": content}
                            )
                elif "text" in response:
                    content = response.get("text") or ""
                    if content:
                        openai_func.user_conversations[user_id].append(
                            {"role": "assistant", "content": content}
                        )
                else:
                    openai_func.user_conversations[user_id].append(
                        {"role": "assistant", "content": str(response)}
                    )
            else:
                openai_func.user_conversations[user_id].append(
                    {"role": "assistant", "content": str(response)}
                )
        except Exception:
            logging.exception("Не удалось добавить ответ ассистента в user_conversations.")

        if isinstance(response, dict):
            if "products" in response:
                meta = response.get("meta") or {}
                if meta.get("display_text") and response.get("text"):
                    logging.info("Отправка текстового ответа с описанием (печатает...)")
                    try:
                        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
                    except Exception:
                        pass
                    await asyncio.sleep(1)
                    await bot.send_message(chat_id=message.chat.id, text=response["text"])

                for product in response["products"]:
                    logging.info("Отправка рекомендованного товара от GPT (печатает...)")
                    try:
                        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
                    except Exception:
                        pass
                    await asyncio.sleep(1)
                    text = product.get("text") or "Описание товара отсутствует."
                    photo = product.get("photo")
                    if product.get("product_index") and product["product_index"] in Products:
                        sel["product"] = product["product_index"]
                    if photo:
                        prepared = prepare_photo_for_send(photo)
                        if prepared:
                            try:
                                if prepared[0] in ("url", "file_id"):
                                    await bot.send_photo(chat_id=message.chat.id, photo=prepared[1], caption=text)
                                elif prepared[0] == "file":
                                    await bot.send_photo(chat_id=message.chat.id, photo=prepared[1], caption=text)
                                else:
                                    await bot.send_message(chat_id=message.chat.id, text=text)
                            except Exception:
                                logging.exception(
                                    "Не удалось отправить фото товара (%s), отправляем текст.",
                                    _describe_media_source(photo),
                                )
                                await bot.send_message(chat_id=message.chat.id, text=text)
                        else:
                            await bot.send_message(chat_id=message.chat.id, text=text)
                    else:
                        await bot.send_message(chat_id=message.chat.id, text=text)
            elif response.get("photo"):
                logging.info("Отправка фото-ответа от GPT (печатает...)")
                try:
                    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
                except Exception:
                    pass
                await asyncio.sleep(1)
                prepared = prepare_photo_for_send(response["photo"])
                if prepared:
                    try:
                        if prepared[0] in ("url", "file_id"):
                            await bot.send_photo(chat_id=message.chat.id, photo=prepared[1], caption=response.get("text", ""))
                        elif prepared[0] == "file":
                            await bot.send_photo(chat_id=message.chat.id, photo=prepared[1], caption=response.get("text", ""))
                        else:
                            await bot.send_message(chat_id=message.chat.id, text=response.get("text", str(response)))
                    except Exception:
                        logging.exception(
                            "Не удалось отправить фото-ответ (%s), отправляем текст.",
                            _describe_media_source(response.get("photo")),
                        )
                        await bot.send_message(chat_id=message.chat.id, text=response.get("text", str(response)))
                else:
                    await bot.send_message(chat_id=message.chat.id, text=response.get("text", str(response)))
            else:
                logging.info("Отправка текстового ответа от GPT (печатает...)")
                try:
                    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
                except Exception:
                    pass
                await asyncio.sleep(1)
                await bot.send_message(chat_id=message.chat.id, text=response.get("text", str(response)))
        else:
            logging.info("Отправка простого ответа от GPT (печатает...)")
            try:
                await bot.send_chat_action(chat_id=message.chat.id, action="typing")
            except Exception:
                pass
            await asyncio.sleep(1)
            await bot.send_message(chat_id=message.chat.id, text=str(response))

        if GROUP_ID:
            await send_or_update_manager_message(user_id)

    except Exception as e:
        err = str(e)
        if "401" in err or "api key" in err.lower():
            answer = "⚠️ Ошибка авторизации OpenAI (401). Проверьте API_KEY."
        else:
            answer = f"⚠️ Ошибка GPT: {e}"
        await message.answer(answer, reply_markup=start_keyboard())


# ====== Запуск бота ======
async def main():
    await load_products()
    logging.info("Бот загружен и готов. Запуск polling...")
    async def _shutdown():
        storage = getattr(dp, "storage", None)
        if storage:
            for method_name in ("close", "wait_closed"):
                method = getattr(storage, method_name, None)
                if callable(method):
                    result = method()
                    if inspect.isawaitable(result):
                        await result
        session = getattr(bot, "session", None)
        if session:
            close_method = getattr(session, "close", None)
            if callable(close_method):
                result = close_method()
                if inspect.isawaitable(result):
                    await result

    while True:
        try:
            await dp.start_polling(bot)
        except exceptions.TelegramNetworkError:
            logging.warning("❌ Ошибка сети Telegram, переподключаемся через 3 сек...")
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            logging.info("⏹️ Получен сигнал остановки polling, завершаем работу.")
            await _shutdown()
            return
        except KeyboardInterrupt:
            logging.info("🛑 Бот остановлен вручную")
            await _shutdown()
            return
        except Exception:
            logging.exception("Неожиданная ошибка в polling, перезапуск через 3 сек...")
            await asyncio.sleep(5)
    await _shutdown()

async def show_typing(bot, chat_id: int):
    """Постоянно показывает 'печатает...', пока задача не отменена"""
    try:
        while True:
            await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(5)  # обновляет статус каждые 5 сек
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("🛑 Запуск остановлен пользователем.")
    except asyncio.CancelledError:
        logging.info("⏹️ Поллинг отменён и приложение завершено.")
