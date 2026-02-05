import os
import re
import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))  # группа, куда постим запросы
REQUEST_TTL_SECONDS = int(os.getenv("REQUEST_TTL_SECONDS", str(48 * 3600)))
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")  # например Real_Flat_Bot

ALLOWLIST_RAW = os.getenv("AGENTS_ALLOWLIST", "").strip()
AGENTS_ALLOWLIST = {u.strip().lstrip("@").lower() for u in ALLOWLIST_RAW.split(",") if u.strip()}


@dataclass
class Request:
    rid: str
    author_id: int
    author_username: Optional[str]
    created_at: float
    status: str = "active"  # active / closed
    group_message_id: Optional[int] = None
    data: Dict[str, str] = field(default_factory=dict)


REQUESTS: Dict[str, Request] = {}
USER_STATE: Dict[int, Dict] = {}
USER_ACTIVE_RID: Dict[int, str] = {}
RID_COUNTER = 0


def next_rid() -> str:
    global RID_COUNTER
    RID_COUNTER += 1
    return f"R{RID_COUNTER:03d}"


def is_allowed_agent(update: Update) -> bool:
    if not AGENTS_ALLOWLIST:
        return True
    u = update.effective_user
    if not u:
        return False
    return (u.username or "").lower() in AGENTS_ALLOWLIST


REQUEST_FIELDS = [
    ("district", "Район? (например: Сабуртало)"),
    ("budget", "Бюджет? (например: до $900)"),
    ("rooms", "Комнаты? (например: 2к / 3к)"),
    ("term", "Срок? (например: 12 мес)"),
    ("viewing", "Когда смотреть? (например: сегодня/завтра)"),
    ("comment", "Комментарий? (можно коротко)"),
]


def build_request_text(req: Request) -> str:
    d = req.data
    lines = [
        f"🔎 ЗАПРОС #{req.rid}",
    ]
    if req.status == "closed":
        lines.append("🟢 СТАТУС: ЗАКРЫТ")
    lines += [
        f"Район: {d.get('district','—')}",
        f"Бюджет: {d.get('budget','—')}",
        f"Комнаты: {d.get('rooms','—')}",
        f"Срок: {d.get('term','—')}",
        f"Смотреть: {d.get('viewing','—')}",
        f"Комментарий: {d.get('comment','—')}",
        "",
        "⬇️ Нажми кнопку, чтобы отправить варианты автору (приватно).",
    ]
    return "\n".join(lines)


def request_keyboard(req: Request) -> InlineKeyboardMarkup:
    deep_link = f"https://t.me/{BOT_USERNAME}?start=reply_{req.rid}" if BOT_USERNAME else "https://t.me/"
    kb = [
        [InlineKeyboardButton("📩 Отправить вариант", url=deep_link)],
        [InlineKeyboardButton("✅ Закрыть запрос", callback_data=f"close_{req.rid}")],
    ]
    return InlineKeyboardMarkup(kb)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    u = update.effective_user
    if not u:
        return

    if context.args:
        payload = context.args[0]
        m = re.match(r"reply_(R\d+)", payload)
        if m:
            rid = m.group(1)
            req = REQUESTS.get(rid)
            if not req or req.status != "active":
                await update.message.reply_text("Этот запрос уже закрыт или не найден.")
                return
            if not is_allowed_agent(update):
                await update.message.reply_text("Доступ ограничен.")
                return
            USER_ACTIVE_RID[u.id] = rid
            await update.message.reply_text(
                f"Ок. Пришли ссылки для #{rid}.\n"
                "Можно несколько сообщений подряд.\n"
                "Когда закончил — напиши: ГОТОВО"
            )
            return

    await update.message.reply_text(
        "Привет! Я бот Real Flats.\n\n"
        "Команды:\n"
        "/request — создать запрос (в личке)\n"
        "/my — мои активные запросы\n"
        "/help — помощь"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(
        "Как пользоваться:\n"
        "1) /request в личке — создаёшь запрос по форме\n"
        "2) Я публикую запрос в группе\n"
        "3) Агенты жмут «Отправить вариант» и кидают ссылки мне в личку\n"
        "4) Я отправляю ссылки только автору запроса\n"
        "5) Запрос живёт 48 часов или пока автор не закроет."
    )


async def request_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat and update.effective_chat.type != "private":
        await update.message.reply_text("Создавай запрос в личке боту.")
        return
    if not is_allowed_agent(update):
        await update.message.reply_text("Доступ ограничен.")
        return

    u = update.effective_user
    rid = next_rid()
    req = Request(rid=rid, author_id=u.id, author_username=u.username, created_at=time.time())
    REQUESTS[rid] = req
    USER_STATE[u.id] = {"mode": "request", "rid": rid, "step": 0}
    await update.message.reply_text(f"Создаём запрос #{rid}.\n{REQUEST_FIELDS[0][1]}")


async def my_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    u = update.effective_user
    active = [r for r in REQUESTS.values() if r.author_id == u.id and r.status == "active"]
    if not active:
        await update.message.reply_text("У тебя нет активных запросов.")
        return
    lines = ["Твои активные запросы:"]
    for r in active:
        d = r.data
        lines.append(f"• #{r.rid} — {d.get('district','—')} | {d.get('budget','—')} | {d.get('rooms','—')}")
    await update.message.reply_text("\n".join(lines))


async def close_request(req: Request, context: ContextTypes.DEFAULT_TYPE):
    if req.status == "closed":
        return
    req.status = "closed"
    if req.group_message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=GROUP_CHAT_ID,
                message_id=req.group_message_id,
                text=build_request_text(req),
                reply_markup=None,
                disable_web_page_preview=True,
            )
        except Exception:
            pass
    try:
        await context.bot.send_message(req.author_id, f"🟢 Запрос #{req.rid} закрыт.")
    except Exception:
        pass


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    m = re.match(r"close_(R\d+)", q.data or "")
    if not m:
        return
    rid = m.group(1)
    req = REQUESTS.get(rid)
    if not req:
        return
    if update.effective_user and update.effective_user.id != req.author_id:
        await q.answer("Закрыть может только автор.", show_alert=True)
        return
    await close_request(req, context)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def handle_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    u = update.effective_user
    if not u:
        return
    text = (update.message.text or "").strip()

    st = USER_STATE.get(u.id)
    if st and st.get("mode") == "request":
        rid = st["rid"]
        step = st["step"]
        key, _ = REQUEST_FIELDS[step]
        REQUESTS[rid].data[key] = text
        step += 1
        if step >= len(REQUEST_FIELDS):
            USER_STATE.pop(u.id, None)
            req = REQUESTS[rid]
            msg = await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=build_request_text(req),
                reply_markup=request_keyboard(req),
                disable_web_page_preview=True,
            )
            req.group_message_id = msg.message_id
            await update.message.reply_text(f"Готово ✅ Запрос #{rid} опубликован в группе.")
            return
        st["step"] = step
        await update.message.reply_text(REQUEST_FIELDS[step][1])
        return

    rid = USER_ACTIVE_RID.get(u.id)
    if rid:
        req = REQUESTS.get(rid)
        if not req or req.status != "active":
            USER_ACTIVE_RID.pop(u.id, None)
            await update.message.reply_text("Этот запрос уже закрыт.")
            return

        if text.lower() == "готово":
            USER_ACTIVE_RID.pop(u.id, None)
            await update.message.reply_text("Ок ✅ Отправка завершена.")
            return

        await context.bot.send_message(
            chat_id=req.author_id,
            text=f"🏠 Вариант по #{rid} от @{u.username or u.first_name}:\n{text}",
            disable_web_page_preview=False,
        )

        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=f"✅ @{u.username or u.first_name} отправил вариант автору #{rid}",
        )

        await update.message.reply_text("Отправлено ✅ Скидывай ещё ссылки или напиши ГОТОВО.")
        return

    await update.message.reply_text("Напиши /request чтобы создать запрос.")


async def ttl_watcher(app: Application):
    while True:
        now = time.time()
        for req in list(REQUESTS.values()):
            if req.status != "active":
                continue
            if now - req.created_at >= REQUEST_TTL_SECONDS:
                try:
                    await app.bot.send_message(
                        chat_id=req.author_id,
                        text=f"⏳ Запрос #{req.rid} живёт уже 48 часов.\nАктуально?\n"
                             f"Напиши: ДА (продлить) или НЕТ (закрыть)."
                    )
                except Exception:
                    pass
                req.created_at = now + 3600  # чтобы не спамить
        await asyncio.sleep(600)


async def handle_yes_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    u = update.effective_user
    if not u:
        return
    text = (update.message.text or "").strip().lower()
    if text not in {"да", "нет"}:
        return
    candidates = [r for r in REQUESTS.values() if r.author_id == u.id and r.status == "active"]
    if not candidates:
        return
    req = sorted(candidates, key=lambda r: r.created_at)[-1]
    if text == "да":
        req.created_at = time.time()
        await update.message.reply_text(f"Ок ✅ Продлил запрос #{req.rid} ещё на 48 часов.")
    else:
        await close_request(req, context)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    if GROUP_CHAT_ID == 0:
        raise RuntimeError("GROUP_CHAT_ID is required")
    if not BOT_USERNAME:
        raise RuntimeError("BOT_USERNAME is required")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("request", request_cmd))
    app.add_handler(CommandHandler("my", my_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & filters.Regex(r"^(да|нет)$"), handle_yes_no))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_private_text))

    app.post_init = lambda application: asyncio.create_task(ttl_watcher(application))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
