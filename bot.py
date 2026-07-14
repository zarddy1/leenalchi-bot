"""
Leenalchi Cafe — Telegram-бот системи лояльності (бабл-ті + бали)

Клієнт: /start -> ділиться номером телефону (кнопка, підтверджує сам Telegram,
        SMS не потрібні) -> бачить баланс і історію нарахувань.
Адмін:  окремі команди/кнопки, доступні тільки user_id з списку ADMIN_IDS ->
        пошук клієнта за номером (повним або останніми 4 цифрами),
        нарахування/списання балів.

Запуск:
    pip install -r requirements.txt
    export BOT_TOKEN="твій_токен_від_BotFather"
    export ADMIN_IDS="123456789,987654321"
    python bot.py
"""

import asyncio
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    Contact,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("leenalchi_bot")

# ---------------------------------------------------------------------------
# Конфігурація
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}
DB_PATH = os.environ.get("DB_PATH", "leenalchi.db")
MENU_URL = "https://leenalchi.choiceqr.com/menu"

# Приклад — потім заміниш на реальні назви й вартість напоїв
REWARDS = [
    {"name": "Маленький бабл-ті", "cost": 80},
    {"name": "Великий бабл-ті", "cost": 150},
]

# ---------------------------------------------------------------------------
# База даних (SQLite, простий і надійний варіант для старту)
# ---------------------------------------------------------------------------

def db_init() -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                phone       TEXT UNIQUE,
                name        TEXT,
                balance     INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                amount      INTEGER,
                note        TEXT,
                by_admin    INTEGER,
                created_at  TEXT
            )
            """
        )
        con.commit()


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits and not digits.startswith("+"):
        digits = "+" + digits
    return digits


def get_user_by_phone(phone: str):
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
        return dict(row) if row else None


def get_users_by_suffix(suffix: str, limit: int = 10):
    """Пошук клієнтів за останніми цифрами номера (напр. останні 4)."""
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM users WHERE phone LIKE ? ORDER BY created_at DESC LIMIT ?",
            (f"%{suffix}", limit),
        ).fetchall()
        return [dict(r) for r in rows]


def search_clients(query: str):
    """Універсальний пошук: повний номер -> точний збіг,
    короткий фрагмент (напр. останні 4 цифри) -> пошук за закінченням номера."""
    digits = re.sub(r"\D", "", query or "")
    if len(digits) >= 9:
        phone = normalize_phone(query)
        user = get_user_by_phone(phone)
        return [user] if user else []
    if len(digits) >= 3:
        return get_users_by_suffix(digits)
    return []


def get_user_by_id(telegram_id: int):
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return dict(row) if row else None


def upsert_user(telegram_id: int, phone: str, name: str):
    """Створює клієнта або прив'язує справжній telegram_id до запису,
    який адмін міг раніше створити вручну через /register (з негативним fake_id)."""
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        existing_by_phone = con.execute(
            "SELECT * FROM users WHERE phone = ?", (phone,)
        ).fetchone()
        if existing_by_phone and existing_by_phone["telegram_id"] != telegram_id:
            old_id = existing_by_phone["telegram_id"]
            con.execute(
                "UPDATE users SET telegram_id = ?, name = ? WHERE telegram_id = ?",
                (telegram_id, name, old_id),
            )
            con.execute(
                "UPDATE transactions SET telegram_id = ? WHERE telegram_id = ?",
                (telegram_id, old_id),
            )
        else:
            con.execute(
                """
                INSERT INTO users (telegram_id, phone, name, balance, created_at)
                VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET phone=excluded.phone, name=excluded.name
                """,
                (telegram_id, phone, name, datetime.utcnow().isoformat()),
            )
        con.commit()


def apply_points(phone: str, amount: int, note: str, by_admin: bool) -> dict | None:
    user = get_user_by_phone(phone)
    if not user:
        return None
    new_balance = max(0, user["balance"] + amount)
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            "UPDATE users SET balance = ? WHERE telegram_id = ?",
            (new_balance, user["telegram_id"]),
        )
        con.execute(
            """
            INSERT INTO transactions (telegram_id, amount, note, by_admin, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user["telegram_id"], amount, note, int(by_admin), datetime.utcnow().isoformat()),
        )
        con.commit()
    user["balance"] = new_balance
    return user


def get_all_users(limit: int = 50):
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_history(telegram_id: int, limit: int = 10):
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM transactions WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
            (telegram_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Допоміжне
# ---------------------------------------------------------------------------

def rewards_text(balance: int) -> str:
    lines = []
    for r in REWARDS:
        if balance >= r["cost"]:
            lines.append(f"✅ {r['name']} ({r['cost']} балів) — вже можна забрати!")
        else:
            left = r["cost"] - balance
            lines.append(f"🧋 {r['name']} ({r['cost']} балів) — не вистачає {left}")
    return "\n".join(lines)


def client_card(u: dict) -> str:
    return f"👤 {u['name']}\n📱 {u['phone']}\n💰 Баланс: {u['balance']} балів"


def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


CLIENT_COMMANDS = [
    BotCommand(command="start", description="Почати / мій кабінет"),
    BotCommand(command="menu", description="Меню кафе"),
    BotCommand(command="balance", description="Мій баланс балів"),
    BotCommand(command="history", description="Історія нарахувань"),
]

ADMIN_COMMANDS = CLIENT_COMMANDS + [
    BotCommand(command="admin", description="Панель персоналу"),
    BotCommand(command="find", description="Знайти клієнта (номер або останні 4 цифри)"),
    BotCommand(command="list", description="Список усіх клієнтів"),
    BotCommand(command="broadcast", description="Розіслати новину всім клієнтам"),
    BotCommand(command="add", description="Нарахувати бали"),
    BotCommand(command="sub", description="Списати бали"),
    BotCommand(command="register", description="Зареєструвати клієнта вручну"),
]


async def setup_commands(bot: Bot) -> None:
    await bot.set_my_commands(CLIENT_COMMANDS, scope=BotCommandScopeDefault())
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception as e:
            log.warning("Не вдалось задати меню команд для адміна %s: %s", admin_id, e)


# ---------------------------------------------------------------------------
# Клавіатури
# ---------------------------------------------------------------------------

CONTACT_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📱 Поділитися номером", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

CLIENT_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="🧋 Меню")],
        [KeyboardButton(text="📜 Історія")],
    ],
    resize_keyboard=True,
)

ADMIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔍 Пошук клієнта"), KeyboardButton(text="📋 Всі клієнти")],
        [KeyboardButton(text="📢 Розсилка"), KeyboardButton(text="❓ Довідка")],
    ],
    resize_keyboard=True,
)


# ---------------------------------------------------------------------------
# Роутери
# ---------------------------------------------------------------------------

client_router = Router()
admin_router = Router()


class AdminStates(StatesGroup):
    waiting_search = State()
    waiting_broadcast = State()


@client_router.message(Command("start"))
async def cmd_start(message: Message):
    user = get_user_by_id(message.from_user.id)
    if user:
        await message.answer(
            f"З поверненням, {user['name']}! 🦜\n\n"
            f"Твій баланс: <b>{user['balance']}</b> балів\n\n{rewards_text(user['balance'])}",
            reply_markup=CLIENT_KB,
        )
        return
    await message.answer(
        "Привіт! Я бот <b>Leenalchi Cafe</b> 🦜🧋\n"
        "Твій трохи дивний, але завжди приємний друг.\n\n"
        "Поділись номером телефону, щоб я міг створити твій бонусний рахунок "
        "і бариста міг нараховувати тобі бали за замовлення.",
        reply_markup=CONTACT_KB,
    )


@client_router.message(F.contact)
async def on_contact(message: Message):
    contact: Contact = message.contact
    if contact.user_id != message.from_user.id:
        await message.answer("Будь ласка, поділись саме своїм номером телефону 🙂")
        return
    phone = normalize_phone(contact.phone_number)
    name = message.from_user.first_name or "Друже"
    upsert_user(message.from_user.id, phone, name)
    await message.answer(
        f"Готово, {name}! Акаунт створено 🎉\n"
        f"Твій номер {phone} прив'язаний до бонусного рахунку.\n\n"
        "Обери дію на клавіатурі нижче 👇",
        reply_markup=CLIENT_KB,
    )


@client_router.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer(f"Наше меню 🧋\n{MENU_URL}")


@client_router.message(Command("balance"))
async def cmd_balance(message: Message):
    user = get_user_by_id(message.from_user.id)
    if not user:
        await message.answer("Спершу поділись номером телефону: /start")
        return
    await message.answer(
        f"Баланс: <b>{user['balance']}</b> балів\n\n{rewards_text(user['balance'])}"
    )


@client_router.message(Command("history"))
async def cmd_history(message: Message):
    user = get_user_by_id(message.from_user.id)
    if not user:
        await message.answer("Спершу поділись номером телефону: /start")
        return
    history = get_history(user["telegram_id"])
    if not history:
        await message.answer("Поки що порожньо. Замов щось смачне 🧋")
        return
    lines = []
    for h in history:
        sign = "+" if h["amount"] >= 0 else ""
        date = h["created_at"][:16].replace("T", " ")
        note = f" — {h['note']}" if h["note"] else ""
        lines.append(f"{sign}{h['amount']} балів{note}  ({date})")
    await message.answer("Останні операції:\n\n" + "\n".join(lines))


@client_router.message(F.text == "💰 Баланс")
async def btn_balance(message: Message):
    await cmd_balance(message)


@client_router.message(F.text == "🧋 Меню")
async def btn_menu(message: Message):
    await cmd_menu(message)


@client_router.message(F.text == "📜 Історія")
async def btn_history(message: Message):
    await cmd_history(message)


# ---------------------------------------------------------------------------
# Адмінська частина
# ---------------------------------------------------------------------------

@admin_router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "Панель персоналу 🦜\n\n"
        "/find +380XXXXXXXXX або останні 4 цифри — знайти клієнта\n"
        "/list — список усіх зареєстрованих клієнтів\n"
        "/add +380XXXXXXXXX 50 [примітка] — нарахувати бали\n"
        "/sub +380XXXXXXXXX 50 [примітка] — списати бали\n"
        "/register +380XXXXXXXXX Ім'я — зареєструвати клієнта вручну\n\n"
        "Або користуйся кнопками нижче 👇",
        reply_markup=ADMIN_KB,
    )


@admin_router.message(Command("list"))
async def cmd_list(message: Message):
    if not is_admin(message.from_user.id):
        return
    users = get_all_users()
    if not users:
        await message.answer("Клієнтів ще немає.")
        return
    lines = [f"{u['phone']} — {u['name']} ({u['balance']} балів)" for u in users]
    await message.answer("Зареєстровані клієнти:\n\n" + "\n".join(lines))


@admin_router.message(Command("broadcast"))
async def cmd_broadcast_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_broadcast)
    await message.answer("Надішли фото з підписом (текст новини) — розішлю всім клієнтам. /cancel — скасувати.")


@admin_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("Скасовано.", reply_markup=ADMIN_KB)


@admin_router.message(AdminStates.waiting_broadcast, F.photo)
async def cmd_broadcast_send(message: Message, state: FSMContext):
    await state.clear()
    photo_id = message.photo[-1].file_id
    caption = message.caption or ""
    users = get_all_users(limit=100000)
    sent, failed = 0, 0
    for u in users:
        if u["telegram_id"] <= 0:
            continue
        try:
            await message.bot.send_photo(u["telegram_id"], photo_id, caption=caption)
            sent += 1
        except Exception:
            failed += 1
    await message.answer(f"Розіслано: {sent}, не доставлено: {failed}", reply_markup=ADMIN_KB)


@admin_router.message(AdminStates.waiting_broadcast)
async def cmd_broadcast_wrong(message: Message):
    await message.answer("Потрібне фото з підписом. Спробуй ще раз або /cancel.")


@admin_router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    if not command.args:
        await message.answer("Формат: /find +380XXXXXXXXX або останні 4 цифри, напр. /find 1782")
        return
    await run_search(message, command.args.strip())


async def run_search(message: Message, query: str):
    results = search_clients(query)
    if not results:
        digits = re.sub(r"\D", "", query)
        phone = normalize_phone(query) if len(digits) >= 9 else None
        extra = f"\nЗареєструвати: /register {phone} Ім'я" if phone else ""
        await message.answer(f"Клієнта за запитом «{query}» не знайдено.{extra}")
        return
    if len(results) == 1:
        await message.answer(client_card(results[0]))
        return
    lines = [f"{u['phone']} — {u['name']} ({u['balance']} балів)" for u in results]
    await message.answer(
        f"Знайдено {len(results)} клієнтів за «{query}»:\n\n" + "\n".join(lines) +
        "\n\nВведи повний номер у /find для точного результату."
    )


@admin_router.message(Command("register"))
async def cmd_register(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    if not command.args:
        await message.answer("Формат: /register +380XXXXXXXXX Ім'я")
        return
    parts = command.args.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: /register +380XXXXXXXXX Ім'я")
        return
    phone, name = normalize_phone(parts[0]), parts[1]
    if get_user_by_phone(phone):
        await message.answer("Клієнт з таким номером вже існує.")
        return
    # Реєструємо із службовим telegram_id (від'ємний), поки клієнт сам не запустить бота —
    # тоді записи об'єднаються під його справжнім id при першому /start.
    fake_id = -abs(hash(phone)) % 10_000_000
    upsert_user(fake_id, phone, name)
    await message.answer(f"Клієнта {name} ({phone}) зареєстровано з балансом 0.")


async def _add_or_sub(message: Message, command: CommandObject, sign: int, label: str):
    if not is_admin(message.from_user.id):
        return
    if not command.args:
        await message.answer(f"Формат: /{label} +380XXXXXXXXX 50 [примітка]")
        return
    parts = command.args.strip().split(maxsplit=2)
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer(f"Формат: /{label} +380XXXXXXXXX 50 [примітка]")
        return
    phone = normalize_phone(parts[0])
    amount = sign * int(parts[1])
    note = parts[2] if len(parts) > 2 else ""
    user = apply_points(phone, amount, note, by_admin=True)
    if not user:
        await message.answer(
            f"Клієнта з номером {phone} не знайдено.\n"
            f"Зареєструвати: /register {phone} Ім'я"
        )
        return
    verb = "Нараховано" if sign > 0 else "Списано"
    await message.answer(
        f"{verb} {abs(amount)} балів для {user['name']} ({phone}).\n"
        f"Новий баланс: {user['balance']}"
    )
    # Повідомляємо самого клієнта, якщо він вже реєструвався через бота (id > 0)
    if user["telegram_id"] > 0:
        try:
            bot = message.bot
            sign_str = "+" if amount >= 0 else ""
            note_str = f" ({note})" if note else ""
            await bot.send_message(
                user["telegram_id"],
                f"🧋 {sign_str}{amount} балів{note_str}\nТвій баланс: {user['balance']}",
            )
        except Exception as e:  # клієнт міг заблокувати бота — не критично
            log.warning("Не вдалось повідомити клієнта %s: %s", user["telegram_id"], e)


@admin_router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject):
    await _add_or_sub(message, command, sign=1, label="add")


@admin_router.message(Command("sub"))
async def cmd_sub(message: Message, command: CommandObject):
    await _add_or_sub(message, command, sign=-1, label="sub")


# --- кнопки адмінської клавіатури ---

@admin_router.message(F.text == "📋 Всі клієнти")
async def btn_list(message: Message):
    await cmd_list(message)


@admin_router.message(F.text == "📢 Розсилка")
async def btn_broadcast(message: Message, state: FSMContext):
    await cmd_broadcast_start(message, state)


@admin_router.message(F.text == "❓ Довідка")
async def btn_help(message: Message):
    await cmd_admin(message)


@admin_router.message(F.text == "🔍 Пошук клієнта")
async def btn_search_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_search)
    await message.answer("Введи номер телефону або останні 4 цифри:")


@admin_router.message(AdminStates.waiting_search)
async def btn_search_run(message: Message, state: FSMContext):
    await state.clear()
    await run_search(message, message.text.strip())


# ---------------------------------------------------------------------------
# Точка входу
# ---------------------------------------------------------------------------

async def main():
    if not BOT_TOKEN:
        raise SystemExit("Задай змінну середовища BOT_TOKEN (токен від @BotFather)")
    db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)
    dp.include_router(client_router)
    await setup_commands(bot)
    log.info("Бот запущено. Адміни: %s", ADMIN_IDS or "не задані!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
