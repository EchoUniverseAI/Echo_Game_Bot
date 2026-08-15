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

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
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


@dp.message(Command("testdaily"))
async def cmd_testdaily(m: Message):
    if not _is_admin(m):
        return
    subs = load_subs()
    await m.answer(f"Broadcasting today's word to all subscribers now… \U0001f525 (subscribers: {len(subs)})")
    await daily_broadcast(force=True)
    await m.answer("Done ✅ (check the logs for sent count)")


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
        return

    subs = load_subs()
    if not subs:
        log.info("daily: no subscribers yet")
        if not force:
            state["last_sent"] = today
            save_state(state)
        return

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
    """Validate Telegram WebApp initData and return the user's numeric id, or None."""
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
        return int(json.loads(user_raw)["id"])
    except Exception:
        return None


_CORS = {"Access-Control-Allow-Origin": "*"}


async def handle_register(request: web.Request):
    try:
        init_data = await request.text()
    except Exception:
        init_data = ""
    uid = validate_init_data(init_data, BOT_TOKEN)
    if not uid:
        return web.json_response({"ok": False, "error": "invalid"}, status=403, headers=_CORS)
    newly = add_subscriber(uid)
    if newly:
        log.info("registered via game: %s (total=%d)", uid, len(load_subs()))
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


# ---------------- startup ----------------
async def setup_commands():
    """Public users see only start/play/stop. Admin also sees the tracking
    commands in their own command menu."""
    public = [
        BotCommand(command="start", description="Play ECHO + daily word"),
        BotCommand(command="play", description="Open the game"),
        BotCommand(command="stop", description="Stop the daily reminder"),
    ]
    try:
        await bot.set_my_commands(public, scope=BotCommandScopeDefault())
        if ADMIN_ID:
            admin = public + [
                BotCommand(command="stats", description="(admin) subscribers & growth"),
                BotCommand(command="testdaily", description="(admin) send today's word now"),
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
    log.info("ECHO bot started. game=%s daily=%02d:%02d UTC", GAME_URL, DAILY_HOUR_UTC, DAILY_MINUTE_UTC)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
