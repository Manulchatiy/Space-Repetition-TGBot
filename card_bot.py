import asyncio
import random
import re
import aiosqlite
import os
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
from db import DB_PATH

from db import (
    init_db, add_card, get_card, delete_card, update_card,
    get_due_cards, process_review_result, record_rep_command,
    get_user_stats_data, get_user_subjects, get_user_cards_count, get_last_subject
)

load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
MSK = timezone(timedelta(hours=3))
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- НОВЫЕ ФОТО ID ИЗ ТЗ ---
PHOTO_START = "AgACAgIAAxkBAAPhauOnRD9hEyOx_GwzR_rmn2L_bPMAAnInaxsIZSFLwAi4KsoqpQsBAAMCAAN5AAM9BA"
PHOTO_SUBJECT = "AgACAgIAAxkBAAPwauOodjoUMjhmWVKiL2OzaTLofgcAApYnaxsIZSFLc-T4km9FQDQBAAMCAAN5AAM9BA"
PHOTO_REP_EMPTY = "AgACAgIAAxkBAAObauOeH7XGhHbDBeXZSwV0R8wTbdsAAnUnaxsIZSFLq012E8cPC80BAAMCAAN5AAM9BA"
PHOTO_REP_LOADED = "AgACAgIAAxkBAAP0auOokGTnu2GXMx4AAeYPA2Vur76XAAJ2J2sbCGUhS71fsXdhGSCNAQADAgADeQADPQQ"
PHOTO_STAT = "AgACAgIAAxkBAANoauOYXVZ57sASymuoy3hWZ3f7iJ8AAncnaxsIZSFLkwmX_GUe8b0BAAMCAAN4AAM9BA"
PHOTO_ID_PROMPT = "AgACAgIAAxkBAAP4auOoqK9C8050tISvuoxjYh-6Y9AAAngnaxsIZSFLQ39hZU4DYLoBAAMCAAN5AAM9BA"


# --- FSM СОСТОЯНИЯ ---
class CardCreation(StatesGroup):
    subject_choice = State()
    new_subject_input = State()
    front_side = State()
    question = State()
    check_type = State()
    correct_answer = State()
    options = State()
    back_side = State()

class CardEdit(StatesGroup):
    field_choice = State()
    new_value = State()

class FastCreate(StatesGroup):
    subject_choice = State()      # выбор предмета кнопками
    new_subject_input = State()   # ввод нового предмета
    waiting_content = State()     # ожидание 4 строк


# --- КЛАВИАТУРЫ ГЛАВНОГО МЕНЮ ---
def get_main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Создать карточку для повторений", callback_data="menu_create")],
        [InlineKeyboardButton(text="🔄 Повторить истекшие", callback_data="menu_rep")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="menu_stat")]
    ])

# --- КОМАНДЫ ---
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer_photo(
        photo=PHOTO_START,
        caption="Это бот для интервальных повторений...",
        reply_markup=get_main_menu_kb()
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "<b>Список команд:</b>\n"
        "/start — главное меню\n"
        "/help — список команд\n"
        "/new — создать карточку для повторений\n"
        "/id — найти карточку по id\n"
        "/rep — повторить истекшие карточки\n"
        "/stat — открыть статистику пользователя\n"
        "/clear — сбросить текущий диалог"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("cancel", "clear"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    REVIEW_SESSIONS.pop(message.from_user.id, None)   
    await message.answer("Диалог отменен.", reply_markup=get_main_menu_kb())

# --- БЫСТРОЕ СОЗДАНИЕ (/fast) ---
@dp.message(Command("fast"))
async def cmd_fast(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    subjects = await get_user_subjects(tg_id)

    if not subjects:
        await message.answer(
            "⚡ <b>Быстрый режим.</b>\n\n"
            "У тебя пока нет предметов. Введи название первого:",
            parse_mode="HTML",
        )
        await state.set_state(FastCreate.new_subject_input)
        return

    # Сохраняем список в FSM — в callback_data пойдут только индексы
    await state.update_data(fast_subjects_list=subjects)

    buttons = [
        [InlineKeyboardButton(text=s, callback_data=f"fastsubj_{i}")]
        for i, s in enumerate(subjects)
    ]
    buttons.append(
        [InlineKeyboardButton(text="➕ Добавить новый предмет", callback_data="fastsubj_new")]
    )
    buttons.append(
        [InlineKeyboardButton(text="❌ Отмена", callback_data="fast_cancel")]
        )

    await message.answer(
        "⚡ <b>Bliz-create!</b>\nВыбери предмет:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await state.set_state(FastCreate.subject_choice)


@dp.message(FastCreate.waiting_content, F.text & ~F.text.startswith("/"))
async def process_fast_content(message: Message, state: FSMContext):
    data = await state.get_data()
    subject = data.get("fast_subject", "Без предмета")
    tg_id = message.from_user.id

    raw = message.text.strip()

    # Сначала пробуем разделитель "***" (многострочные поля)
    parts = [p.strip() for p in re.split(r"\n\s*\*{3,}\s*\n", raw)]   # ← ИЗМЕНЕНО: --- на ***
    if len(parts) == 4:
        front_text, back_text, question, answer_part = parts
    else:
        # Fallback: простой формат — 4 строки (для коротких карточек)
        lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
        if len(lines) == 4:
            front_text, back_text, question, answer_part = lines
        else:
            await message.answer(
                "❌ Не смог разобрать карточку.\n\n"
                "Есть <b>два формата</b>:\n\n"
                "<b>1) Простой</b> — ровно 4 строки (если поля короткие):\n"
                "<code>лицевая\nобратная\nвопрос\nответ</code>\n\n"
                "<b>2) С разделителем</b> — если поля многострочные, "
                "разделяй их строкой <code>***</code>:\n"                          # ← ИЗМЕНЕНО: --- на ***
                "<code>лицевая\n"
                "(может быть\nмногострочной)\n"
                "***\n"                                                           # ← ИЗМЕНЕНО
                "обратная\n"
                "(тоже)\n"
                "***\n"                                                           # ← ИЗМЕНЕНО
                "вопрос\n"
                "***\n"                                                           # ← ИЗМЕНЕНО
                "вариант1; вариант2</code>\n\n"
                "Попробуй снова или /cancel.",
                parse_mode="HTML",
            )
            return

    # Определяем тип: есть ; → choice, иначе → typing
    if ";" in answer_part:
        opts = [p.strip() for p in answer_part.split(";") if p.strip()]
        if len(opts) < 2:
            await message.answer(
                "❌ Для «опроса» нужно хотя бы 2 варианта через <code>;</code>.\n"
                "Попробуй снова или /cancel.",
                parse_mode="HTML",
            )
            return
        check_type = "choice"
        correct_answer = opts[0]
        options_str = ";".join(opts)
    else:
        check_type = "typing"
        correct_answer = answer_part
        options_str = None

    card_id = await add_card(
        tg_id=tg_id,
        subject=subject,
        front_text=front_text,
        front_photo_id=None,
        back_text=back_text,
        question=question,
        check_type=check_type,
        correct_answer=correct_answer,
        options_str=options_str,
    )

    await state.clear()

    card = await get_card(card_id, tg_id)
    await message.answer(
        f"⚡ Карточка #{card_id} создана!\n\n{format_card_info(card)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"card_del_{card_id}"),
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"card_edit_{card_id}"),
            ]
        ]),
    )


@dp.callback_query(F.data == "fast_cancel")
async def callback_fast_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Быстрое создание отменено.")
    await callback.answer()

@dp.callback_query(FastCreate.subject_choice, F.data.startswith("fastsubj_"))
async def process_fast_subject_choice(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split("_", 1)[1]
    await callback.answer()

    if action == "new":
        await callback.message.edit_text(
            "⚡ Введи название нового предмета:",
            parse_mode="HTML",
        )
        await state.set_state(FastCreate.new_subject_input)
        return

    # Это индекс — достаём предмет из FSM-списка
    data = await state.get_data()
    subjects_list = data.get("fast_subjects_list", [])
    try:
        idx = int(action)
    except ValueError:
        idx = -1
    if idx < 0 or idx >= len(subjects_list):
        await callback.message.edit_text("⚠️ Список устарел. Начни заново — /fast")
        return

    subject = subjects_list[idx]
    await state.update_data(fast_subject=subject)
    await state.set_state(FastCreate.waiting_content)

    await callback.message.edit_text(
        fast_instruction_text(subject),
        parse_mode="HTML",
    )

@dp.message(FastCreate.new_subject_input, ~F.text.startswith("/"))
async def process_fast_new_subject(message: Message, state: FSMContext):
    subject = (message.text or "").strip()
    if not subject:
        await message.answer("Пустое название. Введи ещё раз:")
        return

    await state.update_data(fast_subject=subject)
    await state.set_state(FastCreate.waiting_content)

    await message.answer(
        fast_instruction_text(subject),
        parse_mode="HTML",
    )


# --- СОЗДАНИЕ КАРТОЧКИ (/new и меню) С ФОТО ВЫБОРА ПРЕДМЕТА ---
@dp.message(Command("new"))
@dp.callback_query(F.data == "menu_create")
async def start_card_creation(event: Message | CallbackQuery, state: FSMContext):
    user_id = event.from_user.id
    subjects = await get_user_subjects(user_id)
    
    msg_text = "🧬 Выберите предмет..." if subjects else "🧬 Введите название предмета/дисциплины."
    
    if subjects:
        keyboard_buttons = []
        for subj in subjects:
            keyboard_buttons.append([InlineKeyboardButton(text=subj, callback_data=f"createsubj_{subj}")])
        keyboard_buttons.append([InlineKeyboardButton(text="➕️ Добавить новый предмет", callback_data="createsubj_new")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        if isinstance(event, Message):
            sent = await event.answer_photo(photo=PHOTO_SUBJECT, caption=msg_text, reply_markup=keyboard)
        else:
            await event.message.delete()
            sent = await event.message.answer_photo(photo=PHOTO_SUBJECT, caption=msg_text, reply_markup=keyboard)
        await state.set_state(CardCreation.subject_choice)
    else:
        if isinstance(event, Message):
            sent = await event.answer_photo(photo=PHOTO_SUBJECT, caption=msg_text)
        else:
            await event.message.delete()
            sent = await event.message.answer_photo(photo=PHOTO_SUBJECT, caption=msg_text)
        await state.set_state(CardCreation.new_subject_input)
    
    await state.update_data(menu_msg_id=sent.message_id)

@dp.callback_query(CardCreation.subject_choice, F.data.startswith("createsubj_"))
async def process_subject_choice(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split("_", 1)[1]
    await callback.answer()
    
    if action == "new":
        try:
            await callback.message.edit_caption(caption="🧬 Введите название предмета/дисциплины.", reply_markup=None)
        except Exception:
            await callback.message.delete()
            sent = await callback.message.answer("🧬 Введите название предмета/дисциплины.")
            await state.update_data(menu_msg_id=sent.message_id)
        await state.set_state(CardCreation.new_subject_input)
    else:
        await state.update_data(subject=action)
        try:
            await callback.message.edit_caption(caption=f"✅ Предмет: {action}\n⬆️ Пришлите фото и/или текст лицевой стороны карточки...", reply_markup=None)
        except Exception:
            await callback.message.delete()
            sent = await callback.message.answer(f"✅ Предмет: {action}\n⬆️ Пришлите фото и/или текст лицевой стороны карточки...")
            await state.update_data(menu_msg_id=sent.message_id)
        await state.set_state(CardCreation.front_side)

@dp.message(CardCreation.new_subject_input, ~F.text.startswith("/"))
async def process_new_subject(message: Message, state: FSMContext):
    subject = message.text.strip()
    await state.update_data(subject=subject)
    try:
        await message.delete()
    except Exception:
        pass
    
    data = await state.get_data()
    if "menu_msg_id" in data:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=data["menu_msg_id"])
        except Exception:
            pass
            
    sent = await message.answer(f"✅ Предмет: {subject}\n⬆️ Пришлите фото и/или текст лицевой стороны карточки...")
    await state.update_data(menu_msg_id=sent.message_id)
    await state.set_state(CardCreation.front_side)

@dp.message(CardCreation.front_side, F.photo | (F.text & ~F.text.startswith("/")))
async def process_front_side(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id if message.photo else None
    front_text = message.caption if message.photo else message.text
    
    await state.update_data(front_photo_id=photo_id, front_text=front_text)
    try:
        await message.delete()
    except Exception:
        pass
    
    data = await state.get_data()
    if "menu_msg_id" in data:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=data["menu_msg_id"],
                text="❓️ Введите проверочный вопрос..."
            )
        except Exception:
            sent = await message.answer("❓️ Введите проверочный вопрос...")
            await state.update_data(menu_msg_id=sent.message_id)
    else:
        sent = await message.answer("❓️ Введите проверочный вопрос...")
        await state.update_data(menu_msg_id=sent.message_id)
        
    await state.set_state(CardCreation.question)

@dp.message(CardCreation.question, ~F.text.startswith("/"))
async def process_question_input(message: Message, state: FSMContext):
    question = message.text.strip()
    await state.update_data(question=question)
    try:
        await message.delete()
    except Exception:
        pass
    
    data = await state.get_data()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⌨️ Клавиатура", callback_data="checktype_typing"),
            InlineKeyboardButton(text="🗳️ Опрос-викторина", callback_data="checktype_choice")
        ]
    ])
    try:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=data["menu_msg_id"],
            text="Выберите формат вопроса...",
            reply_markup=keyboard
        )
    except Exception:
        sent = await message.answer("Выберите формат вопроса...", reply_markup=keyboard)
        await state.update_data(menu_msg_id=sent.message_id)
        
    await state.set_state(CardCreation.check_type)

@dp.callback_query(CardCreation.check_type, F.data.startswith("checktype_"))
async def process_check_type_choice(callback: CallbackQuery, state: FSMContext):
    check_type = callback.data.split("_")[1]
    await state.update_data(check_type=check_type)
    await callback.answer()
    
    if check_type == "typing":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Вернуться назад", callback_data="back_to_question")]
        ])
        await callback.message.edit_text("Введите правильный ответ", reply_markup=keyboard)
        await state.set_state(CardCreation.correct_answer)
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Вернуться назад", callback_data="back_to_question")]
        ])
        await callback.message.edit_text(
            "Напишите варианты ответа через \";\". Правильный ответ должен быть первым в списке.",
            reply_markup=keyboard
        )
        await state.set_state(CardCreation.options)

@dp.callback_query(F.data == "back_to_question")
async def back_to_question_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("❓️ Введите проверочный вопрос...")
    await state.set_state(CardCreation.question)
    await state.update_data(menu_msg_id=callback.message.message_id)

@dp.message(CardCreation.correct_answer, ~F.text.startswith("/"))
async def process_typing_answer(message: Message, state: FSMContext):
    ans = message.text.strip()
    await state.update_data(correct_answer=ans, options_str=None)
    try:
        await message.delete()
    except Exception:
        pass
    
    data = await state.get_data()
    try:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=data["menu_msg_id"],
            text="↩️ Введите текст обратной стороны карточки:"
        )
    except Exception:
        sent = await message.answer("↩️ Введите текст обратной стороны карточки:")
        await state.update_data(menu_msg_id=sent.message_id)
        
    await state.set_state(CardCreation.back_side)

@dp.message(CardCreation.options, ~F.text.startswith("/"))
async def process_options_answer(message: Message, state: FSMContext):
    raw_text = message.text.strip()
    parts = [p.strip() for p in raw_text.split(";") if p.strip()]
    if not parts:
        await message.answer("❌️ Ошибка! Укажите хотя бы один вариант ответа через ';'.")
        return
    
    correct_ans = parts[0]
    options_str = ";".join(parts)
    await state.update_data(correct_answer=correct_ans, options_str=options_str)
    try:
        await message.delete()
    except Exception:
        pass
    
    data = await state.get_data()
    try:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=data["menu_msg_id"],
            text="↩️ Введите текст обратной стороны карточки:"
        )
    except Exception:
        sent = await message.answer("↩️ Введите текст обратной стороны карточки:")
        await state.update_data(menu_msg_id=sent.message_id)
        
    await state.set_state(CardCreation.back_side)

@dp.message(CardCreation.back_side, ~F.text.startswith("/"))
async def process_back_side_input(message: Message, state: FSMContext):
    back_text = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    
    data = await state.get_data()
    tg_id = message.from_user.id
    
    card_id = await add_card(
        tg_id=tg_id,
        subject=data["subject"],
        front_text=data.get("front_text"),
        front_photo_id=data.get("front_photo_id"),
        back_text=back_text,
        question=data.get("question"),
        check_type=data["check_type"],
        correct_answer=data["correct_answer"],
        options_str=data.get("options_str")
    )
    
    card = await get_card(card_id, tg_id)
    await state.clear()
    
    text = format_card_info(card)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"card_del_{card_id}"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data=f"card_edit_{card_id}")
        ]
    ])
    
    menu_msg_id = data.get("menu_msg_id")
    if menu_msg_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=menu_msg_id)
        except Exception:
            pass
            
    if card['front_photo_id']:
        await bot.send_photo(
            chat_id=message.chat.id,
            photo=card['front_photo_id'],
            caption=f"🎉 Карточка создана!\n\n{text}",
            reply_markup=keyboard
        )
    else:
        await message.answer(f"🎉 Карточка создана!\n\n{text}", reply_markup=keyboard)


def fast_instruction_text(subject: str) -> str:
    return (
        f"⚡ <b>Bliz-create!</b>\n"
        f"Предмет: <b>{subject}</b>\n\n"
        f"Пришли карточку <b>ОДНИМ</b> сообщением.\n\n"
        f"<b>Простой формат</b> (4 строки, если поля короткие):\n"
        f"<code>лицевая сторона\n"
        f"обратная сторона\n"
        f"вопрос\n"
        f"24; 72; 168; 336</code>\n"
        f"(первый вариант — правильный; без <code>;</code> — ввод с клавиатуры)\n\n"
        f"<b>С разделителем</b> (если поля многострочные — "
        f"разделяй поля строкой <code>***</code>):\n"                              # ← ИЗМЕНЕНО: --- на ***
        f"<code>Сколько часов в 7 днях?\n"
        f"(пояснение)\n"
        f"***\n"                                                                    # ← ИЗМЕНЕНО
        f"7 × 24 = 168\n"
        f"***\n"                                                                    # ← ИЗМЕНЕНО
        f"Сколько часов в неделе?\n"
        f"***\n"                                                                    # ← ИЗМЕНЕНО
        f"24; 72; 168; 336</code>\n\n"
        f"⚠️ Фото в этом режиме недоступно\n\n"
        f"Отмена — /cancel"
    )

def format_review_time(utc_str: str | None) -> str:
    """Преобразует строку UTC из БД в МСК для отображения пользователю."""
    if not utc_str:
        return "—"
    try:
        dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S МСК")
    except Exception:
        return utc_str
    
# --- ФОРМАТИРОВАНИЕ ИНФОРМАЦИИ КАРТОЧКИ ---
def format_card_info(card: dict) -> str:
    options_display = "Нет"
    if card['check_type'] == 'choice' and card['options_str']:
        opts = card['options_str'].split(';')
        options_display = ", ".join(opts)
        
    return (
        f"id: {card['id']}\n"
        f"🧬 Предмет: {card['subject']}\n"
        f"⏳️ Следующее время повторения: {format_review_time(card['next_review_at'])}\n"
        f"⬆️ Лицевая сторона: {card['front_text'] or 'Нет'}\n"
        f"❓️ Вопрос: {card['question'] or 'Нет'}\n"
        f"🗳️ Варианты ответов: {options_display}\n"
        f"✔ Верный ответ: {card['correct_answer']}\n"
        f"↩️ Обратная сторона: {card['back_text'] or 'Нет'}"
    )


# --- УДАЛЕНИЕ И ИЗМЕНЕНИЕ КАРТОЧЕК ---
@dp.callback_query(F.data.startswith("card_del_yes_"))
async def callback_delete_yes(callback: CallbackQuery):
    card_id = int(callback.data.split("_")[3])
    deleted = await delete_card(card_id, callback.from_user.id)
    if deleted is None:
        await callback.answer("Карточка не найдена или не ваша", show_alert=True)
        return
    await callback.message.delete()
    await callback.message.answer(f"Карточка (id:{card_id}) удалена.")
    await callback.answer()

@dp.callback_query(F.data.startswith("card_del_no_"))
async def callback_delete_no(callback: CallbackQuery):
    card_id = int(callback.data.split("_")[3])
    card = await get_card(card_id, callback.from_user.id)
    if card:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"card_del_{card_id}"),
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"card_edit_{card_id}")
            ]
        ])
        if card['front_photo_id']:
            await callback.message.edit_caption(caption=format_card_info(card), reply_markup=keyboard)
        else:
            await callback.message.edit_text(format_card_info(card), reply_markup=keyboard)
    else:
        await callback.message.delete()

@dp.callback_query(F.data.startswith("card_del_"))
async def callback_delete_card(callback: CallbackQuery):
    card_id = int(callback.data.split("_")[2])
    card = await get_card(card_id, callback.from_user.id)
    if not card:
        await callback.answer("Карточка не найдена или не ваша", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Да", callback_data=f"card_del_yes_{card_id}"),
            InlineKeyboardButton(text="Нет", callback_data=f"card_del_no_{card_id}")
        ]
    ])
    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer("Вы уверены?")

@dp.callback_query(F.data.startswith("card_edit_"))
async def callback_edit_card(callback: CallbackQuery, state: FSMContext):
    card_id = int(callback.data.split("_")[2])
    card = await get_card(card_id, callback.from_user.id)
    if not card:
        await callback.answer("Карточка не найдена или не ваша", show_alert=True)
        return
    await state.update_data(edit_card_id=card_id)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Фото", callback_data="edfield_front_photo_id"),
         InlineKeyboardButton(text="Лицевая сторона", callback_data="edfield_front_text")],
        [InlineKeyboardButton(text="Вопрос", callback_data="edfield_question"),
         InlineKeyboardButton(text="Варианты ответов", callback_data="edfield_options_str")],
        [InlineKeyboardButton(text="Верный ответ", callback_data="edfield_correct_answer"),
         InlineKeyboardButton(text="Текст обратной стороны", callback_data="edfield_back_text")]
    ])
    
    try:
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except Exception:
        pass
    await callback.answer()
    await state.set_state(CardEdit.field_choice)

@dp.callback_query(CardEdit.field_choice, F.data.startswith("edfield_"))
async def callback_edit_field_choice(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split("_", 1)[1]
    await state.update_data(edit_field=field)
    await callback.answer()
    
    try:
        await callback.message.delete()
    except Exception:
        pass
    
    if field == "front_photo_id":
        prompt_text = "Пришлите новое фото"
    else:
        field_names = {
            "front_text": "лицевую сторону",
            "question": "проверочный вопрос",
            "options_str": "варианты ответа через ';'",
            "correct_answer": "верный ответ",
            "back_text": "текст обратной стороны"
        }
        fname = field_names.get(field, field)
        prompt_text = f"Введите новое значение для поля: {fname}"
        
    await callback.message.answer(prompt_text)
    await state.set_state(CardEdit.new_value)

@dp.message(CardEdit.new_value, F.photo)
async def process_edit_new_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    card_id = data["edit_card_id"]
    field = data["edit_field"]
    
    if field != "front_photo_id":
        await message.answer("Ожидался текстовый ввод, а не фото. Попробуйте еще раз.")
        return
        
    photo_id = message.photo[-1].file_id
    ok = await update_card(card_id, message.from_user.id, {field: photo_id})
    if not ok:
        await message.answer("Карточка не найдена или не ваша")
        return
    
    try:
        await message.delete()
    except Exception:
        pass
        
    await send_updated_card(message.chat.id, card_id, message.from_user.id, state)

@dp.message(CardEdit.new_value, F.text & ~F.text.startswith("/"))
async def process_edit_new_text(message: Message, state: FSMContext):
    data = await state.get_data()
    card_id = data["edit_card_id"]
    field = data["edit_field"]
    
    if field == "front_photo_id":
        await message.answer("Ожидалось фото, а не текст. Пришлите фото.")
        return
        
    val = message.text.strip()
    ok = await update_card(card_id, message.from_user.id, {field: val})
    if not ok:
        await message.answer("Карточка не найдена или не ваша")
        return
    
    try:
        await message.delete()
    except Exception:
        pass
        
    await send_updated_card(message.chat.id, card_id, message.from_user.id, state)

async def send_updated_card(chat_id: int, card_id: int, tg_id: int, state: FSMContext):
    await state.clear()
    card = await get_card(card_id, tg_id)
    if not card:
        return
        
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"card_del_{card_id}"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data=f"card_edit_{card_id}")
        ]
    ])
    
    text = f"🔧 Карточка обновлена!\n\n{format_card_info(card)}"
    if card['front_photo_id']:
        await bot.send_photo(chat_id=chat_id, photo=card['front_photo_id'], caption=text, reply_markup=keyboard)
    else:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)


# --- ПОИСК ПО ID (/id) С ФОТО-ПРОМПТОМ ---
@dp.message(Command("id"))
async def cmd_id_search(message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer_photo(photo=PHOTO_ID_PROMPT, caption="Введите id карточки, например: /id 5")
        return
    
    card_id = int(parts[1])
    card = await get_card(card_id, message.from_user.id)
    if not card:
        await message.answer("❌️ Карточка с таким ID не найдена.")
        return
    
    text = format_card_info(card)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"card_del_{card_id}"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data=f"card_edit_{card_id}")
        ]
    ])
    
    if card['front_photo_id']:
        await message.answer_photo(photo=card['front_photo_id'], caption=text, reply_markup=keyboard)
    else:
        await message.answer(text, reply_markup=keyboard)


# --- ПОВТОРЕНИЕ ИСТЕКШИХ (/rep) С ФОТО ИЗ ТЗ ---
REVIEW_SESSIONS = {}

@dp.message(Command("rep"))
@dp.callback_query(F.data == "menu_rep")
async def cmd_rep(event: Message | CallbackQuery):
    tg_id = event.from_user.id
    await record_rep_command(tg_id)
    
    due = await get_due_cards(tg_id)
    if not due:
        msg = "But nobody came..."
        if isinstance(event, Message):
            await event.answer_photo(photo=PHOTO_REP_EMPTY, caption=msg)
        else:
            await event.message.answer_photo(photo=PHOTO_REP_EMPTY, caption=msg)
        return
        
    REVIEW_SESSIONS[tg_id] = {
        "due_cards": due,
        "index": 0,
        "total_count": len(due)
    }
    
    info_text = f"📋️ Поступило {len(due)} карт(ы) на повторение..."
    if isinstance(event, Message):
        await event.answer_photo(photo=PHOTO_REP_LOADED, caption=info_text)
    else:
        await event.message.answer_photo(photo=PHOTO_REP_LOADED, caption=info_text)
        
    await send_next_review_card(tg_id, event.message if isinstance(event, CallbackQuery) else event)

async def send_next_review_card(tg_id: int, message_obj: Message):
    session = REVIEW_SESSIONS.get(tg_id)
    if not session or session["index"] >= len(session["due_cards"]):
        total_rev = (session or {}).get("total_count", 0)
        await message_obj.answer(f"✅️ Завершено! Прогресс повторений: {total_rev}")
        REVIEW_SESSIONS.pop(tg_id, None)
        return
        
    idx = session["index"]
    card = session["due_cards"][idx]
    total = session["total_count"]
    
    card_num = idx + 1
    front_text = card['front_text'] or ""
    q_text = card['question'] or ""
    
    caption = (
        f"{card_num}/{total}\n\n"
        f"\"{front_text}\"\n\n"
        f"Вопрос: {q_text}"
    )
    
    if card['check_type'] == 'choice' and card['options_str']:
        opts = card['options_str'].split(';')
        random.shuffle(opts)
        buttons = []
        for opt in opts:
            buttons.append([InlineKeyboardButton(text=opt, callback_data=f"revans_{card['id']}_{opt}")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    else:
        keyboard = None
        
    if card['front_photo_id']:
        sent = await bot.send_photo(chat_id=tg_id, photo=card['front_photo_id'], caption=caption, reply_markup=keyboard)
    else:
        sent = await bot.send_message(chat_id=tg_id, text=caption, reply_markup=keyboard)
        
    session["current_msg_id"] = sent.message_id
    session["current_card"] = card

@dp.callback_query(F.data.startswith("revans_"))
async def process_callback_review_answer(callback: CallbackQuery):
    tg_id = callback.from_user.id
    parts = callback.data.split("_", 2)
    card_id = int(parts[1])
    chosen_ans = parts[2]
    
    session = REVIEW_SESSIONS.get(tg_id)
    if not session:
        await callback.answer("Сессия повторения устарела.")
        return
        
    card = session["current_card"]
    is_correct = (chosen_ans == card['correct_answer'])
    
    await handle_review_answer_logic(tg_id, card, is_correct, callback.message)
    await callback.answer()

@dp.message(F.text & ~F.text.startswith("/"), StateFilter(None))
async def process_text_review_answer(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return
        
    tg_id = message.from_user.id
    session = REVIEW_SESSIONS.get(tg_id)
    if not session:
        return
        
    card = session["current_card"]
    if card['check_type'] == 'typing':
        user_ans = message.text.strip()
        is_correct = (user_ans.lower() == card['correct_answer'].strip().lower())
        
        try:
            await message.delete()
        except Exception:
            pass
            
        await handle_review_answer_logic(tg_id, card, is_correct, message)

async def handle_review_answer_logic(tg_id: int, card: dict, is_correct: bool, message_obj: Message):
    session = REVIEW_SESSIONS.get(tg_id)
    if session and "current_msg_id" in session:
        try:
            await bot.delete_message(chat_id=tg_id, message_id=session["current_msg_id"])
        except Exception:
            pass
            
    updated_card, new_stage = await process_review_result(card['id'], is_correct, tg_id)
    
    if is_correct:
        if card['stage'] >= 4:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="Да", callback_data=f"maxstage_del_{card['id']}"),
                    InlineKeyboardButton(text="Нет", callback_data=f"maxstage_keep_{card['id']}")
                ]
            ])
            await bot.send_message(
                chat_id=tg_id,
                text="♟️ Информация успешно усвоена. Хотите удалить карточку?",
                reply_markup=keyboard
            )
        else:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭️ Перейти к следующей", callback_data="next_card")]
            ])
            await bot.send_message(
                chat_id=tg_id,
                text=f"Верный ответ!\n\n↩️ Обратная сторона карточки: {card['back_text'] or 'Нет'}",
                reply_markup=keyboard
            )
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⏭️ Перейти к следующей", callback_data="next_card"),
                InlineKeyboardButton(text="⚖ Подать апелляцию", callback_data=f"appeal_{card['id']}")
            ]
        ])
        await bot.send_message(
            chat_id=tg_id,
            text=f"❌️ Кажется вы ошиблись. Верный ответ: {card['correct_answer']}, я полагаю.\n\n"
                 f"↩️ Обратная сторона карточки: {card['back_text'] or 'Нет'}\n\n"
                 f"⏳️ Срок флеш-карты (id: {card['id']}) вернется к 24 часам.",
            reply_markup=keyboard
        )

@dp.callback_query(F.data == "next_card")
async def callback_next_card(callback: CallbackQuery):
    tg_id = callback.from_user.id
    await callback.message.delete()
    
    session = REVIEW_SESSIONS.get(tg_id)
    if session:
        session["index"] += 1
        await send_next_review_card(tg_id, callback.message)
    await callback.answer()

@dp.callback_query(F.data.startswith("appeal_"))
async def callback_appeal(callback: CallbackQuery):
    card_id = int(callback.data.split("_")[1])
    tg_id = callback.from_user.id
    
    await process_review_result(card_id, is_correct=True, tg_id=callback.from_user.id)
    
    await callback.message.delete()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭️ Перейти к следующей", callback_data="next_card")]
    ])
    await callback.message.answer(
        "✏️ Приятно. Отмечу ответ верным.",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("maxstage_del_"))
async def callback_maxstage_del(callback: CallbackQuery):
    card_id = int(callback.data.split("_")[2])
    tg_id = callback.from_user.id
    deleted = await delete_card(card_id, tg_id)
    if deleted is None:
        await callback.answer("Карточка не найдена или не ваша", show_alert=True)
        return
    await callback.message.delete()
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭️ Перейти к следующей", callback_data="next_card")]
    ])
    await callback.message.answer("Карточка удалена.", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("maxstage_keep_"))
async def callback_maxstage_keep(callback: CallbackQuery):
    tg_id = callback.from_user.id
    await callback.message.delete()
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭️ Перейти к следующей", callback_data="next_card")]
    ])
    await callback.message.answer("Флеш-карта вновь переходит на этап 14 дней.", reply_markup=keyboard)
    await callback.answer()


# --- СТАТИСТИКА (/stat и кнопка меню) С ФОТО ---
@dp.message(Command("stat"))
@dp.callback_query(F.data == "menu_stat")
async def cmd_stat(event: Message | CallbackQuery):
    tg_id = event.from_user.id
    user_name = event.from_user.full_name
    
    stats = await get_user_stats_data(tg_id)
    subjects = await get_user_subjects(tg_id)
    cards_count = await get_user_cards_count(tg_id)
    subjects_str = ", ".join(subjects) if subjects else "Нет предметов"
    
    text = (
        f"📈 Прогресс пользователя {user_name}:\n\n"
        f"🧬 Дисциплины: {subjects_str}\n"
        f"📚 Всего карточек: {cards_count}\n"
        f"⏳️ Пройдено за все время: {stats['total_reviewed']}\n"
        f"📆 Пройдено за день: {stats['today_reviewed']}\n"
        f"🗑️ Удалено карт: {stats['deleted_cards']}"
    )
    try:
        if isinstance(event, Message):
            await event.answer_photo(photo=PHOTO_STAT, caption=text)
        else:
            await event.message.answer_photo(photo=PHOTO_STAT, caption=text)
            await event.answer()
    except Exception as e:
        print(f"/stat photo error: {e}")
        # fallback: текстом, если фото протухло
        if isinstance(event, Message):
            await event.answer(text)
        else:
            await event.message.answer(text)
            await event.answer()


# --- ФОНОВЫЙ ПЛАНИРОВЩИК НАПОМИНАНИЙ (ДУОЛИНГО СТИЛЬ) ---
async def reminder_worker():
    while True:
        await asyncio.sleep(21600)  # проверка каждые 6 часов
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT * FROM user_stats")
                users = await cur.fetchall()

                for u in users:
                    try:
                        tg_id = u['tg_id']

                        due_cards = await get_due_cards(tg_id)
                        if not due_cards:
                            continue

                        count = len(due_cards)
                        last_rep = u['last_rep_at']

                        # Если /rep никогда не делал — считаем «свежим»,
                        # напоминания шлём (иначе новые юзеры их не получат никогда)
                        hours_since_rep = 0
                        if last_rep:
                            dt_rep = datetime.strptime(last_rep, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                            hours_since_rep = (datetime.now(timezone.utc) - dt_rep).total_seconds() / 3600
                            if hours_since_rep >= 240:
                                continue  # 10 дней без /rep — не напоминаем

                        required_interval_hours = 6
                        if hours_since_rep >= 120:
                            required_interval_hours = 24
                        elif hours_since_rep >= 48:
                            required_interval_hours = 12

                        last_sent = u['last_reminder_sent_at']
                        if last_sent:
                            dt_sent = datetime.strptime(last_sent, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                            hours_since_sent = (datetime.now(timezone.utc) - dt_sent).total_seconds() / 3600
                            if hours_since_sent < required_interval_hours:
                                continue

                        rem_count = u['reminder_count'] + 1

                        if rem_count <= 4:
                            msg_text = f"🔔 Может, пора уже повторить? (/rep)\nКарточек в очереди: {count}"
                        elif rem_count <= 8:
                            msg_text = f"🔔 Повторите карточки. В очереди: {count}"
                        elif rem_count <= 12:
                            msg_text = f"🔔 Вам следует немедленно повторить карточки. В очереди: {count}"
                        elif rem_count <= 20:
                            msg_text = f"{count} ожидают."
                        elif rem_count <= 40:
                            msg_text = f"Я здесь. В очереди: {count}"
                        elif rem_count <= 60:
                            msg_text = f"Я друг. Я тебя не обижу. Мертвых карточек: {count}"
                        else:
                            msg_text = f"{count} {count} {count} {count} {count} Я знаю где вы живете."

                        await bot.send_message(chat_id=tg_id, text=msg_text)

                        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                        await db.execute(
                            """
                            UPDATE user_stats
                            SET reminder_count = ?, last_reminder_sent_at = ?
                            WHERE tg_id = ?
                            """,
                            (rem_count, now_str, tg_id),
                        )
                        await db.commit()

                    except Exception as e:
                        # Ошибка у ОДНОГО пользователя — не ломаем цикл для остальных
                        print(f"Reminder error for {u['tg_id']}: {e}")
                        continue

        except Exception as e:
            # Глобальная ошибка (соединение с БД, сам запрос SELECT * FROM user_stats)
            print(f"Reminder worker global error: {e}")


# --- ЗАПУСК БОТА ---
async def main():
    await init_db()
    asyncio.create_task(reminder_worker())
    print("Bot started...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())