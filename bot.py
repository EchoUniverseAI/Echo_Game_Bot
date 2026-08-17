"""
ECHO Universe — dedicated game bot (aiogram v3)
Runs on Railway as a long-polling worker + tiny HTTP server (/register, /).

Daily-reminder hardening in this version
  • Persists the last date it broadcast (state.json) so a restart never double-sends.
  • STARTUP CATCH-UP: if today's send time has already passed and today wasn't sent yet,
    it sends on boot — so a redeploy after the daily hour no longer skips the day.
  • /status (admin) prints subscriber count, last_sent, next fire time, and the data path —
    the fastest way to confirm subscribers actually persisted.

Environment variables (Railway → Variables)
  BOT_TOKEN         (required)
  GAME_URL          default https://echo-games.netlify.app
  DAILY_HOUR_UTC    default 6    (6 UTC = 9:00 AM Riyadh)
  DAILY_MINUTE_UTC  default 0
  DATA_DIR          default .    → MUST point to a Railway Volume (e.g. /data) or subscribers are lost on redeploy
  ADMIN_ID          owner id     (enables /id, /testdaily, /status)
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
ADMIN_ID = os.environ.get("ADMIN_ID", "6058949586")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("echo-bot")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set. Add it in Railway → Variables.")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ---------------- storage ----------------
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
    target = now.replace(hour=DAILY_HOUR_UTC, minute=DAILY_MINUTE_UTC, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def play_kb(text: str = "▶ Play ECHO") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=GAME_URL))]]
    )


WELCOME = (
    "👁️ <b>ECHO</b> — teach it to be human.\n\n"
    "Every day ECHO has <b>a word for you</b>, and a journey of lessons you <b>live</b>, not just read.\n"
    "Tap to play, and come back each day for ECHO's new word to keep your streak alive. 🔥"
)

# ---------------- handlers ----------------
@dp.message(CommandStart())
async def cmd_start(m: Message):
    subs = load_subs()
    subs.add(m.chat.id)
    save_subs(subs)
    await m.answer(WELCOME, reply_markup=play_kb())


@dp.message(Command("play"))
async def cmd_play(m: Message):
    await m.answer("Play ECHO now 👇", reply_markup=play_kb())


@dp.message(Command("stop"))
async def cmd_stop(m: Message):
    subs = load_subs()
    if m.chat.id in subs:
        subs.discard(m.chat.id)
        save_subs(subs)
    await m.answer("ECHO's daily reminder is off. Send /start to turn it back on. 👁️")


def _is_admin(m: Message) -> bool:
    return bool(ADMIN_ID) and str(m.chat.id) == str(ADMIN_ID)


@dp.message(Command("id"))
async def cmd_id(m: Message):
    if not _is_admin(m):
        return
    await m.answer(f"Your chat id: <code>{m.chat.id}</code>")


@dp.message(Command("status"))
async def cmd_status(m: Message):
    # admin-only health check — the fastest way to see whether subscribers persisted
    if not _is_admin(m):
        return
    subs = load_subs()
    st = load_state()
    exists = os.path.exists(SUBS_FILE)
    await m.answer(
        "<b>ECHO bot status</b>\n"
        f"subscribers: <b>{len(subs)}</b>\n"
        f"your id in list: <b>{'yes' if m.chat.id in subs else 'no'}</b>\n"
        f"last daily sent: <b>{st.get('last_sent', 'never')}</b>\n"
        f"today (UTC): <b>{today_str()}</b>\n"
        f"next fire: <b>{next_fire():%Y-%m-%d %H:%M} UTC</b>\n"
        f"data dir: <code>{DATA_DIR}</code>  file exists: <b>{'yes' if exists else 'no'}</b>\n"
        f"<i>If data dir is not a mounted Volume, this list is wiped on every redeploy.</i>"
    )


@dp.message(Command("testdaily"))
async def cmd_testdaily(m: Message):
    if not _is_admin(m):
        return
    await m.answer("Broadcasting today's word to all subscribers now… 🔥")
    sent = await daily_broadcast(force=True)
    await m.answer(f"Done ✅ sent={sent} (0 means the subscriber list is empty — check the Volume)")


# ---------------- daily reminder ----------------
DAILY_MSGS = [
    "👁️ ECHO has a new word for you today — don't break your streak. 🔥",
    "ECHO is waiting. Today's word is ready. 👁️",
    "A small daily lesson, and growth that adds up. Open ECHO now. 🔥",
    "One step today with ECHO keeps your journey alive. 👁️",
]


async def daily_broadcast(force: bool = False) -> int:
    """Send today's word. Records the date so it won't repeat. Returns sent count."""
    st = load_state()
    if not force and st.get("last_sent") == today_str():
        log.info("daily: already sent today (%s), skipping", today_str())
        return 0
    subs = load_subs()
    if not subs:
        log.info("daily: no subscribers yet (is DATA_DIR a mounted Volume?)")
        # still stamp the date on a forced run so a manual test doesn't loop
        if force:
            st["last_sent"] = today_str(); save_state(st)
        return 0
    idx = datetime.now(timezone.utc).toordinal() % len(DAILY_MSGS)
    text = DAILY_MSGS[idx]
    sent, dead = 0, []
    for cid in list(subs):
        try:
            await bot.send_message(cid, text, reply_markup=play_kb("🔥 Open today's word"))
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            s = str(e).lower()
            if any(k in s for k in ("blocked", "deactivated", "chat not found", "user is deactivated")):
                dead.append(cid)
            else:
                log.warning("send to %s failed: %s", cid, e)
    if dead:
        subs = load_subs()
        for c in dead:
            subs.discard(c)
        save_subs(subs)
    st["last_sent"] = today_str()
    save_state(st)
    log.info("daily broadcast: sent=%d removed=%d", sent, len(dead))
    return sent


async def scheduler():
    """Startup catch-up, then fire once per day at the configured UTC time."""
    # catch-up: if today's slot already passed and we haven't sent today, send now
    now = datetime.now(timezone.utc)
    slot = now.replace(hour=DAILY_HOUR_UTC, minute=DAILY_MINUTE_UTC, second=0, microsecond=0)
    if now >= slot and load_state().get("last_sent") != today_str():
        log.info("startup catch-up: today's slot passed and not sent yet — sending now")
        try:
            await daily_broadcast()
        except Exception as e:
            log.warning("catch-up broadcast error: %s", e)
    while True:
        target = next_fire()
        wait = (target - datetime.now(timezone.utc)).total_seconds()
        log.info("next daily reminder at %s UTC (in %.1f h)", target.strftime("%Y-%m-%d %H:%M"), wait / 3600)
        await asyncio.sleep(max(1, wait))
        try:
            await daily_broadcast()
        except Exception as e:
            log.warning("daily broadcast error: %s", e)
        await asyncio.sleep(2)


# ---------------- HTTP /register endpoint (in-game opt-in from the Mini App) ----------------
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def verify_init_data(init_data: str):
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None
        check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, received_hash):
            return None
        user = json.loads(pairs.get("user", "{}"))
        return user.get("id")
    except Exception:
        return None


async def handle_register(request: web.Request):
    init_data = await request.text()
    uid = verify_init_data(init_data)
    if not uid:
        return web.Response(status=403, text="invalid", headers=CORS)
    subs = load_subs()
    if uid not in subs:
        subs.add(uid)
        save_subs(subs)
        log.info("registered via Mini App: %s (total=%d)", uid, len(subs))
    return web.Response(text="ok", headers=CORS)


async def handle_options(request: web.Request):
    return web.Response(status=204, headers=CORS)


async def handle_health(request: web.Request):
    return web.Response(text="ECHO bot is running")


async def start_web():
    app = web.Application()
    app.router.add_post("/register", handle_register)
    app.router.add_options("/register", handle_options)
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "8080"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("HTTP server listening on :%d  (POST /register)", port)


async def on_startup():
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Play ECHO", web_app=WebAppInfo(url=GAME_URL))
        )
        log.info("menu button set to Mini App")
    except Exception as e:
        log.warning("could not set menu button: %s", e)


async def main():
    await on_startup()
    await start_web()
    asyncio.create_task(scheduler())
    log.info("ECHO bot started. game=%s daily=%02d:%02d UTC data_dir=%s",
             GAME_URL, DAILY_HOUR_UTC, DAILY_MINUTE_UTC, DATA_DIR)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
