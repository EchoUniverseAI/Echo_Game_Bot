# ECHO Universe — Game Bot + In‑Game Daily Opt‑In (Python / aiogram v3)

A Telegram bot for the ECHO game: a **Play ECHO** Mini App button, a **daily reminder**,
and — new — an **in‑game button** that lets any player who opens the game *inside Telegram*
subscribe to ECHO's daily word **without typing `/start`**.

---

## What changed in this version
1. **In‑game opt‑in.** The game now shows a button “🔔 فعّل التحديثات والمهام اليومية”.
   When a player taps it, Telegram asks permission (`requestWriteAccess`), and the game
   sends the player's signed identity to the bot, which auto‑subscribes them.
2. **Bot now serves a web endpoint** (`POST /register`) to receive that. So the bot is now a
   **web** service (not a worker) — `Procfile` = `web: python bot.py`.
3. **No more skipped days.** The scheduler now records the last day it sent and runs a
   **startup catch‑up**, so a redeploy after the daily time still sends today's reminder.
4. **Group‑safe daily send.** If a subscriber is a group (where WebApp buttons aren't allowed),
   the daily message is resent with a plain link button instead of failing silently.

> ⚠️ Telegram rule: a bot can only message people who started it **or** who granted write
> access via the Mini App. Players who open the game in a **normal browser** (not inside
> Telegram) can't be reached — that's a Telegram limitation, not a bug. The in‑game button is
> hidden for them automatically.

---

## 1) Create the bot in BotFather
1. `@BotFather` → `/newbot` → name + username.
2. Copy the **token** (BOT_TOKEN).

## 2) Deploy on Railway (as a WEB service)
1. Railway → New Service → from a repo with these files.
2. Railway auto‑detects Python and runs the `Procfile` (`web: python bot.py`).
3. **Generate a public domain:** Railway → Service → **Settings → Networking → Generate Domain**.
   You'll get something like `https://echo-bot-production.up.railway.app`. **Copy it.**
4. **Variables** — add:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | BotFather token (required) |
| `GAME_URL` | `https://echo-games.netlify.app` |
| `DAILY_HOUR_UTC` | `6`  ← 6 UTC = 9:00 AM Riyadh |
| `DAILY_MINUTE_UTC` | `0` |
| `DATA_DIR` | `/data` |
| `ADMIN_ID` | your numeric id (for `/id`, `/testdaily`) |

5. **Add a Volume mounted at `/data`** (Railway → Volumes). Without it, the subscriber list
   is wiped on every redeploy and nobody receives the daily word. **This is required.**

## 3) Point the game at the bot
In `index.html`, find this line (near the top of the main script) and replace the URL with the
Railway domain from step 2 **plus `/register`**:

```js
const ECHO_REGISTER_URL = 'https://YOUR-BOT.up.railway.app/register';  // 👈 REPLACE THIS
```

Then redeploy the game to Netlify. Done.

## 4) Daily reminder time (UTC)
Riyadh = UTC+3, so subtract 3 hours: 9:00 AM Riyadh → `DAILY_HOUR_UTC=6`; 8:00 PM → `17`.

## Commands
- `/start` — welcome + play button + subscribe (everyone)
- `/play` — resend the play button (everyone)
- `/stop` — turn off the daily reminder (everyone)
- `/id` — shows your chat id (**admin only**)
- `/testdaily` — broadcast today's reminder to all subscribers now (**admin only**)

## How to confirm it actually sent
- Railway logs show `daily broadcast: sent=N removed=M`. `sent=N` (N>0) = it reached N players.
- `daily: no subscribers yet` or `sent=0` = the subscriber list is empty (check the Volume).
- Fastest live test: `/start` first so you're subscribed, then `/testdaily` — you should
  receive the “🔥 Open today's word” message yourself.

## Project files
- `bot.py` — the bot (aiogram v3 + aiohttp web endpoint)
- `index.html` — the game (with the in‑game opt‑in button)
- `requirements.txt`, `runtime.txt`, `Procfile`, `.env.example`

## Run locally (optional)
```bash
pip install -r requirements.txt
export BOT_TOKEN=xxxx GAME_URL=https://echo-games.netlify.app DATA_DIR=. PORT=8080
python bot.py
```
