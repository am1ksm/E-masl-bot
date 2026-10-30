"""
Telegram-бот для багов на Vercel + Neon (Postgres).

- POST  -> Telegram webhook (обновления от Telegram)
- GET   -> ежедневная сводка (вызывает Vercel Cron)
"""

import html
import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_ID = int(os.environ["GROUP_ID"])
BUGS_THREAD_ID = int(os.environ["BUGS_THREAD_ID"])
FEATURES_THREAD_ID = int(os.environ.get("FEATURES_THREAD_ID") or 0)
RESULT_THREAD_ID = int(os.environ.get("RESULT_THREAD_ID") or REPORT_THREAD_ID)
TESTED_THREAD_ID = int(os.environ.get("TESTED_THREAD_ID") or 0)  # тема для протестированного, 0 = выключено  # тема «Результат дня»
RESULT_HOUR = 17  # с этого часа (по Алматы) отправляем «Результат дня» вместо напоминания  # тема «Функционал», 0 = выключено
REPORT_THREAD_ID = int(os.environ.get("REPORT_THREAD_ID") or BUGS_THREAD_ID)
DATABASE_URL = os.environ["DATABASE_URL"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Almaty"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bug-bot")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bugs (
    id BIGSERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    thread_id BIGINT,
    source_msg_id BIGINT NOT NULL,
    bot_msg_id BIGINT,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    assignee TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_bug (
    user_id BIGINT PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    thread_id BIGINT,
    created_at BIGINT NOT NULL
)
"""
PENDING_SEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_search (
    user_id BIGINT PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    thread_id BIGINT,
    created_at BIGINT NOT NULL
)
"""
FEATURES_SCHEMA = """
CREATE TABLE IF NOT EXISTS features (
    id BIGSERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    thread_id BIGINT,
    source_msg_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    assignee TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""
PENDING_FEATURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_feature (
    user_id BIGINT PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    thread_id BIGINT,
    created_at BIGINT NOT NULL
)
"""
PENDING_REWORK_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_rework (
    user_id BIGINT PRIMARY KEY,
    kind TEXT NOT NULL,
    item_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    thread_id BIGINT,
    created_at BIGINT NOT NULL
)
"""
PENDING_TTL_SECONDS = 600  # сколько ждать сообщение после нажатия «Новый баг»

STATUS_LABELS = {
    "new": "🆕 Новый",
    "accepted": "✋ Принял",
    "working": "🔧 Делаю",
    "done": "✅ Сделал",
    "tested": "🧪 Протестировал",
    "shipped": "🚀 Залито",
}
BUTTON_LABELS = {
    "accept": "✋ Принял",
    "work": "🔧 Делаю",
    "done": "✅ Сделал",
    "test": "🧪 Протестировал",
    "ship": "🚀 Залито",
    "rework": "🔁 На доработку",
}
ACTION_TO_STATUS = {
    "accept": "accepted",
    "work": "working",
    "done": "done",
    "test": "tested",
    "ship": "shipped",
}
# какие кнопки доступны после каждого закрытого статуса
NEXT_ACTIONS = {"done": "ship", "shipped": "test"}

dp = Dispatcher()


# ---------- тексты и кнопки ----------

def note_line(item) -> str:
    note = item["note"] if "note" in item.keys() else None
    return f"\nПричина доработки: {html.escape(note)}" if note else ""


def bug_card(bug) -> str:
    who = f" — {html.escape(bug['assignee'])}" if bug["assignee"] else ""
    return (
        f"🐞 <b>Баг #{bug['id']}</b>\n"
        f"{html.escape(bug['title'])}\n\n"
        f"Статус: {STATUS_LABELS[bug['status']]}{who}{note_line(bug)}"
    )


def bug_keyboard(bug_id: int, status: str):
    if status in NEXT_ACTIONS:
        action = NEXT_ACTIONS[status]
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=BUTTON_LABELS[action], callback_data=f"bug:{bug_id}:{action}")
        ]])
    if status == "tested":
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=BUTTON_LABELS["rework"], callback_data=f"bug:{bug_id}:rework")
        ]])
    row = [
        InlineKeyboardButton(text=BUTTON_LABELS[a], callback_data=f"bug:{bug_id}:{a}")
        for a in ("accept", "work", "done")
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


def list_keyboard(include_done: bool):
    if include_done:
        button = InlineKeyboardButton(text="🕑 Только открытые", callback_data="list:open")
    else:
        button = InlineKeyboardButton(text="📋 Все баги", callback_data="list:all")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def main_keyboard():
    # постоянные кнопки внизу чата
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Новый баг")],
            [KeyboardButton(text="📋 Все баги"), KeyboardButton(text="🕑 Только открытые")],
            [KeyboardButton(text="✍️ Напишите имя"), KeyboardButton(text="🗑 Удалить баг")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


FEATURE_STATUS = {
    "new": "🆕 Новый",
    "accepted": "✋ Принял",
    "working": "🔧 В процессе",
    "done": "✅ Сделал",
    "tested": "🧪 Протестировал",
    "shipped": "🚀 Залито",
}
FEATURE_BUTTONS = {
    "accept": "✋ Принял",
    "work": "🔧 В процессе",
    "done": "✅ Сделал",
    "test": "🧪 Протестировал",
    "ship": "🚀 Залито",
    "rework": "🔁 На доработку",
}
FEATURE_ACTIONS = {"accept": "accepted", "work": "working", "done": "done", "test": "tested", "ship": "shipped"}
FEATURE_NEXT_ACTIONS = {"done": "ship", "shipped": "test"}


def feature_card(f) -> str:
    who = f" — {html.escape(f['assignee'])}" if f["assignee"] else ""
    return (
        f"💡 <b>Функционал #{f['id']}</b>\n"
        f"{html.escape(f['title'])}\n\n"
        f"Статус: {FEATURE_STATUS[f['status']]}{who}{note_line(f)}"
    )


def feature_keyboard(feature_id: int, status: str):
    if status in FEATURE_NEXT_ACTIONS:
        action = FEATURE_NEXT_ACTIONS[status]
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=FEATURE_BUTTONS[action], callback_data=f"feat:{feature_id}:{action}")
        ]])
    if status == "tested":
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=FEATURE_BUTTONS["rework"], callback_data=f"feat:{feature_id}:rework")
        ]])
    row = [
        InlineKeyboardButton(text=FEATURE_BUTTONS[a], callback_data=f"feat:{feature_id}:{a}")
        for a in ("accept", "work", "done")
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


async def start_rework(cb: CallbackQuery, conn: asyncpg.Connection, kind: str, item_id: int):
    await conn.execute(
        """INSERT INTO pending_rework (user_id, kind, item_id, chat_id, thread_id, created_at)
           VALUES ($1, $2, $3, $4, $5, $6)
           ON CONFLICT (user_id) DO UPDATE
           SET kind = $2, item_id = $3, chat_id = $4, thread_id = $5, created_at = $6""",
        cb.from_user.id, kind, item_id, cb.message.chat.id, cb.message.message_thread_id, int(time.time()),
    )
    await cb.bot.send_message(
        cb.message.chat.id,
        "Напишите причину доработки одним сообщением.",
        message_thread_id=cb.message.message_thread_id,
    )
    await cb.answer()


async def try_rework_reason(message: Message, conn: asyncpg.Connection) -> bool:
    # если бот ждёт причину доработки от этого пользователя, текст — это причина
    text = message.text
    if not text or text.startswith("/"):
        return False
    pending = await conn.fetchrow("SELECT * FROM pending_rework WHERE user_id = $1", message.from_user.id)
    if pending is None:
        return False
    await conn.execute("DELETE FROM pending_rework WHERE user_id = $1", message.from_user.id)
    if int(time.time()) - pending["created_at"] > PENDING_TTL_SECONDS:
        return True

    table = "bugs" if pending["kind"] == "bug" else "features"
    reason = text.strip()
    now = datetime.now(TZ).isoformat(timespec="minutes")
    await conn.execute(
        f"UPDATE {table} SET status = 'working', note = $1, updated_at = $2 WHERE id = $3",
        reason, now, pending["item_id"],
    )
    item = await conn.fetchrow(f"SELECT * FROM {table} WHERE id = $1", pending["item_id"])
    if pending["kind"] == "bug":
        card, keyboard, label = bug_card(item), bug_keyboard(item["id"], "working"), "Баг"
    else:
        card, keyboard, label = feature_card(item), feature_keyboard(item["id"], "working"), "Функционал"

    if item["bot_msg_id"]:
        try:
            await message.bot.edit_message_text(
                card, chat_id=item["chat_id"], message_id=item["bot_msg_id"], reply_markup=keyboard
            )
        except TelegramBadRequest:
            pass
    await message.bot.send_message(
        item["chat_id"],
        f"🔁 {label} #{item['id']} возвращён на доработку.\nПричина: {html.escape(reason)}",
        message_thread_id=pending["thread_id"],
    )
    return True


async def post_tested(bot: Bot, item, kind: str):
    if not TESTED_THREAD_ID:
        return
    link = source_link(item)
    await bot.send_message(
        item["chat_id"],
        f'🧪 <b>Протестировано: {kind} #{item["id"]}</b>\n'
        f'{html.escape(item["title"])}\n'
        f'<a href="{link}">Открыть исходное сообщение</a>',
        message_thread_id=TESTED_THREAD_ID,
    )


def features_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить функционал")],
            [KeyboardButton(text="📋 Все функционалы")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def search_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✍️ Напишите имя")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def user_name(user) -> str:
    return f"@{user.username}" if user.username else user.full_name


def source_link(bug) -> str:
    # ссылка на исходное сообщение (фото + описание) в теме
    chat_part = str(abs(bug["chat_id"]))
    if chat_part.startswith("100"):
        chat_part = chat_part[3:]  # убираем префикс -100 из ID супергруппы
    if bug["thread_id"]:
        return f"https://t.me/c/{chat_part}/{bug['thread_id']}/{bug['source_msg_id']}"
    return f"https://t.me/c/{chat_part}/{bug['source_msg_id']}"


def build_summary(rows, include_done: bool = False) -> str:
    today = datetime.now(TZ).strftime("%d.%m")
    if include_done:
        head = f"📋 Все баги ({today})"
    else:
        head = f"🕑 Сводка багов на 14:00 ({today})"
    if not rows:
        return f"{head}\n\nБагов нет ✨" if include_done else f"{head}\n\nОткрытых багов нет ✨"

    sections = [("new", "🆕 Новые"), ("accepted", "✋ Приняты"), ("working", "🔧 В работе")]
    if include_done:
        sections.append(("done", "✅ Сделанные"))
        sections.append(("shipped", "🚀 Залито"))
        sections.append(("tested", "🧪 Протестированные"))
    lines = [head]
    for key, title in sections:
        items = [b for b in rows if b["status"] == key]
        if not items:
            continue
        lines.append(f"\n<b>{title} ({len(items)})</b>")
        for b in items:
            who = f" — {html.escape(b['assignee'])}" if b["assignee"] else ""
            link = source_link(b)
            lines.append(f'<a href="{link}">#{b["id"]}</a> {html.escape(b["title"])}{who}' + "\n")
    if include_done:
        lines.append(f"\nВсего багов: {len(rows)}")
    else:
        lines.append(f"\nВсего открыто: {len(rows)}")
    return "\n".join(lines)


async def fetch_bugs(conn: asyncpg.Connection, include_done: bool = False):
    if include_done:
        return await conn.fetch("SELECT * FROM bugs ORDER BY id")
    return await conn.fetch(
        "SELECT * FROM bugs WHERE status NOT IN ('done', 'tested', 'shipped') ORDER BY id"
    )


# ---------- хендлеры Telegram ----------

@dp.message(Command("bugs"), F.chat.id == GROUP_ID)
async def on_bugs_command(message: Message, conn: asyncpg.Connection):
    # «/bugs» — только открытые, «/bugs all» — все, включая сделанные
    parts = (message.text or "").split()
    include_done = len(parts) > 1 and parts[1].lower() == "all"
    rows = await fetch_bugs(conn, include_done=include_done)
    await message.bot.send_message(
        message.chat.id,
        build_summary(rows, include_done=include_done),
        message_thread_id=message.message_thread_id,
        reply_markup=list_keyboard(include_done),
    )


@dp.callback_query(F.data.startswith("list:"))
async def on_list_switch(cb: CallbackQuery, conn: asyncpg.Connection):
    include_done = cb.data == "list:all"
    rows = await fetch_bugs(conn, include_done=include_done)
    try:
        await cb.message.edit_text(
            build_summary(rows, include_done=include_done),
            reply_markup=list_keyboard(include_done),
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await cb.answer()


@dp.message(Command("start"), F.chat.id == GROUP_ID)
async def on_start(message: Message):
    await message.bot.send_message(
        message.chat.id,
        "Кнопки включены: используйте их внизу чата.",
        message_thread_id=message.message_thread_id,
        reply_markup=(
            main_keyboard() if message.message_thread_id == BUGS_THREAD_ID
            else features_keyboard() if message.message_thread_id == FEATURES_THREAD_ID
            else search_keyboard()
        ),
    )


@dp.message(F.chat.id == GROUP_ID, F.text.in_({"📋 Все баги", "🕑 Только открытые"}))
async def on_button_list(message: Message, conn: asyncpg.Connection):
    include_done = message.text == "📋 Все баги"
    rows = await fetch_bugs(conn, include_done=include_done)
    await message.bot.send_message(
        message.chat.id,
        build_summary(rows, include_done=include_done),
        message_thread_id=message.message_thread_id,
        reply_markup=list_keyboard(include_done),
    )


async def find_contacts_text(conn: asyncpg.Connection, query: str) -> str:
    tokens = query.split()
    if not tokens:
        return "Напишите ФИО или его часть."
    conditions = " AND ".join(f"full_name ILIKE ${i + 1}" for i in range(len(tokens)))
    rows = await conn.fetch(
        f"SELECT * FROM contacts WHERE {conditions} ORDER BY full_name LIMIT 10",
        *[f"%{t}%" for t in tokens],
    )
    if not rows:
        return "Никого не нашёл. Попробуйте фамилию или другое написание."
    return "\n\n".join(
        f"<b>{html.escape(r['full_name'])}</b>\n"
        f"{html.escape(r['role'])}, {html.escape(r['org'])}\n"
        f"{html.escape(r['email'])}"
        for r in rows
    )


@dp.message(F.chat.id == GROUP_ID, F.text == "✍️ Напишите имя")
async def on_button_ask_name(message: Message, conn: asyncpg.Connection):
    await conn.execute(
        """INSERT INTO pending_search (user_id, chat_id, thread_id, created_at)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (user_id) DO UPDATE SET chat_id = $2, thread_id = $3, created_at = $4""",
        message.from_user.id, message.chat.id, message.message_thread_id, int(time.time()),
    )
    await message.reply("Напишите фамилию, имя или отчество депутата или сотрудника.")


@dp.message(F.chat.id == GROUP_ID, F.message_thread_id == FEATURES_THREAD_ID, F.text == "➕ Добавить функционал")
async def on_button_add_feature(message: Message, conn: asyncpg.Connection):
    await conn.execute(
        """INSERT INTO pending_feature (user_id, chat_id, thread_id, created_at)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (user_id) DO UPDATE SET chat_id = $2, thread_id = $3, created_at = $4""",
        message.from_user.id, message.chat.id, message.message_thread_id, int(time.time()),
    )
    await message.reply(
        "Отправьте описание и фото функционала, по одному сообщению на пункт. "
        "Режим выключится через 10 минут без новых."
    )


def build_features_list(rows) -> str:
    if not rows:
        return "💡 Функционалов пока нет"
    sections = [
        ("new", "🆕 Новые"),
        ("accepted", "✋ Приняты"),
        ("working", "🔧 В процессе"),
        ("done", "✅ Сделанные"),
        ("shipped", "🚀 Залито"),
        ("tested", "🧪 Протестированные"),
    ]
    lines = [f"💡 <b>Все функционалы ({len(rows)})</b>"]
    for key, title in sections:
        items = [f for f in rows if f["status"] == key]
        if not items:
            continue
        lines.append(f"\n<b>{title} ({len(items)})</b>")
        for f in items:
            who = f" — {html.escape(f['assignee'])}" if f["assignee"] else ""
            link = source_link(f)
            lines.append(f'<a href="{link}">#{f["id"]}</a> {html.escape(f["title"])}{who}' + "\n")
    return "\n".join(lines)


@dp.message(F.chat.id == GROUP_ID, F.message_thread_id == FEATURES_THREAD_ID, F.text == "📋 Все функционалы")
async def on_button_all_features(message: Message, conn: asyncpg.Connection):
    rows = await conn.fetch("SELECT * FROM features ORDER BY id")
    await message.bot.send_message(
        message.chat.id,
        build_features_list(rows),
        message_thread_id=message.message_thread_id,
    )


@dp.message(
    F.chat.id == GROUP_ID,
    F.message_thread_id == FEATURES_THREAD_ID,
    F.text | F.photo | F.document,
)
async def on_new_feature(message: Message, conn: asyncpg.Connection):
    if message.text and message.text.startswith("/"):
        return
    if await try_rework_reason(message, conn):
        return

    pending = await conn.fetchrow(
        "SELECT * FROM pending_feature WHERE user_id = $1", message.from_user.id
    )
    if pending is None or int(time.time()) - pending["created_at"] > PENDING_TTL_SECONDS:
        if pending is not None:
            await conn.execute("DELETE FROM pending_feature WHERE user_id = $1", message.from_user.id)
        return

    # режим продлевается после каждого добавленного пункта
    await conn.execute(
        "UPDATE pending_feature SET created_at = $1 WHERE user_id = $2",
        int(time.time()), message.from_user.id,
    )

    raw = (message.text or message.caption or "").strip()
    title = raw.splitlines()[0][:200] if raw else "Без описания"
    now = datetime.now(TZ).isoformat(timespec="minutes")
    feature_id = await conn.fetchval(
        """INSERT INTO features (chat_id, thread_id, source_msg_id, title, status, created_at, updated_at)
           VALUES ($1, $2, $3, $4, 'new', $5, $5) RETURNING id""",
        message.chat.id, message.message_thread_id, message.message_id, title, now,
    )
    feature = await conn.fetchrow("SELECT * FROM features WHERE id = $1", feature_id)
    sent = await message.reply(feature_card(feature), reply_markup=feature_keyboard(feature_id, "new"))
    await conn.execute("UPDATE features SET bot_msg_id = $1 WHERE id = $2", sent.message_id, feature_id)


@dp.callback_query(F.data.startswith("feat:"))
async def on_feature_status(cb: CallbackQuery, conn: asyncpg.Connection):
    _, feature_id_s, action = cb.data.split(":")
    feature = await conn.fetchrow("SELECT * FROM features WHERE id = $1", int(feature_id_s))
    if feature is None:
        await cb.answer("Функционал не найден", show_alert=True)
        return
    if action == "rework":
        if feature["status"] != "tested":
            await cb.answer("Доработка доступна после теста", show_alert=True)
            return
        await start_rework(cb, conn, "feature", feature["id"])
        return
    if feature["status"] in ("done", "tested", "shipped") and FEATURE_NEXT_ACTIONS.get(feature["status"]) != action:
        await cb.answer("Уже закрыто")
        return

    name = user_name(cb.from_user)
    new_status = FEATURE_ACTIONS[action]
    if action in ("accept", "work") or not feature["assignee"]:
        assignee = name
    else:
        assignee = feature["assignee"]

    now = datetime.now(TZ).isoformat(timespec="minutes")
    await conn.execute(
        "UPDATE features SET status = $1, assignee = $2, updated_at = $3 WHERE id = $4",
        new_status, assignee, now, feature["id"],
    )
    feature = await conn.fetchrow("SELECT * FROM features WHERE id = $1", feature["id"])
    try:
        await cb.message.edit_text(
            feature_card(feature),
            reply_markup=feature_keyboard(feature["id"], feature["status"]),
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    if new_status == "tested":
        await post_tested(cb.bot, feature, "функционал")
    await cb.answer(FEATURE_STATUS[new_status])


@dp.message(F.chat.id == GROUP_ID, F.message_thread_id != BUGS_THREAD_ID, F.text)
async def on_name_query(message: Message, conn: asyncpg.Connection):
    if message.text.startswith("/"):
        return
    if await try_rework_reason(message, conn):
        return
    search = await conn.fetchrow(
        "SELECT * FROM pending_search WHERE user_id = $1", message.from_user.id
    )
    if search is None:
        return
    await conn.execute("DELETE FROM pending_search WHERE user_id = $1", message.from_user.id)
    if int(time.time()) - search["created_at"] <= PENDING_TTL_SECONDS:
        await message.reply(await find_contacts_text(conn, message.text.strip()))


@dp.message(F.chat.id == GROUP_ID, F.text == "➕ Новый баг")
async def on_button_new_bug(message: Message, conn: asyncpg.Connection):
    await conn.execute(
        """INSERT INTO pending_bug (user_id, chat_id, thread_id, created_at)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (user_id) DO UPDATE
           SET chat_id = $2, thread_id = $3, created_at = $4""",
        message.from_user.id, message.chat.id, message.message_thread_id, int(time.time()),
    )
    await message.reply(
        "Отправляйте баги: скриншот или описание, по одному сообщению на баг. "
        "Режим работает, пока вы отправляете баги, и выключится через 10 минут без новых."
    )


@dp.message(F.chat.id == GROUP_ID, F.text == "🗑 Удалить баг")
async def on_button_delete_hint(message: Message):
    await message.reply("Чтобы удалить баг, отправьте /delbug и его номер, например: /delbug 5")


@dp.message(Command("delbug"), F.chat.id == GROUP_ID)
async def on_delete_bug(message: Message, conn: asyncpg.Connection):
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("Укажите номер бага: /delbug 5")
        return

    bug_id = int(parts[1])
    bug = await conn.fetchrow("SELECT * FROM bugs WHERE id = $1", bug_id)
    if bug is None:
        await message.reply(f"Баг #{bug_id} не найден.")
        return

    # удаляем карточку бота; исходное сообщение с фото остаётся
    if bug["bot_msg_id"]:
        try:
            await message.bot.delete_message(bug["chat_id"], bug["bot_msg_id"])
        except TelegramBadRequest:
            pass  # карточка уже удалена или у бота нет прав

    await conn.execute("DELETE FROM bugs WHERE id = $1", bug_id)
    await message.reply(f"Баг #{bug_id} удалён.")


@dp.message(
    F.chat.id == GROUP_ID,
    F.message_thread_id == BUGS_THREAD_ID,
    F.text | F.photo | F.document,
)
async def on_new_bug(message: Message, conn: asyncpg.Connection):
    if message.text and message.text.startswith("/"):
        return
    if await try_rework_reason(message, conn):
        return

    # баг создаём только если этот пользователь недавно нажал «➕ Новый баг»
    pending = await conn.fetchrow(
        "SELECT * FROM pending_bug WHERE user_id = $1", message.from_user.id
    )
    if pending is None or int(time.time()) - pending["created_at"] > PENDING_TTL_SECONDS:
        if pending is not None:
            await conn.execute("DELETE FROM pending_bug WHERE user_id = $1", message.from_user.id)
        return
    # режим «несколько багов»: ожидание продлевается после каждого созданного бага
    await conn.execute(
        "UPDATE pending_bug SET created_at = $1 WHERE user_id = $2",
        int(time.time()), message.from_user.id,
    )

    raw = (message.text or message.caption or "").strip()
    title = raw.splitlines()[0][:120] if raw else "Скриншот без описания"
    now = datetime.now(TZ).isoformat(timespec="minutes")

    bug_id = await conn.fetchval(
        """INSERT INTO bugs (chat_id, thread_id, source_msg_id, title, status, created_at, updated_at)
           VALUES ($1, $2, $3, $4, 'new', $5, $5) RETURNING id""",
        message.chat.id, message.message_thread_id, message.message_id, title, now,
    )
    bug = await conn.fetchrow("SELECT * FROM bugs WHERE id = $1", bug_id)

    sent = await message.reply(bug_card(bug), reply_markup=bug_keyboard(bug_id, "new"))
    await conn.execute("UPDATE bugs SET bot_msg_id = $1 WHERE id = $2", sent.message_id, bug_id)
    log.info("Создан баг #%s", bug_id)


@dp.callback_query(F.data.startswith("bug:"))
async def on_status(cb: CallbackQuery, conn: asyncpg.Connection):
    _, bug_id_s, action = cb.data.split(":")
    bug = await conn.fetchrow("SELECT * FROM bugs WHERE id = $1", int(bug_id_s))
    if bug is None:
        await cb.answer("Баг не найден", show_alert=True)
        return
    if action == "rework":
        if bug["status"] != "tested":
            await cb.answer("Доработка доступна после теста", show_alert=True)
            return
        await start_rework(cb, conn, "bug", bug["id"])
        return
    if bug["status"] in ("done", "tested", "shipped") and NEXT_ACTIONS.get(bug["status"]) != action:
        await cb.answer("Баг уже закрыт")
        return

    name = user_name(cb.from_user)
    new_status = ACTION_TO_STATUS[action]
    # «Принял» и «Делаю» ставят исполнителем нажавшего.
    # «Сделал» назначает нажавшего, только если исполнителя ещё нет.
    if action in ("accept", "work") or not bug["assignee"]:
        assignee = name
    else:
        assignee = bug["assignee"]

    now = datetime.now(TZ).isoformat(timespec="minutes")
    await conn.execute(
        "UPDATE bugs SET status = $1, assignee = $2, updated_at = $3 WHERE id = $4",
        new_status, assignee, now, bug["id"],
    )
    bug = await conn.fetchrow("SELECT * FROM bugs WHERE id = $1", bug["id"])

    try:
        await cb.message.edit_text(bug_card(bug), reply_markup=bug_keyboard(bug["id"], bug["status"]))
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    if new_status == "tested":
        await post_tested(cb.bot, bug, "баг")
    await cb.answer(STATUS_LABELS[new_status])


# ---------- HTTP-обработчики ----------

async def connect_db() -> asyncpg.Connection:
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(SCHEMA)
    await conn.execute(PENDING_SCHEMA)
    await conn.execute(PENDING_SEARCH_SCHEMA)
    await conn.execute(FEATURES_SCHEMA)
    await conn.execute(PENDING_FEATURE_SCHEMA)
    await conn.execute(PENDING_REWORK_SCHEMA)
    await conn.execute("ALTER TABLE bugs ADD COLUMN IF NOT EXISTS note TEXT")
    await conn.execute("ALTER TABLE features ADD COLUMN IF NOT EXISTS note TEXT")
    await conn.execute("ALTER TABLE features ADD COLUMN IF NOT EXISTS bot_msg_id BIGINT")
    return conn


async def handle_webhook(body: bytes, headers: dict) -> int:
    if WEBHOOK_SECRET and headers.get("x-telegram-bot-api-secret-token") != WEBHOOK_SECRET:
        return 403

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    conn = await connect_db()
    try:
        update = Update.model_validate(json.loads(body), context={"bot": bot})
        await dp.feed_update(bot, update, conn=conn)
    finally:
        await conn.close()
        await bot.session.close()
    return 200


def build_reminder(open_rows, done_rows) -> str:
    now = datetime.now(TZ).strftime("%d.%m %H:%M")
    lines = [f"🔔 <b>Напоминание по багам ({now})</b>"]
    if not open_rows and not done_rows:
        lines.append("\nОткрытых багов нет, сегодня ничего не закрыто ✨")
        return "\n".join(lines)

    sections = [("new", "🆕 Новые"), ("accepted", "✋ Приняты"), ("working", "🔧 В работе")]
    for key, title in sections:
        items = [b for b in open_rows if b["status"] == key]
        if not items:
            continue
        lines.append(f"\n<b>{title} ({len(items)})</b>")
        for b in items:
            who = f" — {html.escape(b['assignee'])}" if b["assignee"] else ""
            lines.append(f'<a href="{source_link(b)}">#{b["id"]}</a> {html.escape(b["title"])}{who}' + "\n")

    if done_rows:
        lines.append(f"\n<b>✅ Сделано сегодня ({len(done_rows)})</b>")
        for b in done_rows:
            who = f" — {html.escape(b['assignee'])}" if b["assignee"] else ""
            lines.append(f'<a href="{source_link(b)}">#{b["id"]}</a> {html.escape(b["title"])}{who}' + "\n")

    lines.append(f"\nОткрыто: {len(open_rows)}")
    return "\n".join(lines)


def build_day_result(bugs_done, features_done) -> str:
    today = datetime.now(TZ).strftime("%d.%m")
    lines = [f"🏁 <b>Результат дня ({today})</b>"]
    if not bugs_done and not features_done:
        lines.append("\nЗа сегодня ничего не закрыто.")
        return "\n".join(lines)

    if bugs_done:
        lines.append(f"\n<b>🐞 Баги закрыты ({len(bugs_done)})</b>")
        for b in bugs_done:
            who = f" — {html.escape(b['assignee'])}" if b["assignee"] else ""
            lines.append(f'<a href="{source_link(b)}">#{b["id"]}</a> {html.escape(b["title"])}{who}' + "\n")

    if features_done:
        lines.append(f"\n<b>💡 Функционал сделан ({len(features_done)})</b>")
        for f in features_done:
            who = f" — {html.escape(f['assignee'])}" if f["assignee"] else ""
            lines.append(f'<a href="{source_link(f)}">#{f["id"]}</a> {html.escape(f["title"])}{who}' + "\n")

    return "\n".join(lines)


async def handle_summary(headers: dict) -> int:
    if CRON_SECRET and headers.get("authorization") != f"Bearer {CRON_SECRET}":
        return 401

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    conn = await connect_db()
    try:
        now = datetime.now(TZ)
        today_like = f"{now.strftime('%Y-%m-%d')}%"
        done_bugs = await conn.fetch(
            "SELECT * FROM bugs WHERE status IN ('done', 'tested', 'shipped') AND updated_at LIKE $1 ORDER BY id",
            today_like,
        )
        if now.hour >= RESULT_HOUR:
            done_features = await conn.fetch(
                "SELECT * FROM features WHERE status IN ('done', 'tested', 'shipped') AND updated_at LIKE $1 ORDER BY id",
                today_like,
            )
            await bot.send_message(
                GROUP_ID,
                build_day_result(done_bugs, done_features),
                message_thread_id=RESULT_THREAD_ID,
            )
        else:
            open_rows = await fetch_bugs(conn)
            await bot.send_message(
                GROUP_ID,
                build_reminder(open_rows, done_bugs),
                message_thread_id=REPORT_THREAD_ID,
                reply_markup=list_keyboard(False),
            )
    finally:
        await conn.close()
        await bot.session.close()
    return 200


# ---------- ASGI-вход для Vercel ----------

async def app(scope, receive, send):
    if scope["type"] != "http":
        return

    method = scope["method"]
    headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}

    body = b""
    if method == "POST":
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break

    if method == "POST":
        status = await handle_webhook(body, headers)
    elif method == "GET":
        status = await handle_summary(headers)
    else:
        status = 405

    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"text/plain; charset=utf-8")],
    })
    await send({"type": "http.response.body", "body": b"ok" if status == 200 else b"error"})
