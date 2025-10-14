import asyncio
import os
import re
import logging
from collections import Counter
import csv
from aiogram.enums import ParseMode
from datetime import datetime
from aiogram.filters import Command
from aiogram import Bot, Dispatcher, types, exceptions
from aiogram.filters import CommandStart, StateFilter
from contextlib import contextmanager
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message, InputMediaPhoto
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
from collections import defaultdict

from db import Session, Flats as DBFlats , Credit, Flats 
import openai_func

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

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ====== Загрузка квартир из БД ======
import logging
from contextlib import contextmanager
from db import Session, Flats as DBFlats

# Глобальная переменная для хранения квартир
Flats = {}

credit_works = False

@contextmanager
def get_session():
    """
    Контекстный менеджер для безопасной работы с сессией SQLAlchemy.
    Гарантирует закрытие сессии после использования.
    """
    session = Session()
    try:
        yield session
    finally:
        session.close()

async def load_flats():
    """
    Загружает квартиры из базы данных и сохраняет в глобальный словарь Flats.
    Если база пуста или произошла ошибка, используется заглушка.
    """
    global Flats
    try:
        with get_session() as session:
            db_flats = session.query(DBFlats).filter(DBFlats.status == "Свободно").all()
    except Exception as e:
        logging.exception("Ошибка загрузки квартир из БД: %s", e)
        db_flats = []

    # Заглушка, если база пуста
    if not db_flats:
        class _Stub:
            number = "N/A"
            rooms = 1
            sq_m = 30
            price = 10000
            stage = 1
            plan = "https://via.placeholder.com/600x400.png?text=No+Flats+in+DB"
        db_flats = [_Stub()]

    Flats = {
        i + 1: {
            "type": flat.type,
            "rooms": flat.rooms,
            "area": getattr(flat, "sq_m", None),
            "price": getattr(flat, "price", 0),
            "stage": getattr(flat, "stage", None),
            "photo": getattr(flat, "plan", None) or "https://via.placeholder.com/600x400.png?text=No+Image",
            "raw": flat,
            "cached_file_id": None
        }
        for i, flat in enumerate(sorted(db_flats, key=lambda f: f.number))
    }
    logging.info("Загружено квартир: %d", len(Flats))


# ====== FSM ======
class CreditState(StatesGroup):
    waiting_downpayment = State()
    waiting_term = State()
    asking_question = State()
    sending_phone = State()

# ====== Хранилища в памяти ======
user_selection = {}         # per user: flat, phone, username, name, display_chat_id, display_msg_id, ...
manager_message_ids = {}

# ====== Кнопки и клавиатуры ======
def main_keyboard(index: int, has_contact: bool) -> InlineKeyboardMarkup:
    nav_row = [
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"flat_{index-1}"),
        InlineKeyboardButton(text="➡️ Вперёд", callback_data=f"flat_{index+1}")
    ]
    choose_row = [
        InlineKeyboardButton(text="🔍 Подробнее", callback_data=f"choose_{index}")
    ]
    extra_row = [
        InlineKeyboardButton(text="📋 Список квартир", callback_data="show_list")
    ]
    return InlineKeyboardMarkup(inline_keyboard=[nav_row, choose_row, extra_row])

def choose_keyboard(index: int, has_contact: bool = False) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(text="💳 Рассчитать в кредит", callback_data=f"calc_{index}"),
    ]
    back = [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="back_to_list")]

    keyboard = [row, back]

    if not has_contact:
        contact_row = [
            InlineKeyboardButton(text="📲 Отправить контакт", callback_data="request_phone")
        ]
        keyboard.insert(1, contact_row)

    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def result_keyboard(flat_index: int = 1) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(text="🔄 Новый расчёт", callback_data="new_calc"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])

def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_flats")]])

# ====== Утилиты ======
def flat_caption(index: int) -> str:
    f = Flats.get(index)
    if not f:
        return "🏠 Квартира недоступна"
    flat_type = f.get("type")
    price = f.get("price") or 0
    price_str = f"{price:,}".replace(",", " ")
    raw = f.get("raw")
    number = getattr(raw, "number", "?") if raw else "?"
    return (f"🏠 {flat_type} {index} (№{number})\n"
            f"• Комнат: {f.get('rooms', '—')}\n"
            f"• Площадь: {f.get('area', '—')} м²\n"
            f"• Этаж: {f.get('stage', '—')}\n"
            f"• Цена: {price_str} $")


def calc_credit(price: int, percent: int, months: int):
    rate = 0.25 / 12
    initial = price * percent / 100
    loan = price - initial
    payment = 0 if months == 0 else loan * (rate * (1 + rate) ** months) / ((1 + rate) ** months - 1)
    total = payment * months
    overpay = total - loan
    return int(initial), int(loan), int(round(payment)), int(round(total)), int(round(overpay))

def build_manager_message(user_id: int) -> str:
    sel = user_selection.get(user_id, {})
    name = sel.get("name") or "Неизвестно"
    phone = sel.get("phone") or "Не указан"
    flat_idx = sel.get("flat")

    # Инфо о квартире
    if flat_idx and Flats.get(flat_idx):
        raw = Flats[flat_idx]["raw"]
        number = getattr(raw, "number", "?")
        rooms = getattr(raw, "rooms", "?")
        sq_m = getattr(raw, "sq_m", "?")
        price = getattr(raw, "price", "?")
        flat_line = f"🏠 Квартира №{number}\nКомнат: {rooms}, Площадь: {sq_m} м², Цена: {price}$"
    else:
        flat_line = "🏠 Квартира: не выбрана"

    # Диалог из openai_func.user_conversations
    try:
        conv = openai_func.user_conversations.get(user_id, [])
        if conv:
            dialog_lines = []
            for msg in conv:
                role = msg.get("role", "user")
                content = msg.get("content", "").strip()
                if not content:
                    continue
                if role == "user":
                    dialog_lines.append(f"👤 Клиент: {content}")
                elif role == "assistant":
                    dialog_lines.append(f"🤖 Бот: {content}")
                else:
                    dialog_lines.append(f"{role}: {content}")
            dialog = "\n\n".join(dialog_lines)
        else:
            dialog = "—"
    except Exception:
        dialog = "—"

    text = (
        f"👤 Имя: {name}\n"
        f"📞 Телефон: {phone}\n"
        f"{flat_line}\n\n"
        f"💬 Диалог с GPT:\n"
        f"<pre>{dialog}</pre>"
    )

    text = text.replace("Shum", "")
    return text


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
        except Exception:
            pass
    try:
        sent = await bot.send_message(GROUP_ID, text, parse_mode=ParseMode.HTML)
        manager_message_ids[user_id] = sent.message_id
    except Exception:
        logging.exception("Не удалось отправить менеджерское сообщение.")


# ====== CSV ======
def persist_contact_to_csv(user_id: int, filename: str = "contacts.csv"):
    sel = user_selection.get(user_id, {})
    if not sel.get("phone"):
        return
    row = {
        "timestamp": datetime.utcnow().isoformat(),
        "user_id": user_id,
        "username": sel.get("username") or "",
        "name": sel.get("name") or "",
        "phone": sel.get("phone") or "",
        "flat": sel.get("flat") or "",
        "downpayment": sel.get("downpayment") or "",
        "months": sel.get("months") or "",
        "payment": sel.get("payment") or ""
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

        try:
            await bot.send_message(GROUP_ID, full_text, parse_mode=ParseMode.HTML)
            logging.info(f"[DELAY TASK] Отложенное сообщение успешно отправлено в группу {GROUP_ID} для user_id={user_id}")
        except Exception:
            logging.exception("Не удалось отправить отложенное уведомление менеджерам.")

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
        if is_url(photo_value):
            return ('url', photo_value)
        # локальный путь: делаем абсолютный путь
        p = os.path.expanduser(photo_value)
        p = os.path.abspath(p)
        if os.path.exists(p) and os.path.isfile(p):
            try:
                return ('file', types.InputFile(p), p)
            except Exception:
                logging.exception("Не удалось создать InputFile для %s", p)
                return None
        # может это уже file_id (не очень вероятно, но поддержим)
        if photo_value.isdigit() or '/' not in photo_value and len(photo_value) > 30:
            # heuristics: long token without slash could be file_id
            return ('file_id', photo_value)
        return None
    # If it's already aiogram InputFile
    if isinstance(photo_value, types.InputFile):
        return ('file', photo_value, None)
    return None

async def safe_send_and_store(chat_obj, user_id: int, photo_value, caption=None, reply_markup=None, flat_index: int = None):
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
            if flat_index and getattr(sent, "photo", None):
                file_id = sent.photo[-1].file_id
                Flats[flat_index]["cached_file_id"] = file_id
                logging.info("Cached file_id for flat %s", flat_index)
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

async def try_edit_display_message(user_id: int, photo_value, caption=None, reply_markup=None, flat_index: int = None):
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
            cached = Flats.get(flat_index, {}).get("cached_file_id") if flat_index else None
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
    user_id = message.from_user.id
    index = 1
    has_contact = bool(user_selection.get(user_id, {}).get("phone"))

    # Сохраняем chat_id сразу
    user_sel = user_selection.setdefault(user_id, {})
    user_sel["display_chat_id"] = message.chat.id

    # Проверяем, что фото есть, иначе используем заглушку
    flat = Flats.get(index)
    if not flat:
        flat = {"photo": "https://via.placeholder.com/600x400.png?text=No+Image", "raw": None, "rooms": "—", "area": "—", "price": 0, "stage": "—"}

    # Отправляем и сохраняем display message
    sent = await safe_send_and_store(
        message,
        user_id,
        flat["photo"],
        caption=flat_caption(index),
        reply_markup=main_keyboard(index, has_contact),
        flat_index=index
    )

    if sent is None:
        # fallback: отправляем простой текст
        await message.answer(flat_caption(index), reply_markup=main_keyboard(index, has_contact))


@dp.callback_query(lambda c: c.data and c.data.startswith("flat_"))
async def cb_switch_flat(cb: CallbackQuery):
    user_id = cb.from_user.id
    try:
        idx = int(cb.data.split("_")[1])
    except Exception:
        idx = 1
    if idx < 1:
        idx = len(Flats)
    if idx > len(Flats):
        idx = 1
    has_contact = bool(user_selection.get(user_id, {}).get("phone"))

    # Пытаемся редактировать ранее отправленное сообщение
    edited = await try_edit_display_message(user_id, Flats[idx]["photo"], caption=flat_caption(idx), reply_markup=main_keyboard(idx, has_contact), flat_index=idx)
    if not edited:
        # fallback: отправим новое сообщение и сохраним его id (при отправке локального файла будем кэшировать file_id)
        await safe_send_and_store(cb.message, user_id, Flats[idx]["photo"], caption=flat_caption(idx), reply_markup=main_keyboard(idx, has_contact), flat_index=idx)

    await cb.answer()

@dp.callback_query(lambda c: c.data == "show_list")
async def cb_show_list(cb: CallbackQuery):
    user_id = cb.from_user.id
    if not user_selection.get(user_id, {}).get("phone"):
        await request_contact_prompt(cb.message, user_id)
        await cb.answer()
        return
    rooms_counter = Counter([v["rooms"] for v in Flats.values()])
    text = "📋 Список квартир:\n" + "\n".join([f"{rooms}-комнатных: {count} шт" for rooms, count in sorted(rooms_counter.items())])
    edited = await try_edit_display_message(user_id, photo_value=None, caption=text, reply_markup=back_keyboard())
    if not edited:
        await safe_send_and_store(cb.message, user_id, None, caption=text, reply_markup=back_keyboard())
    await cb.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("choose_"))
async def cb_choose(cb: CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    idx = int(cb.data.split("_")[1])
    sel = user_selection.setdefault(user_id, {})
    sel["flat"] = idx
    has_phone = bool(sel.get("phone"))

    edited = await try_edit_display_message(user_id, Flats[idx]["photo"], caption=flat_caption(idx), reply_markup=choose_keyboard(idx, has_contact=has_phone), flat_index=idx)
    if not edited:
        await safe_send_and_store(cb.message, user_id, Flats[idx]["photo"], caption=flat_caption(idx), reply_markup=choose_keyboard(idx, has_contact=has_phone), flat_index=idx)

    if not has_phone:
        await request_contact_prompt(cb.message, user_id)
        await state.set_state(CreditState.sending_phone)

    if has_phone and GROUP_ID:
        await send_or_update_manager_message(user_id)

    await cb.answer()

@dp.callback_query(lambda c: c.data == "back_to_list")
async def cb_back_to_list(cb: CallbackQuery):
    user_id = cb.from_user.id
    idx = user_selection.get(user_id, {}).get("flat", 1)
    has_contact = bool(user_selection.get(user_id, {}).get("phone"))

    edited = await try_edit_display_message(user_id, Flats[idx]["photo"], caption=flat_caption(idx), reply_markup=main_keyboard(idx, has_contact), flat_index=idx)
    if not edited:
        await safe_send_and_store(cb.message, user_id, Flats[idx]["photo"], caption=flat_caption(idx), reply_markup=main_keyboard(idx, has_contact), flat_index=idx)
    await cb.answer()

# ====== Новый хэндлер расчёта кредита ======
@dp.callback_query(lambda c: c.data and c.data.startswith("calc_"))
async def cb_calc(cb: CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    idx = int(cb.data.split("_")[1])
    sel = user_selection.setdefault(user_id, {})
    sel["flat"] = idx

    # Проверяем телефон
    if not sel.get("phone"):
        await request_contact_prompt(cb.message, user_id)
        await state.set_state(CreditState.sending_phone)
        await cb.answer()
        return

    flat = Flats.get(idx)
    if not flat:
        await cb.message.answer("❌ Квартира не найдена.")
        await cb.answer()
        return

    # Сохраняем стандартный первоначальный взнос 20% (можно изменить)
    sel["downpayment"] = 20

    # Запрашиваем только срок кредита
    await cb.message.answer(
        f"💳 Рассчёт кредита для квартиры №{getattr(flat['raw'], 'number', '?')} ({flat['rooms']} комн., {flat['area']} м², {flat['price']}$)\n\n"
        "Пожалуйста, введите срок кредита в месяцах или годах (например: 6мес или 2 года):"
    )
    await state.set_state(CreditState.waiting_term)
    await cb.answer()


# ====== Хэндлеры для срока и downpayment (оставлены как были) ======
@dp.message(StateFilter(CreditState.waiting_term))
async def set_term(message: Message, state: FSMContext):
    text = message.text.strip().lower()
    months = None

    if text.isdigit():
        n = int(text)
        if n < 6:
            months = n * 12  # меньше 6 — это годы
        else:
            months = n       # 6 и больше — это месяцы
    else:
        match = re.match(r"(\d+)\s*(год|года|лет)", text)
        if match:
            months = int(match.group(1)) * 12
        else:
            match = re.match(r"(\d+)\s*мес", text)
            if match:
                months = int(match.group(1))

    if not months or months < 6 or months > 60*12:
        await message.answer("⚠️ Введите корректный срок (от 6 мес до 5 лет).")
        return

    sel = user_selection.get(message.from_user.id, {})
    flat_idx = sel.get("flat", 1)
    percent = sel.get("downpayment", 20)
    flat = Flats.get(flat_idx, Flats[1])

    initial, loan, payment, total, overpay = calc_credit(flat["price"], percent, months)
    sel.update({
        "months": months,
        "payment": payment
    })

    result_text = (
        f"🏠 Квартира: №{getattr(flat['raw'], 'number', '?')} ({flat['rooms']} комн.)\n"
        f"💰 Первоначальный взнос: {percent}% ({initial}$)\n"
        f"📅 Срок: {months} мес\n"
        f"🏦 Сумма кредита: {loan}$\n"
        f"💵 Ежемесячный платёж: {payment}$\n"
        f"📊 Общая выплата: {total}$\n"
    )

    await message.answer(result_text, reply_markup=result_keyboard(flat_idx))
    await state.clear()


@dp.message(StateFilter(CreditState.waiting_downpayment))
async def set_downpayment(message: Message, state: FSMContext):
    try:
        percent = int(message.text.strip())
        if percent < 0 or percent > 100:
            await message.answer("❌ Введите корректный процент (от 0 до 100).")
            return
    except Exception:
        await message.answer("⚠️ Введите число, например: 20")
        return

    sel = user_selection.setdefault(message.from_user.id, {})
    sel["downpayment"] = percent

    await message.answer("Теперь введите срок кредита (от 6 мес до 5 лет). Например: 2 года или 24 мес.")
    await state.set_state(CreditState.waiting_term)


@dp.message(StateFilter(CreditState.waiting_term))
async def set_term_2(message: Message, state: FSMContext):
    text = message.text.strip().lower()
    months = None
    if text.isdigit():
        months = int(text)
    else:
        match = re.match(r"(\d+)\s*(год|года|лет)", text)
        if match:
            months = int(match.group(1)) * 12
    if not months or months < 6 or months > 360:
        await message.answer("⚠️ Введите корректный срок (от 6 до 60 месяцев).")
        return
    sel = user_selection.get(message.from_user.id, {})
    flat_idx = sel.get("flat", 1)
    percent = sel.get("downpayment", 20)
    flat = Flats.get(flat_idx, Flats[1])
    initial, loan, payment, total, overpay = calc_credit(flat["price"], percent, months)
    sel.update({
        "months": months,
        "payment": payment
    })
    result_text = (
        f"🏠 Квартира: №{getattr(flat['raw'], 'number', '?')} ({flat['rooms']} комн.)\n"
        f"💰 Первоначальный взнос: {percent}% ({initial}$)\n"
        f"📅 Срок: {months} мес\n"
        f"🏦 Сумма кредита: {loan}$\n"
        f"💵 Ежемесячный платёж: {payment}$\n"
        f"📊 Общая выплата: {total}$\n"
    )
    await message.answer(result_text, reply_markup=result_keyboard(flat_idx))
    await state.clear()

@dp.callback_query(lambda c: c.data == "new_calc")
async def cb_new_calc(cb: CallbackQuery):
    user_id = cb.from_user.id
    idx = user_selection.get(user_id, {}).get("flat", 1)
    has_contact = bool(user_selection.get(user_id, {}).get("phone"))
    edited = await try_edit_display_message(user_id, Flats[idx]["photo"], caption=flat_caption(idx), reply_markup=main_keyboard(idx, has_contact), flat_index=idx)
    if not edited:
        await safe_send_and_store(cb.message, user_id, Flats[idx]["photo"], caption=flat_caption(idx), reply_markup=main_keyboard(idx, has_contact), flat_index=idx)
    await cb.answer()

@dp.callback_query(lambda c: c.data == "back_to_flats")
async def cb_back_to_flats(cb: CallbackQuery):
    user_id = cb.from_user.id
    idx = user_selection.get(user_id, {}).get("flat", 1)
    has_contact = bool(user_selection.get(user_id, {}).get("phone"))
    edited = await try_edit_display_message(user_id, Flats[idx]["photo"], caption=flat_caption(idx), reply_markup=main_keyboard(idx, has_contact), flat_index=idx)
    if not edited:
        await safe_send_and_store(cb.message, user_id, Flats[idx]["photo"], caption=flat_caption(idx), reply_markup=main_keyboard(idx, has_contact), flat_index=idx)
    await cb.answer()

@dp.message(StateFilter(CreditState.sending_phone))
async def handle_phone(message: Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text.strip()

    # ✅ Проверка: оставляем только цифры
    phone = "".join(filter(str.isdigit, phone))

    # ✅ Проверка: только цифры и длина 9–15
    if not (phone.isdigit() and 9 <= len(phone) <= 15):
        await message.answer("❌ Введите корректный номер телефона — только цифры, длиной от 9 до 15.")
        return

    # ⬇️ остальной твой код без изменений
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

    if sel.get("flat") and GROUP_ID:
        await send_or_update_manager_message(message.from_user.id)

    await message.answer("✅ Спасибо! Номер сохранён. Теперь доступны все действия.", reply_markup=types.ReplyKeyboardRemove())
    await message.answer(
    "🇷🇺 Добро пожаловать в Royal Residence.\n"
    "Я — ваш AI-менеджер по продажам, созданный, чтобы помочь вам найти идеальную квартиру.\n"
    "Подберу лучшие варианты, покажу планировки и помогу рассчитать ипотеку.\n\n"
    "Пожалуйста, уточните ваши пожелания — количество комнат, этаж, площадь или бюджет.\n\n"
    "Это поможет подобрать самые подходящие варианты 🏙 \n\n"
    "С чего начнём поиск?\n\n"

    "🇺🇿 Royal Residence-ga xush kelibsiz.\n"
    "Men sizning AI savdo menejeringizman, u sizga mukammal kvartirani topishga yordam berish uchun yaratilgan.\n"
    "Men eng yaxshi variantlarni tanlayman, sizga qavat rejalarini ko'rsataman va ipotekani hisoblashingizga yordam beraman.\n\n"

    "Iltimos, afzalliklaringizni belgilang - xonalar soni, qavat, maydon va byudjet.\n\n"

    "Bu sizga eng mos variantlarni topishga yordam beradi 🏙\n\n"

    "Qidiruvni qayerdan boshlaymiz?\n\n"
)

    await state.clear()

@dp.message()
async def handle_question(message: Message, state: FSMContext):
    user_id = message.from_user.id
    sel = user_selection.setdefault(user_id, {})

    # 🚫 Проверяем — если нет телефона
    if not sel.get("phone"):
        await message.answer(
            "📲 Пожалуйста, сначала отправьте ваш номер телефона, чтобы продолжить.",
            reply_markup=types.ReplyKeyboardMarkup(
                keyboard=[[types.KeyboardButton(text="Отправить контакт", request_contact=True)]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        await state.set_state(CreditState.sending_phone)
        return

    question = message.text.strip()
    if not question:
        await message.answer("⚠️ Пустой вопрос. Напишите текст.")
        return

    try:
        # 🔹 GPT-ответ с фото
        logging.info("Пользователь запросил у GPT: %s", question)

        # --- сразу показываем "печатает..." ---
        try:
            await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        except Exception:
            pass

        # --- Сохраняем сообщение пользователя в истории (чтобы диалог был полным) ---
        try:
            # гарантируем, что openai_func.user_conversations существует и это dict-like
            if not hasattr(openai_func, "user_conversations"):
                openai_func.user_conversations = defaultdict(list)
            openai_func.user_conversations[message.from_user.id].append({
                "role": "user",
                "content": question
            })
        except Exception:
            logging.exception("Не удалось сохранить сообщение пользователя в user_conversations.")

        # ---- ВАЖНО: ask_openai_sync в openai_func — асинхронная функция, поэтому ждём результат с await ----
        response = await openai_func.ask_openai_sync(user_id, message.text, bot=bot, chat_id=message.chat.id)

        # --- После получения ответа сохраняем ответы ассистента в истории ---
        try:
            # Нормализуем разные типы ответов и добавляем как assistant messages
            if isinstance(response, dict):
                # Если есть flats -> добавляем каждый текст ответа
                if "flats" in response:
                    for flat in response["flats"]:
                        content = flat.get("text") or ""
                        if content:
                            openai_func.user_conversations[message.from_user.id].append({
                                "role": "assistant",
                                "content": content
                            })
                elif "text" in response:
                    content = response.get("text") or ""
                    if content:
                        openai_func.user_conversations[message.from_user.id].append({
                            "role": "assistant",
                            "content": content
                        })
                else:
                    # fallback: stringify dict
                    openai_func.user_conversations[message.from_user.id].append({
                        "role": "assistant",
                        "content": str(response)
                    })
            else:
                # response could be plain string
                openai_func.user_conversations[message.from_user.id].append({
                    "role": "assistant",
                    "content": str(response)
                })
        except Exception:
            logging.exception("Не удалось добавить ответ ассистента в user_conversations.")

        # --- Отправляем ответ пользователю (в зависимости от структуры) ---
        if isinstance(response, dict):
            if "flats" in response:
                for flat in response["flats"]:
                    logging.info("Отправка варианта квартиры от GPT (печатает...)")
                    try:
                        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
                    except Exception:
                        pass
                    logging.info("печатает...")
                    # небольшая пауза, чтобы статус видел пользователь
                    await asyncio.sleep(1)
                    if flat.get("photo"):
                        await bot.send_photo(chat_id=message.chat.id, photo=flat["photo"], caption=flat["text"])
                    else:
                        await bot.send_message(chat_id=message.chat.id, text=flat["text"])
            elif response.get("photo"):
                logging.info("Отправка фото-ответа от GPT (печатает...)")
                try:
                    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
                except Exception:
                    pass
                logging.info("печатает...")
                await asyncio.sleep(1)
                await bot.send_photo(chat_id=message.chat.id, photo=response["photo"], caption=response.get("text", ""))
            else:
                logging.info("Отправка текст-ответа от GPT (печатает...)")
                try:
                    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
                except Exception:
                    pass
                logging.info("печатает...")
                await asyncio.sleep(1)
                await bot.send_message(chat_id=message.chat.id, text=response.get("text", str(response)))
        else:
            logging.info("Отправка простого ответа от GPT (печатает...)")
            try:
                await bot.send_chat_action(chat_id=message.chat.id, action="typing")
            except Exception:
                pass
            logging.info("печатает...")
            await asyncio.sleep(1)
            await bot.send_message(chat_id=message.chat.id, text=str(response))

        # Обновляем менеджерское сообщение (если нужно)
        if GROUP_ID:
            await send_or_update_manager_message(message.from_user.id)


    except Exception as e:
        err = str(e)
        if "401" in err or "api key" in err.lower():
            answer = "⚠️ Ошибка авторизации OpenAI (401). Проверьте API_KEY."
        else:
            answer = f"⚠️ Ошибка GPT: {e}"
        await message.answer(answer, reply_markup=back_keyboard())


# ====== Запуск бота ======
async def main():
    await load_flats()
    logging.info("Бот загружен и готов. Запуск polling...")
    while True:
        try:
            await dp.start_polling(bot)
        except exceptions.TelegramNetworkError:
            logging.warning("❌ Ошибка сети Telegram, переподключаемся через 3 сек...")
            await asyncio.sleep(5)
        except KeyboardInterrupt:
            logging.info("🛑 Бот остановлен вручную")
            break
        except Exception:
            logging.exception("Неожиданная ошибка в polling, перезапуск через 3 сек...")
            await asyncio.sleep(5)

async def show_typing(bot, chat_id: int):
    """Постоянно показывает 'печатает...', пока задача не отменена"""
    try:
        while True:
            await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(5)  # обновляет статус каждые 5 сек
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
