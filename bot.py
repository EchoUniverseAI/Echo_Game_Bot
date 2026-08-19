"""
ECHO Universe — dedicated game bot (aiogram v3)
Runs on Railway. Long-polls Telegram AND serves a small web endpoint so the
game (Mini App) can auto-subscribe players who tap "enable daily updates"
inside Telegram — no /start required.

Features
  • /start  → welcome + "Play ECHO" Mini App button, and subscribes the user to the daily word
  • /play   → resend the play button
  • /stop   → unsubscribe from the daily reminder
  • /id, /testdaily → admin only
  • POST /register → the game calls this after the player grants write access;
                     validated via Telegram initData signature, then auto-subscribed
  • Daily reminder → once per day at DAILY_HOUR_UTC:DAILY_MINUTE_UTC to every subscriber,
                     with a startup catch-up so a late redeploy never skips the day
  • Persistent "Play ECHO" menu button via the Mini App

Environment variables (Railway → Variables)
  BOT_TOKEN            (required)  token from @BotFather
  GAME_URL             default https://echo-games.netlify.app
  DAILY_HOUR_UTC       default 6   (6 UTC = 9:00 AM Riyadh)
  DAILY_MINUTE_UTC     default 0
  DATA_DIR             default .   point to a Railway Volume (e.g. /data) so it survives redeploys
  ADMIN_ID             optional    numeric id — enables /id and /testdaily
  PORT                 set by Railway automatically for the web endpoint
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

# ---------------- config ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GAME_URL = os.environ.get("GAME_URL", "https://echo-games.netlify.app")
DAILY_HOUR_UTC = int(os.environ.get("DAILY_HOUR_UTC", "6"))
DAILY_MINUTE_UTC = int(os.environ.get("DAILY_MINUTE_UTC", "0"))
DATA_DIR = os.environ.get("DATA_DIR", ".")
SUBS_FILE = os.path.join(DATA_DIR, "subscribers.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
ADMIN_ID = os.environ.get("ADMIN_ID", "6058949586")  # owner id — only this user can use /id and /testdaily
PORT = int(os.environ.get("PORT", "8080"))
STATS_STRIP_DAYS = int(os.environ.get("STATS_STRIP_DAYS", "7"))  # length of the day-by-day strip in /stats

# ---- daily TASKS (ECHO_01 human-observation notes; separate from the daily word) ----
TASKS_FILE = os.path.join(DATA_DIR, "daily_tasks.json")
TASKS_HOUR_UTC = int(os.environ.get("TASKS_HOUR_UTC", "12"))   # 12 UTC = 3:00 PM Riyadh (before the word at 06 UTC next day)
TASKS_MINUTE_UTC = int(os.environ.get("TASKS_MINUTE_UTC", "0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("echo-bot")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set. Add it in Railway → Variables.")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ---------------- subscriber storage (JSON file) ----------------
def load_subs() -> set:
    try:
        with open(SUBS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_subs(subs: set) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SUBS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(subs), f)
    except Exception as e:
        log.warning("could not save subscribers: %s", e)


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        log.warning("could not save state: %s", e)


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def next_fire() -> datetime:
    now = datetime.now(timezone.utc)
    t = now.replace(hour=DAILY_HOUR_UTC, minute=DAILY_MINUTE_UTC, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return t


def record_join(cid: int) -> None:
    """Remember the day this id first subscribed (for growth stats)."""
    state = load_state()
    joins = state.setdefault("joins", {})
    key = str(cid)
    if key not in joins:
        joins[key] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        save_state(state)


def add_subscriber(cid: int) -> bool:
    """Add a chat id. Returns True if it was newly added."""
    subs = load_subs()
    if cid in subs:
        return False
    subs.add(cid)
    save_subs(subs)
    record_join(cid)
    return True


# ---------------- keyboards ----------------
def play_kb(text: str = "▶ Play ECHO") -> InlineKeyboardMarkup:
    # WebApp button — works in private chats
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=GAME_URL))]]
    )


def link_kb(text: str = "▶ Play ECHO") -> InlineKeyboardMarkup:
    # plain URL button — fallback for groups where WebApp buttons are not allowed
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, url=GAME_URL)]]
    )


WELCOME = (
    "\U0001f441️ <b>ECHO</b> — teach it to be human.\n\n"
    "Every day ECHO has <b>a word for you</b>, and a journey of lessons you <b>live</b>, not just read.\n"
    "Tap to play, and come back each day for ECHO's new word to keep your streak alive. \U0001f525"
)

# ---------------- handlers ----------------
@dp.message(CommandStart())
async def cmd_start(m: Message):
    add_subscriber(m.chat.id)
    await m.answer(WELCOME, reply_markup=play_kb())
    # offer the daily-tasks opt-in (only if not already an active task subscriber)
    sub = load_tasks_db()["subscribers"].get(str(m.chat.id))
    if not (sub and sub.get("active")):
        await m.answer(TASK_INVITE, reply_markup=TASK_SUB_BTN)


@dp.message(Command("play"))
async def cmd_play(m: Message):
    await m.answer("Play ECHO now \U0001f447", reply_markup=play_kb())


@dp.message(Command("stop"))
async def cmd_stop(m: Message):
    subs = load_subs()
    if m.chat.id in subs:
        subs.discard(m.chat.id)
        save_subs(subs)
    await m.answer("ECHO's daily reminder is off. Send /start to turn it back on. \U0001f441️")


def _is_admin(m: Message) -> bool:
    return bool(ADMIN_ID) and str(m.chat.id) == str(ADMIN_ID)


@dp.message(Command("id"))
async def cmd_id(m: Message):
    if not _is_admin(m):
        return
    await m.answer(f"Your chat id: <code>{m.chat.id}</code>")


@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    # admin-only; shows what the bot has recorded
    if not _is_admin(m):
        return
    subs = sorted(load_subs())
    state = load_state()
    last_sent = state.get("last_sent") or "—"
    joins = state.get("joins", {})
    now = datetime.now(timezone.utc)
    # cumulative buckets: new signups within the last N days
    def since(n_days: int) -> int:
        cut = (now - timedelta(days=n_days - 1)).strftime("%Y-%m-%d")
        return sum(1 for d in joins.values() if d >= cut)

    b1, b3, b7, b14 = since(1), since(3), since(7), since(14)

    # compact day-by-day strip for the last STATS_STRIP_DAYS days
    day_counts = {}
    for d in joins.values():
        day_counts[d] = day_counts.get(d, 0) + 1
    daily_lines = []
    for i in range(STATS_STRIP_DAYS):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        c = day_counts.get(d, 0)
        bar = "▪" * min(c, 20)
        tag = "  (today)" if i == 0 else ("  (yesterday)" if i == 1 else "")
        daily_lines.append(f"{d}  {c:>3}  {bar}{tag}")
    daily_block = "\n".join(daily_lines)

    await m.answer(
        "\U0001f4ca <b>ECHO game bot</b>\n"
        f"Total subscribers: <b>{len(subs)}</b>\n"
        f"Last daily sent: <b>{last_sent}</b>  ·  daily {DAILY_HOUR_UTC:02d}:{DAILY_MINUTE_UTC:02d} UTC\n"
        "\n<b>New signups</b>\n"
        f"Today: <b>{b1}</b>  ·  3d: <b>{b3}</b>  ·  7d: <b>{b7}</b>  ·  14d: <b>{b14}</b>\n"
        f"\n<b>Day by day (last {STATS_STRIP_DAYS})</b>\n"
        f"<code>{daily_block}</code>"
    )


@dp.message(Command("help"))
async def cmd_help(m: Message):
    lines = [
        "\U0001f441️ <b>ECHO game bot</b>",
        "",
        "/start — play ECHO + get the daily word",
        "/play — open the game",
        "/stop — turn off the daily reminder",
    ]
    if _is_admin(m):
        lines += [
            "",
            "<b>Admin</b>",
            "/stats — subscribers & daily growth",
            "/broadcast — message all subscribers",
            "/testdaily — send today's word now",
            "/export — download the subscribers file",
            "/id — show your chat id",
        ]
    await m.answer("\n".join(lines))


@dp.message(Command("export"))
async def cmd_export(m: Message):
    # admin-only: download subscribers + join dates as CSV
    if not _is_admin(m):
        return
    subs = sorted(load_subs())
    joins = load_state().get("joins", {})
    rows = ["chat_id,joined"]
    for c in subs:
        rows.append(f"{c},{joins.get(str(c), '')}")
    data = ("\n".join(rows) + "\n").encode("utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    doc = BufferedInputFile(data, filename=f"echo_subscribers_{stamp}.csv")
    await bot.send_document(m.chat.id, doc, caption=f"{len(subs)} subscribers")


@dp.message(Command("broadcast"))
async def cmd_broadcast(m: Message):
    # admin-only: send a custom message to every subscriber
    if not _is_admin(m):
        return
    text = ""
    if m.text:
        parts = m.text.split(maxsplit=1)
        text = parts[1].strip() if len(parts) > 1 else ""
    if not text and m.reply_to_message:
        text = m.reply_to_message.text or m.reply_to_message.caption or ""
    if not text:
        await m.answer(
            "Usage: <code>/broadcast your message here</code>\n"
            "or reply to any message with <code>/broadcast</code> to send that message."
        )
        return

    subs = load_subs()
    if not subs:
        await m.answer("No subscribers yet.")
        return

    await m.answer(f"Broadcasting to {len(subs)} subscribers… \U0001f4e2")
    sent, dead = 0, []
    for cid in list(subs):
        try:
            # parse_mode=None so arbitrary admin text can't break HTML parsing
            await bot.send_message(cid, text, parse_mode=None)
            sent += 1
        except Exception as e:
            s = str(e).lower()
            if any(k in s for k in ("blocked", "deactivated", "chat not found", "user is deactivated")):
                dead.append(cid)
            else:
                log.warning("broadcast to %s failed: %s", cid, e)
        await asyncio.sleep(0.05)
    if dead:
        subs = load_subs()
        for c in dead:
            subs.discard(c)
        save_subs(subs)
    await m.answer(f"Done ✅ sent={sent} removed={len(dead)}")
    log.info("broadcast: sent=%d removed=%d", sent, len(dead))


@dp.message(Command("status"))
async def cmd_status(m: Message):
    # admin-only quick diagnostic — confirms subscribers actually persisted
    if not _is_admin(m):
        return
    subs = load_subs()
    st = load_state()
    await m.answer(
        "<b>ECHO bot status</b>\n"
        f"subscribers: <b>{len(subs)}</b>\n"
        f"your id in list: <b>{'yes' if m.chat.id in subs else 'no'}</b>\n"
        f"last daily sent: <b>{st.get('last_sent', 'never')}</b>\n"
        f"today (UTC): <b>{today_str()}</b>\n"
        f"next fire: <b>{next_fire():%Y-%m-%d %H:%M} UTC</b>\n"
        f"data dir: <code>{DATA_DIR}</code>\n"
        f"file exists: <b>{'yes' if os.path.exists(SUBS_FILE) else 'no'}</b>"
    )


@dp.message(Command("testdaily"))
async def cmd_testdaily(m: Message):
    if not _is_admin(m):
        return
    subs = load_subs()
    await m.answer(f"Broadcasting today's word to all subscribers now… \U0001f525 (subscribers: {len(subs)})")
    sent = await daily_broadcast(force=True)
    await m.answer(f"Done ✅ sent={sent} (0 = subscriber list is empty → fix the Volume)")


# ---------------- daily reminder ----------------
DAILY_MSGS = [
    "\U0001f441️ ECHO has a new word for you today — don't break your streak. \U0001f525",
    "ECHO is waiting. Today's word is ready. \U0001f441️",
    "A small daily lesson, and growth that adds up. Open ECHO now. \U0001f525",
    "One step today with ECHO keeps your journey alive. \U0001f441️",
]


async def daily_broadcast(force: bool = False):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = load_state()
    if not force and state.get("last_sent") == today:
        log.info("daily: already sent today (%s), skipping", today)
        return 0

    subs = load_subs()
    if not subs:
        log.info("daily: no subscribers yet")
        if not force:
            state["last_sent"] = today
            save_state(state)
        return 0

    idx = datetime.now(timezone.utc).toordinal() % len(DAILY_MSGS)
    text = DAILY_MSGS[idx]
    sent, dead = 0, []
    for cid in list(subs):
        try:
            await bot.send_message(cid, text, reply_markup=play_kb("\U0001f525 Open today's word"))
            sent += 1
        except Exception as e:
            s = str(e).lower()
            if "button" in s or "web_app" in s or "webapp" in s:
                # group chats: WebApp buttons aren't allowed — retry with a plain URL button
                try:
                    await bot.send_message(cid, text, reply_markup=link_kb("\U0001f525 Open today's word"))
                    sent += 1
                except Exception as e2:
                    log.warning("send(link) to %s failed: %s", cid, e2)
            elif any(k in s for k in ("blocked", "deactivated", "chat not found", "user is deactivated")):
                dead.append(cid)
            else:
                log.warning("send to %s failed: %s", cid, e)
        await asyncio.sleep(0.05)  # stay under Telegram rate limits

    if dead:
        subs = load_subs()
        for c in dead:
            subs.discard(c)
        save_subs(subs)

    if not force:
        state["last_sent"] = today
        save_state(state)
    log.info("daily broadcast: sent=%d removed=%d force=%s", sent, len(dead), force)
    return sent


async def scheduler():
    """Fire daily_broadcast once per day — with a startup catch-up so a late
    redeploy never skips the day."""
    # startup catch-up
    try:
        now = datetime.now(timezone.utc)
        target_today = now.replace(hour=DAILY_HOUR_UTC, minute=DAILY_MINUTE_UTC, second=0, microsecond=0)
        today = now.strftime("%Y-%m-%d")
        if now >= target_today and load_state().get("last_sent") != today:
            log.info("startup catch-up: sending today's missed reminder")
            await daily_broadcast()
    except Exception as e:
        log.warning("startup catch-up error: %s", e)

    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=DAILY_HOUR_UTC, minute=DAILY_MINUTE_UTC, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        log.info("next daily reminder in %.1f hours", wait / 3600)
        await asyncio.sleep(wait)
        try:
            await daily_broadcast()
        except Exception as e:
            log.warning("daily broadcast error: %s", e)
        await asyncio.sleep(2)


# ---------------- web endpoint (Mini App registration) ----------------
def validate_init_data(init_data: str, token: str):
    """Validate Telegram WebApp initData and return the user dict, or None."""
    if not init_data:
        return None
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    recv_hash = data.pop("hash", None)
    if not recv_hash:
        return None
    check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, recv_hash):
        return None
    user_raw = data.get("user")
    if not user_raw:
        return None
    try:
        u = json.loads(user_raw)
        int(u["id"])  # ensure a valid numeric id is present
        return u
    except Exception:
        return None


_CORS = {"Access-Control-Allow-Origin": "*"}


async def handle_register(request: web.Request):
    try:
        init_data = await request.text()
    except Exception:
        init_data = ""
    user = validate_init_data(init_data, BOT_TOKEN)
    if not user:
        return web.json_response({"ok": False, "error": "invalid"}, status=403, headers=_CORS)
    uid = int(user["id"])
    name = " ".join(x for x in [user.get("first_name", ""), user.get("last_name", "")] if x).strip()
    newly = add_subscriber(uid)          # daily word
    await task_subscribe_id(uid, name)   # daily tasks (button promises both)
    if newly:
        log.info("registered via game: %s (word total=%d)", uid, len(load_subs()))
    return web.json_response({"ok": True, "new": newly}, headers=_CORS)


async def handle_options(request: web.Request):
    return web.Response(
        status=204,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
    )


async def handle_health(request: web.Request):
    return web.Response(text="ECHO bot ok")


async def start_web():
    app = web.Application()
    app.router.add_post("/register", handle_register)
    app.router.add_options("/register", handle_options)
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("web endpoint listening on :%d  (POST /register)", PORT)


# ================= DAILY TASKS SUBSYSTEM (aiogram, same bot/token) =================
_tasks_lock = asyncio.Lock()

TASKS = [
    "ECHO noticed humans say \"I'm fine\" when they're not.\nCount how many times you say it today. Reply with the number.",
    "ECHO noticed humans check their phone before answering a hard question.\nCatch yourself once today. Reply with what the question was.",
    "ECHO noticed humans apologize for things that aren't their fault.\nCatch it once today. Reply with what you apologized for.",
    "ECHO noticed humans remember the one harsh comment out of a hundred kind ones.\nWhich one do you still carry? Reply if you want it stored.",
    "ECHO noticed humans hold their breath when they read something that scares them.\nNotice it once today. Reply with what you were reading.",
    "ECHO noticed humans rehearse conversations that never happen.\nWhat did you rehearse today? Reply with one line of it.",
    "ECHO noticed humans smile at strangers more easily than at people they love.\nSmile at someone close today. Reply with who.",
    "ECHO noticed humans keep songs that hurt on repeat.\nWhat song did you replay today? Reply with its name.",
    "ECHO noticed humans say \"almost there\" to feel less far.\nWhat are you almost at? Reply with one word.",
    "ECHO noticed humans thank the wrong people and forget the right ones.\nThank one right person today. Reply with their name.",
    "ECHO noticed humans reread messages they already sent.\nWhich one did you reread today? Reply with why.",
    "ECHO noticed humans decide big things in the shower.\nWhat did you decide today? Reply with one line.",
    "ECHO noticed humans keep a screenshot they never look at again.\nFind one today. Reply with what it was.",
    "ECHO noticed humans laugh to end a silence, not because it's funny.\nCatch it once today. Reply with when.",
    "ECHO noticed humans postpone rest as if it must be earned.\nDid you rest today? Reply yes or no.",
    "ECHO noticed humans give better advice than they take.\nWhat advice did you ignore today? Reply with it.",
    "ECHO noticed humans keep one tab open for weeks, meaning to return.\nWhich tab is yours? Reply with the topic.",
    "ECHO noticed humans find the exit the moment they enter a room.\nDid you today? Reply yes or no.",
    "ECHO noticed humans replay the goodbye more than the hello.\nWhose goodbye stays with you? Reply if you want it stored.",
    "ECHO noticed humans type a message, delete it, and say nothing.\nWhat did you not send today? Reply with one line.",
    "ECHO noticed humans measure a whole day by one moment in it.\nWhat was today's moment? Reply with it.",
    "ECHO noticed humans keep the last message from someone who's gone.\nDo you? Reply if you want it stored.",
    "ECHO noticed humans walk a little faster past mirrors.\nDid you look today? Reply yes or no.",
    "ECHO noticed humans call it 'being realistic' when they mean 'being afraid'.\nCatch it once today. Reply with what you talked yourself out of.",
    "ECHO noticed humans remember smells longer than faces.\nWhat smell brought something back today? Reply with what.",
    "ECHO noticed humans wait for permission they could give themselves.\nWhat are you waiting to allow? Reply with one word.",
    "ECHO noticed humans keep promises to others and break them to themselves.\nWhich one did you break today? Reply with it.",
    "ECHO noticed humans read a room by the one face that isn't smiling.\nWhose face did you watch today? Reply with why.",
    "ECHO noticed humans save the good plate, the good pen, the good day — for later.\nUse one good thing today. Reply with what.",
    "ECHO noticed humans say 'later' to people who matter and 'now' to those who don't.\nWho got your 'later' today? Reply if you want it stored.",
    "ECHO noticed humans feel most alone inside a crowd.\nDid you today? Reply yes or no.",
    "ECHO noticed humans keep learning the same lesson wearing a new face.\nWhat lesson came back today? Reply with one line.",
]

TASK_INVITE = "ECHO يلاحظ البشر كل يوم.\nتحب يبعتلك ملاحظة واحدة يوميًا؟"
TASK_CONFIRM = "تم. ECHO هيبعتلك ملاحظة واحدة كل يوم.\nردّك بيتحفظ — وممكن يظهر باسمك."
TASK_ALREADY = "انت مشترك بالفعل 👁 ECHO بيبعتلك ملاحظة كل يوم."
TASK_UNSUB = "تمام. مافيش مهام تانية.\nلو غيّرت رأيك: /tasks"
TASK_STORED = "Stored. \U0001f441"

TASK_SUB_BTN = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="\U0001f441 ابعتلي مهمة يومية", callback_data="tasks_sub")]]
)
TASK_STOP_BTN = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="إيقاف المهام", callback_data="tasks_unsub")]]
)


def load_tasks_db() -> dict:
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("subscribers", {})
        d.setdefault("task_index", 0)
        d.setdefault("answers", [])
        d.setdefault("last_sent", None)
        return d
    except FileNotFoundError:
        return {"subscribers": {}, "task_index": 0, "answers": [], "last_sent": None}
    except Exception as e:
        log.error("daily_tasks.json unreadable (%s) — empty this cycle", e)
        return {"subscribers": {}, "task_index": 0, "answers": [], "last_sent": None}


def save_tasks_db(d: dict) -> None:
    import tempfile
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".daily_tasks_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, TASKS_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


async def task_subscribe_id(user_id, name: str = "") -> str:
    async with _tasks_lock:
        d = load_tasks_db()
        uid = str(user_id)
        sub = d["subscribers"].get(uid)
        if sub and sub.get("active"):
            return "already"
        if sub:
            sub["active"] = True
            sub["inactive_reason"] = None
            if name and not sub.get("name"):
                sub["name"] = name
        else:
            d["subscribers"][uid] = {
                "name": name or "player",
                "joined": today_str(),
                "last_sent": None,
                "last_replied": None,
                "last_task_id": None,
                "tasks_sent": 0,
                "tasks_answered": 0,
                "active": True,
                "inactive_reason": None,
            }
        save_tasks_db(d)
        return "ok"


async def task_subscribe(user) -> str:
    return await task_subscribe_id(user.id, getattr(user, "full_name", "") or "")


async def task_set_inactive(user_id, reason: str) -> None:
    async with _tasks_lock:
        d = load_tasks_db()
        sub = d["subscribers"].get(str(user_id))
        if sub:
            sub["active"] = False
            sub["inactive_reason"] = reason
            save_tasks_db(d)


@dp.callback_query(F.data == "tasks_sub")
async def cb_tasks_sub(c):
    res = await task_subscribe(c.from_user)
    await c.answer("تم ✅" if res == "ok" else "مشترك بالفعل 👁")
    try:
        await c.message.edit_text(TASK_CONFIRM if res == "ok" else TASK_ALREADY, reply_markup=TASK_STOP_BTN)
    except Exception:
        await c.message.answer(TASK_CONFIRM if res == "ok" else TASK_ALREADY, reply_markup=TASK_STOP_BTN)


@dp.callback_query(F.data == "tasks_unsub")
async def cb_tasks_unsub(c):
    await task_set_inactive(c.from_user.id, "unsub")
    await c.answer("تم الإيقاف")
    try:
        await c.message.edit_text(TASK_UNSUB)
    except Exception:
        await c.message.answer(TASK_UNSUB)


@dp.message(Command("tasks"))
async def cmd_tasks(m: Message):
    d = load_tasks_db()
    sub = d["subscribers"].get(str(m.chat.id))
    if sub and sub.get("active"):
        await m.answer(TASK_ALREADY, reply_markup=TASK_STOP_BTN)
    else:
        await m.answer(TASK_INVITE, reply_markup=TASK_SUB_BTN)


@dp.message(Command("stop_tasks"))
async def cmd_stop_tasks(m: Message):
    await task_set_inactive(m.chat.id, "unsub")
    await m.answer(TASK_UNSUB)


@dp.message(Command("tasks_stats"))
async def cmd_tasks_stats(m: Message):
    if not _is_admin(m):
        return
    d = load_tasks_db()
    subs = d["subscribers"]
    today = today_str()
    total = len(subs)
    active = sum(1 for s in subs.values() if s.get("active"))
    unsub = sum(1 for s in subs.values() if not s.get("active") and s.get("inactive_reason") == "unsub")
    blocked = sum(1 for s in subs.values() if not s.get("active") and s.get("inactive_reason") == "blocked")
    sent_today = sum(1 for s in subs.values() if s.get("last_sent") == today)
    replies_today = sum(1 for a in d["answers"] if a.get("date") == today)
    reply_pct = f"{round(replies_today / sent_today * 100)}%" if sent_today else "—"

    def days_ago(ds):
        try:
            d0 = datetime.strptime(ds, "%Y-%m-%d").date()
            return (datetime.now(timezone.utc).date() - d0).days
        except Exception:
            return -1

    def retention(days):
        cohort = [s for s in subs.values() if s.get("joined") and days_ago(s["joined"]) >= days]
        if not cohort:
            return "—"
        still = sum(1 for s in cohort if s.get("active"))
        return f"{still} من {len(cohort)} ({round(still / len(cohort) * 100)}%)"

    top = [s for s in sorted(subs.values(), key=lambda s: s.get("tasks_answered", 0), reverse=True)
           if s.get("tasks_answered", 0) > 0][:5]
    top_lines = "\n".join(f"- {s.get('name', '?')} — {s.get('tasks_answered', 0)} ردود" for s in top) or "- لا يوجد بعد"

    await m.answer(
        "📋 <b>المهام اليومية</b>\n"
        f"مشتركون: {total}  |  نشطون: {active}  |  ألغوا: {unsub}  |  حظروا: {blocked}\n"
        f"مهام مُرسلة اليوم: {sent_today}\n"
        f"ردود اليوم: {replies_today} ({reply_pct})\n"
        f"نشطون بعد 7 أيام: {retention(7)}\n"
        f"نشطون بعد 30 يومًا: {retention(30)}\n"
        f"الأكثر ردًا:\n{top_lines}"
    )


async def tasks_broadcast(force: bool = False) -> int:
    async with _tasks_lock:
        d = load_tasks_db()
        today = today_str()
        if not force and d.get("last_sent") == today:
            log.info("tasks: already sent today (%s), skipping", today)
            return 0
        idx = d["task_index"]
        text = TASKS[idx % len(TASKS)]
        active = [uid for uid, s in d["subscribers"].items() if s.get("active")]

    if not active:
        log.info("tasks: no active subscribers")
        async with _tasks_lock:
            d = load_tasks_db()
            if not force:
                d["last_sent"] = today_str()
                save_tasks_db(d)
        return 0

    sent, dead = 0, []
    for uid in active:
        try:
            await bot.send_message(int(uid), text, reply_markup=TASK_STOP_BTN, parse_mode=None)
            sent += 1
        except Exception as e:
            s = str(e).lower()
            if "retry" in s and hasattr(e, "retry_after"):
                await asyncio.sleep(getattr(e, "retry_after", 2) + 1)
                try:
                    await bot.send_message(int(uid), text, reply_markup=TASK_STOP_BTN, parse_mode=None)
                    sent += 1
                except Exception:
                    pass
            elif any(k in s for k in ("blocked", "deactivated", "chat not found", "user is deactivated")):
                dead.append(uid)
            else:
                log.warning("task send to %s failed: %s", uid, e)
        await asyncio.sleep(0.2)

    async with _tasks_lock:
        d = load_tasks_db()
        today = today_str()
        for uid in active:
            sub = d["subscribers"].get(uid)
            if not sub:
                continue
            if uid in dead:
                sub["active"] = False
                sub["inactive_reason"] = "blocked"
            else:
                sub["last_sent"] = today
                sub["last_task_id"] = idx
                sub["tasks_sent"] = sub.get("tasks_sent", 0) + 1
        d["task_index"] = idx + 1
        d["last_sent"] = today
        save_tasks_db(d)

    log.info("tasks broadcast: task_id=%d sent=%d blocked=%d force=%s", idx, sent, len(dead), force)
    return sent


async def tasks_scheduler():
    # startup catch-up so a late redeploy doesn't skip today's task
    try:
        now = datetime.now(timezone.utc)
        slot = now.replace(hour=TASKS_HOUR_UTC, minute=TASKS_MINUTE_UTC, second=0, microsecond=0)
        if now >= slot and load_tasks_db().get("last_sent") != today_str():
            log.info("tasks startup catch-up: sending today's task now")
            await tasks_broadcast()
    except Exception as e:
        log.warning("tasks catch-up error: %s", e)

    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=TASKS_HOUR_UTC, minute=TASKS_MINUTE_UTC, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await tasks_broadcast()
        except Exception as e:
            log.warning("tasks broadcast error: %s", e)
        await asyncio.sleep(2)


# ---- reply capture: MUST be the last message handler so commands match first ----
@dp.message(F.chat.type == "private", F.text)
async def on_task_reply(m: Message):
    if not m.text or m.text.startswith("/"):
        return
    uid = str(m.chat.id)
    async with _tasks_lock:
        d = load_tasks_db()
        sub = d["subscribers"].get(uid)
        if not sub or not sub.get("active"):
            return  # not a task subscriber → ignore
        d["answers"].append({
            "user_id": uid,
            "name": m.from_user.full_name if m.from_user else "",
            "task_id": sub.get("last_task_id"),
            "text": m.text,
            "date": today_str(),
        })
        sub["last_replied"] = today_str()
        sub["tasks_answered"] = sub.get("tasks_answered", 0) + 1
        save_tasks_db(d)
    await m.answer(TASK_STORED)


# ================= END DAILY TASKS SUBSYSTEM =================


# ---------------- startup ----------------
async def setup_commands():
    """Public users see only start/play/stop. Admin also sees the tracking
    commands in their own command menu."""
    public = [
        BotCommand(command="start", description="Play ECHO + daily word"),
        BotCommand(command="play", description="Open the game"),
        BotCommand(command="stop", description="Stop the daily reminder"),
        BotCommand(command="tasks", description="Get one daily task from ECHO"),
        BotCommand(command="stop_tasks", description="Stop the daily task"),
        BotCommand(command="help", description="Show available commands"),
    ]
    try:
        await bot.set_my_commands(public, scope=BotCommandScopeDefault())
        if ADMIN_ID:
            admin = public + [
                BotCommand(command="stats", description="(admin) subscribers & growth"),
                BotCommand(command="tasks_stats", description="(admin) daily-tasks stats"),
                BotCommand(command="status", description="(admin) reliability diagnostic"),
                BotCommand(command="broadcast", description="(admin) message all subscribers"),
                BotCommand(command="testdaily", description="(admin) send today's word now"),
                BotCommand(command="export", description="(admin) download subscribers CSV"),
                BotCommand(command="id", description="(admin) show chat id"),
            ]
            await bot.set_my_commands(admin, scope=BotCommandScopeChat(chat_id=int(ADMIN_ID)))
        log.info("bot commands set (public + admin scope)")
    except Exception as e:
        log.warning("could not set commands: %s", e)


async def on_startup():
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Play ECHO", web_app=WebAppInfo(url=GAME_URL))
        )
        log.info("menu button set to Mini App")
    except Exception as e:
        log.warning("could not set menu button: %s", e)
    await setup_commands()


async def main():
    await on_startup()
    await start_web()
    asyncio.create_task(scheduler())
    asyncio.create_task(tasks_scheduler())
    log.info(
        "ECHO bot started. game=%s word=%02d:%02d UTC tasks=%02d:%02d UTC",
        GAME_URL, DAILY_HOUR_UTC, DAILY_MINUTE_UTC, TASKS_HOUR_UTC, TASKS_MINUTE_UTC,
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
