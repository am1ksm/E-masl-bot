"""
Telegram-бот для багов на Vercel + Neon (Postgres).

- POST  -> Telegram webhook (обновления от Telegram)
- GET   -> ежедневная сводка (вызывает Vercel Cron)
"""

import html
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_ID = int(os.environ["GROUP_ID"])
BUGS_THREAD_ID = int(os.environ["BUGS_THREAD_ID"])
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

STATUS_LABELS = {
    "new": "🆕 Новый",
    "accepted": "✋ Принял",
    "working": "🔧 Делаю",
    "done": "✅ Сделал",
}
BUTTON_LABELS = {"accept": "✋ Принял", "work": "🔧 Делаю", "done": "✅ Сделал"}
ACTION_TO_STATUS = {"accept": "accepted", "work": "working", "done": "done"}

dp = Dispatcher()


# ---------- тексты и кнопки ----------

def bug_card(bug) -> str:
    who = f" — {html.escape(bug['assignee'])}" if bug["assignee"] else ""
    return (
        f"🐞 <b>Баг #{bug['id']}</b>\n"
        f"{html.escape(bug['title'])}\n\n"
        f"Статус: {STATUS_LABELS[bug['status']]}{who}"
    )


def bug_keyboard(bug_id: int, status: str):
    if status == "done":
        return None
    row = [
        InlineKeyboardButton(text=BUTTON_LABELS[a], callback_data=f"bug:{bug_id}:{a}")
        for a in ("accept", "work", "done")
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


def user_name(user) -> str:
    return f"@{user.username}" if user.username else user.full_name


def build_summary(rows) -> str:
    today = datetime.now(TZ).strftime("%d.%m")
    head = f"🕑 Сводка багов на 14:00 ({today})"
    if not rows:
        return f"{head}\n\nОткрытых багов нет ✨"

    sections = [("new", "🆕 Новые"), ("accepted", "✋ Приняты"), ("working", "🔧 В работе")]
    lines = [head]
    for key, title in sections:
        items = [b for b in rows if b["status"] == key]
        if not items:
            continue
        lines.append(f"\n<b>{title} ({len(items)})</b>")
        for b in items:
            who = f" — {html.escape(b['assignee'])}" if b["assignee"] else ""
            lines.append(f"#{b['id']} {html.escape(b['title'])}{who}")
    lines.append(f"\nВсего открыто: {len(rows)}")
    return "\n".join(lines)


async def fetch_open_bugs(conn: asyncpg.Connection):
    return await conn.fetch("SELECT * FROM bugs WHERE status != 'done' ORDER BY id")


# ---------- хендлеры Telegram ----------

@dp.message(
    F.chat.id == GROUP_ID,
    F.message_thread_id == BUGS_THREAD_ID,
    F.text | F.photo | F.document,
)
async def on_new_bug(message: Message, conn: asyncpg.Connection):
    if message.text and message.text.startswith("/"):
        return

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
    if bug["status"] == "done":
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
    await cb.answer(STATUS_LABELS[new_status])


@dp.message(Command("bugs"), F.chat.id == GROUP_ID)
async def on_bugs_command(message: Message, conn: asyncpg.Connection):
    rows = await fetch_open_bugs(conn)
    await message.bot.send_message(
        message.chat.id, build_summary(rows), message_thread_id=message.message_thread_id
    )


# ---------- HTTP-обработчики ----------

async def connect_db() -> asyncpg.Connection:
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(SCHEMA)
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


async def handle_summary(headers: dict) -> int:
    if CRON_SECRET and headers.get("authorization") != f"Bearer {CRON_SECRET}":
        return 401

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    conn = await connect_db()
    try:
        rows = await fetch_open_bugs(conn)
        await bot.send_message(GROUP_ID, build_summary(rows), message_thread_id=REPORT_THREAD_ID)
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
