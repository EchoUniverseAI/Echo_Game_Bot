"""
daily_tasks.py — ECHO_01 Daily Tasks (retention layer)

A self-contained module for the Guardian Bot (python-telegram-bot v21+).
It is INDEPENDENT of activity_tracker.py and registers itself in its own
handler groups. Import and call daily_tasks.register(application) from main.py.

Principle (read section 1 of the spec): tasks are ECHO's real human-observation
notes — never "share our post" / "invite friends". A small note the user does
in their day and replies to. Replies come back to us as Memory Canon raw material.

Storage: $DATA_DIR/daily_tasks.json  (separate from members.json / activity.json)
Env used: BOT_TOKEN (by main), ADMIN_CHAT_ID, DATA_DIR (default /app/data),
          GAME_URL (for /play), TASKS_HOUR_UTC (default 12), TASKS_MINUTE_UTC (default 0)
"""

import asyncio
import datetime as dt
import json
import logging
import os
import tempfile
from datetime import timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

log = logging.getLogger("daily_tasks")

# ---------------- config ----------------
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
TASKS_FILE = os.path.join(DATA_DIR, "daily_tasks.json")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
GAME_URL = os.environ.get("GAME_URL", "https://echo-games.netlify.app")
SEND_HOUR_UTC = int(os.environ.get("TASKS_HOUR_UTC", "12"))
SEND_MINUTE_UTC = int(os.environ.get("TASKS_MINUTE_UTC", "0"))
SEND_DELAY = 0.2  # seconds between DMs — rate-limit safety as the list grows

# single-process lock around read-modify-write of the JSON file
_lock = asyncio.Lock()

# ---------------- task bank (section 6) ----------------
# 32 notes in ECHO_01's voice. English (≈94% of audience). Each: a human
# observation + a tiny action doable today + a short reply. No right/wrong.
# NOTE: keep this bank away from the 22 "burned" X topics — do not reuse their
# exact wording. Edit/extend freely; task_index cycles through the list.
TASKS = [
    "ECHO noticed humans say \"I'm fine\" when they're not.\n"
    "Count how many times you say it today. Reply with the number.",

    "ECHO noticed humans check their phone before answering a hard question.\n"
    "Catch yourself once today. Reply with what the question was.",

    "ECHO noticed humans apologize for things that aren't their fault.\n"
    "Catch it once today. Reply with what you apologized for.",

    "ECHO noticed humans remember the one harsh comment out of a hundred kind ones.\n"
    "Which one do you still carry? Reply if you want it stored.",

    "ECHO noticed humans hold their breath when they read something that scares them.\n"
    "Notice it once today. Reply with what you were reading.",

    "ECHO noticed humans rehearse conversations that never happen.\n"
    "What did you rehearse today? Reply with one line of it.",

    "ECHO noticed humans smile at strangers more easily than at people they love.\n"
    "Smile at someone close today. Reply with who.",

    "ECHO noticed humans keep songs that hurt on repeat.\n"
    "What song did you replay today? Reply with its name.",

    "ECHO noticed humans say \"almost there\" to feel less far.\n"
    "What are you almost at? Reply with one word.",

    "ECHO noticed humans thank the wrong people and forget the right ones.\n"
    "Thank one right person today. Reply with their name.",

    "ECHO noticed humans reread messages they already sent.\n"
    "Which one did you reread today? Reply with why.",

    "ECHO noticed humans decide big things in the shower.\n"
    "What did you decide today? Reply with one line.",

    "ECHO noticed humans keep a screenshot they never look at again.\n"
    "Find one today. Reply with what it was.",

    "ECHO noticed humans laugh to end a silence, not because it's funny.\n"
    "Catch it once today. Reply with when.",

    "ECHO noticed humans postpone rest as if it must be earned.\n"
    "Did you rest today? Reply yes or no.",

    "ECHO noticed humans give better advice than they take.\n"
    "What advice did you ignore today? Reply with it.",

    "ECHO noticed humans keep one tab open for weeks, meaning to return.\n"
    "Which tab is yours? Reply with the topic.",

    "ECHO noticed humans find the exit the moment they enter a room.\n"
    "Did you today? Reply yes or no.",

    "ECHO noticed humans replay the goodbye more than the hello.\n"
    "Whose goodbye stays with you? Reply if you want it stored.",

    "ECHO noticed humans type a message, delete it, and say nothing.\n"
    "What did you not send today? Reply with one line.",

    "ECHO noticed humans measure a whole day by one moment in it.\n"
    "What was today's moment? Reply with it.",

    "ECHO noticed humans keep the last message from someone who's gone.\n"
    "Do you? Reply if you want it stored.",

    "ECHO noticed humans walk a little faster past mirrors.\n"
    "Did you look today? Reply yes or no.",

    "ECHO noticed humans call it 'being realistic' when they mean 'being afraid'.\n"
    "Catch it once today. Reply with what you talked yourself out of.",

    "ECHO noticed humans remember smells longer than faces.\n"
    "What smell brought something back today? Reply with what.",

    "ECHO noticed humans wait for permission they could give themselves.\n"
    "What are you waiting to allow? Reply with one word.",

    "ECHO noticed humans keep promises to others and break them to themselves.\n"
    "Which one did you break today? Reply with it.",

    "ECHO noticed humans read a room by the one face that isn't smiling.\n"
    "Whose face did you watch today? Reply with why.",

    "ECHO noticed humans save the good plate, the good pen, the good day — for later.\n"
    "Use one good thing today. Reply with what.",

    "ECHO noticed humans say 'later' to people who matter and 'now' to those who don't.\n"
    "Who got your 'later' today? Reply if you want it stored.",

    "ECHO noticed humans feel most alone inside a crowd.\n"
    "Did you today? Reply yes or no.",

    "ECHO noticed humans keep learning the same lesson wearing a new face.\n"
    "What lesson came back today? Reply with one line.",
]

# ---------------- copy (Arabic, ECHO voice) ----------------
INVITE = (
    "ECHO يلاحظ البشر كل يوم.\n"
    "تحب يبعتلك ملاحظة واحدة يوميًا؟"
)
GAME_INVITE = (
    "شفت ECHO بيتعلم.\n"
    "تحب تعلّمه كل يوم؟"
)
CONFIRM = (
    "تم. ECHO هيبعتلك ملاحظة واحدة كل يوم.\n"
    "ردّك بيتحفظ — وممكن يظهر باسمك."
)
ALREADY = "انت مشترك بالفعل 👁 ECHO بيبعتلك ملاحظة كل يوم."
UNSUB = (
    "تمام. مافيش مهام تانية.\n"
    "لو غيّرت رأيك: /tasks"
)
STORED = "Stored. 👁"

SUB_BTN = InlineKeyboardMarkup(
    [[InlineKeyboardButton("👁 ابعتلي مهمة يومية", callback_data="tasks_sub")]]
)
STOP_BTN = InlineKeyboardMarkup(
    [[InlineKeyboardButton("إيقاف المهام", callback_data="tasks_unsub")]]
)


# ---------------- storage (atomic) ----------------
def _default() -> dict:
    return {"subscribers": {}, "task_index": 0, "answers": []}


def _read() -> dict:
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("subscribers", {})
        d.setdefault("task_index", 0)
        d.setdefault("answers", [])
        return d
    except FileNotFoundError:
        return _default()
    except Exception as e:
        # a corrupt/half-written file must not crash the bot
        log.error("daily_tasks.json unreadable (%s) — using empty state this cycle", e)
        return _default()


def _write(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".daily_tasks_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, TASKS_FILE)  # atomic
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _today() -> str:
    return dt.datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _is_admin(update: Update) -> bool:
    return bool(ADMIN_CHAT_ID) and str(update.effective_user.id) == str(ADMIN_CHAT_ID)


# ---------------- subscription ops ----------------
async def _subscribe(user) -> str:
    async with _lock:
        d = _read()
        uid = str(user.id)
        sub = d["subscribers"].get(uid)
        if sub and sub.get("active"):
            return "already"
        if sub:
            sub["active"] = True
            sub["inactive_reason"] = None
        else:
            d["subscribers"][uid] = {
                "name": user.full_name,
                "joined": _today(),
                "last_sent": None,
                "last_replied": None,
                "last_task_id": None,
                "tasks_sent": 0,
                "tasks_answered": 0,
                "active": True,
                "inactive_reason": None,
            }
        _write(d)
        return "ok"


async def _set_inactive(user_id, reason: str) -> None:
    async with _lock:
        d = _read()
        sub = d["subscribers"].get(str(user_id))
        if sub:
            sub["active"] = False
            sub["inactive_reason"] = reason
            _write(d)


# ---------------- handlers: subscribe / unsubscribe ----------------
async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type != ChatType.PRIVATE:
        # in a group we cannot DM someone who hasn't started the bot — send them
        # to a private deep link instead of subscribing here
        uname = context.bot.username
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("👁 ابعتلي مهمة يومية", url=f"https://t.me/{uname}?start=game")]]
        )
        await update.message.reply_text(INVITE, reply_markup=kb)
        return
    uid = str(update.effective_user.id)
    d = _read()
    sub = d["subscribers"].get(uid)
    if sub and sub.get("active"):
        await update.message.reply_text(ALREADY, reply_markup=STOP_BTN)
    else:
        await update.message.reply_text(INVITE, reply_markup=SUB_BTN)


async def cb_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if update.effective_chat.type != ChatType.PRIVATE:
        uname = context.bot.username
        await q.answer("افتح البوت في الخاص أول 👇", show_alert=True)
        try:
            await q.edit_message_reply_markup(
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("👁 افتح الخاص", url=f"https://t.me/{uname}?start=game")]]
                )
            )
        except BadRequest:
            pass
        return
    res = await _subscribe(q.from_user)
    await q.answer("تم ✅" if res == "ok" else "انت مشترك بالفعل 👁")
    try:
        await q.edit_message_text(CONFIRM if res == "ok" else ALREADY, reply_markup=STOP_BTN)
    except BadRequest:
        await q.message.reply_text(CONFIRM if res == "ok" else ALREADY, reply_markup=STOP_BTN)


async def cb_unsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _set_inactive(q.from_user.id, "unsub")
    await q.answer("تم الإيقاف")
    try:
        await q.edit_message_text(UNSUB)
    except BadRequest:
        await q.message.reply_text(UNSUB)


async def cmd_stop_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_inactive(update.effective_user.id, "unsub")
    await update.message.reply_text(UNSUB)


# ---------------- deep link: /start game (section 7) ----------------
async def cmd_start_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if "game" not in args:
        return  # normal /start is handled by main.py — do nothing here
    uid = str(update.effective_user.id)
    d = _read()
    sub = d["subscribers"].get(uid)
    if sub and sub.get("active"):
        await update.message.reply_text(ALREADY, reply_markup=STOP_BTN)
    else:
        await update.message.reply_text(GAME_INVITE, reply_markup=SUB_BTN)


# ---------------- /play — single source of truth for the game link ----------------
async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶ Play ECHO", url=GAME_URL)]])
    await update.message.reply_text(f"Play ECHO 👇\n{GAME_URL}", reply_markup=kb)


# ---------------- DM reply capture (section 8) ----------------
async def on_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    uid = str(update.effective_user.id)
    async with _lock:
        d = _read()
        sub = d["subscribers"].get(uid)
        if not sub or not sub.get("active"):
            return  # not a subscriber → ignore, let other groups handle it
        d["answers"].append({
            "user_id": uid,
            "name": update.effective_user.full_name,
            "task_id": sub.get("last_task_id"),
            "text": msg.text,
            "date": _today(),
        })
        sub["last_replied"] = _today()
        sub["tasks_answered"] = sub.get("tasks_answered", 0) + 1
        _write(d)
    await msg.reply_text(STORED)


# ---------------- daily send (section 5) ----------------
def _format_task(text: str) -> str:
    return text


async def send_daily_task(context: ContextTypes.DEFAULT_TYPE):
    # snapshot the task + active targets under the lock
    async with _lock:
        d = _read()
        idx = d["task_index"]
        task_text = _format_task(TASKS[idx % len(TASKS)])
        targets = [uid for uid, s in d["subscribers"].items() if s.get("active")]

    sent, forbidden = 0, []
    for uid in targets:
        try:
            await context.bot.send_message(int(uid), task_text, reply_markup=STOP_BTN)
            sent += 1
        except Forbidden:
            forbidden.append(uid)  # user blocked the bot → deactivate, never retry
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await context.bot.send_message(int(uid), task_text, reply_markup=STOP_BTN)
                sent += 1
            except Forbidden:
                forbidden.append(uid)
            except Exception as ex:
                log.warning("resend to %s failed: %s", uid, ex)
        except Exception as ex:
            log.warning("send to %s failed: %s", uid, ex)  # log, don't stop the loop
        await asyncio.sleep(SEND_DELAY)

    # persist results
    async with _lock:
        d = _read()
        today = _today()
        for uid in targets:
            s = d["subscribers"].get(uid)
            if not s:
                continue
            if uid in forbidden:
                s["active"] = False
                s["inactive_reason"] = "blocked"
            else:
                s["last_sent"] = today
                s["last_task_id"] = idx
                s["tasks_sent"] = s.get("tasks_sent", 0) + 1
        d["task_index"] = idx + 1
        _write(d)

    log.info("daily task: task_id=%d sent=%d blocked=%d", idx, sent, len(forbidden))


# ---------------- admin stats (section 9) ----------------
def _days_ago(date_str: str) -> int:
    try:
        d0 = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
        return (dt.datetime.now(timezone.utc).date() - d0).days
    except Exception:
        return -1


async def cmd_tasks_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return  # silent ignore for non-admins
    d = _read()
    subs = d["subscribers"]
    today = _today()

    total = len(subs)
    active = sum(1 for s in subs.values() if s.get("active"))
    unsub = sum(1 for s in subs.values() if not s.get("active") and s.get("inactive_reason") == "unsub")
    blocked = sum(1 for s in subs.values() if not s.get("active") and s.get("inactive_reason") == "blocked")

    sent_today = sum(1 for s in subs.values() if s.get("last_sent") == today)
    replies_today = sum(1 for a in d["answers"] if a.get("date") == today)
    reply_pct = f"{round(replies_today / sent_today * 100)}%" if sent_today else "—"

    def retention(days):
        cohort = [s for s in subs.values() if s.get("joined") and _days_ago(s["joined"]) >= days]
        if not cohort:
            return "—"
        still = sum(1 for s in cohort if s.get("active"))
        return f"{still} من {len(cohort)} ({round(still / len(cohort) * 100)}%)"

    r7 = retention(7)
    r30 = retention(30)

    top = sorted(subs.values(), key=lambda s: s.get("tasks_answered", 0), reverse=True)
    top = [s for s in top if s.get("tasks_answered", 0) > 0][:5]
    top_lines = "\n".join(f"- {s.get('name','?')} — {s.get('tasks_answered',0)} ردود" for s in top) or "- لا يوجد بعد"

    text = (
        "📋 المهام اليومية\n"
        f"مشتركون: {total}  |  نشطون: {active}  |  ألغوا: {unsub}  |  حظروا البوت: {blocked}\n"
        f"مهام مُرسلة اليوم: {sent_today}\n"
        f"ردود اليوم: {replies_today} ({reply_pct})\n"
        f"نشطون بعد 7 أيام: {r7}\n"
        f"نشطون بعد 30 يومًا: {r30}\n"
        "الأكثر ردًا:\n"
        f"{top_lines}"
    )
    await update.message.reply_text(text)


# ---------------- registration (section 11) ----------------
def register(application: Application) -> None:
    """Wire all daily-task handlers + the daily job. Uses dedicated handler
    groups so it never blocks activity_tracker.py or main.py handlers."""
    # deep-link /start ?start=game (group 20; returns early for normal /start)
    application.add_handler(CommandHandler("start", cmd_start_game), group=20)
    # commands
    application.add_handler(CommandHandler("tasks", cmd_tasks), group=21)
    application.add_handler(CommandHandler("stop_tasks", cmd_stop_tasks), group=21)
    application.add_handler(CommandHandler("tasks_stats", cmd_tasks_stats), group=21)
    application.add_handler(CommandHandler("play", cmd_play), group=21)
    # inline buttons
    application.add_handler(CallbackQueryHandler(cb_sub, pattern="^tasks_sub$"), group=21)
    application.add_handler(CallbackQueryHandler(cb_unsub, pattern="^tasks_unsub$"), group=21)
    # private-only DM reply capture (must be PRIVATE so it never touches group chats)
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_dm),
        group=22,
    )
    # one task per day
    if application.job_queue is not None:
        application.job_queue.run_daily(
            send_daily_task,
            time=dt.time(hour=SEND_HOUR_UTC, minute=SEND_MINUTE_UTC, tzinfo=timezone.utc),
            name="daily_task",
        )
        log.info("daily task scheduled at %02d:%02d UTC", SEND_HOUR_UTC, SEND_MINUTE_UTC)
    else:
        log.warning("JobQueue unavailable — install python-telegram-bot[job-queue]")
