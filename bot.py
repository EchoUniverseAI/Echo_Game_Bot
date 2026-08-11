"""
ECHO Universe — dedicated game bot (aiogram v3)
Runs on Railway as a long-polling worker.

Features
  • /start  → welcome + "Play ECHO" Mini App button, and subscribes the user to the daily word
  • /play   → resend the play button
  • /stop   → unsubscribe from the daily reminder
  • Daily reminder → once per day at DAILY_HOUR_UTC:DAILY_MINUTE_UTC to every subscriber
  • Persistent "Play ECHO" menu button (bottom-left of the chat) via the Mini App

Environment variables (set these in Railway → Variables)
  BOT_TOKEN            (required)  token from @BotFather for the new game bot
  GAME_URL             default https://echo-games.netlify.app   the Mini App URL
  DAILY_HOUR_UTC       default 6   hour (UTC) to send the daily word  (6 UTC = 9:00 AM Riyadh)
  DAILY_MINUTE_UTC     default 0
  DATA_DIR             default .   where subscribers.json is stored — point to a Railway Volume (e.g. /data) so it survives redeploys
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

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


def play_kb(text: str = "▶ Play ECHO") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=GAME_URL))]]
    )


WELCOME = (
    "👁️ <b>ECHO</b> — علّمه أن يكون إنساناً.\n\n"
    "كل يوم عند ECHO <b>كلمة لك</b>، ورحلة دروس <b>تعيشها</b> لا تقرؤها فقط.\n"
    "اضغط للّعب، وارجع كل يوم لكلمة ECHO الجديدة وحافظ على سلسلتك. 🔥"
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
    await m.answer("العب ECHO الآن 👇", reply_markup=play_kb())


@dp.message(Command("stop"))
async def cmd_stop(m: Message):
    subs = load_subs()
    if m.chat.id in subs:
        subs.discard(m.chat.id)
        save_subs(subs)
    await m.answer("تم إيقاف تذكير ECHO اليومي. أرسل /start لتعود إليه. 👁️")


# ---------------- daily reminder ----------------
DAILY_MSGS = [
    "👁️ ECHO عنده كلمة جديدة لك اليوم — لا تكسر سلسلتك. 🔥",
    "ECHO ينتظرك. كلمة اليوم جاهزة. 👁️",
    "درسٌ يومي صغير، وتقدّمٌ يكبر. افتح ECHO الآن. 🔥",
    "خطوة واحدة اليوم مع ECHO تُبقي رحلتك حيّة. 👁️",
]


async def daily_broadcast():
    subs = load_subs()
    if not subs:
        log.info("daily: no subscribers yet")
        return
    idx = datetime.now(timezone.utc).toordinal() % len(DAILY_MSGS)
    text = DAILY_MSGS[idx]
    sent, dead = 0, []
    for cid in list(subs):
        try:
            await bot.send_message(cid, text, reply_markup=play_kb("🔥 افتح كلمة اليوم"))
            sent += 1
            await asyncio.sleep(0.05)  # stay under Telegram rate limits
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
    log.info("daily broadcast: sent=%d removed=%d", sent, len(dead))


async def scheduler():
    """Fire daily_broadcast once per day at the configured UTC time."""
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


async def on_startup():
    # persistent "Play ECHO" button at the bottom-left of every private chat
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Play ECHO", web_app=WebAppInfo(url=GAME_URL))
        )
        log.info("menu button set to Mini App")
    except Exception as e:
        log.warning("could not set menu button: %s", e)


async def main():
    await on_startup()
    asyncio.create_task(scheduler())
    log.info("ECHO bot started. game=%s daily=%02d:%02d UTC", GAME_URL, DAILY_HOUR_UTC, DAILY_MINUTE_UTC)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
