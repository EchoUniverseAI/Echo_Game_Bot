# ECHO Universe — Game Bot (Python / aiogram v3)

A dedicated Telegram bot for the ECHO game: a **Play ECHO** button that opens the Mini App, and a **daily reminder** that brings players back to ECHO's daily word. Runs on Railway as a long‑polling worker.

---

## 1) Create the bot in BotFather
1. Open **@BotFather** → `/newbot` → choose a name and username (e.g. `EchoUniverseGameBot`).
2. Copy the **token** (BOT_TOKEN) — you'll paste it into Railway.
3. (Optional) `/setdescription` and `/setuserpic` for the bot's identity. The **Play ECHO** button is set automatically by the code on startup.

## 2) Deploy on Railway
1. Railway → **New Service** → from a Git repo containing these files (or upload the folder).
2. Railway auto-detects Python via `requirements.txt` and runs the `Procfile` (`worker: python bot.py`).
3. **Variables** — add:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | BotFather token (required) |
| `GAME_URL` | `https://echo-games.netlify.app` |
| `DAILY_HOUR_UTC` | `6`  ← 6 UTC = 9:00 AM Riyadh |
| `DAILY_MINUTE_UTC` | `0` |
| `DATA_DIR` | `/data` |
| `ADMIN_ID` | (optional) your numeric id — enables `/testdaily` broadcast |

4. **Important — add a Volume for persistence:** Railway → Service → **Volumes** → create a Volume mounted at `/data`. Without it, the subscriber list is lost on every redeploy (Railway's filesystem is ephemeral).

## 3) Daily reminder time
Time is in **UTC**. Riyadh = UTC+3, so subtract 3 hours:
- 9:00 AM Riyadh → `DAILY_HOUR_UTC=6`
- 8:00 PM Riyadh → `DAILY_HOUR_UTC=17`

## 4) Invite the group to test
- Share the bot link: `https://t.me/EchoUniverseGameBot` (replace with your bot's username).
- Anyone who taps **Start** is auto-subscribed to the daily word and gets the **Play ECHO** button under the chat.
- Note: Mini App buttons work in private chats; in a group, share the bot link (not a WebApp button).

## Commands
- `/start` — welcome + play button + subscribe to the daily word (everyone)
- `/play` — resend the play button (everyone)
- `/stop` — turn off the daily reminder (everyone)
- `/id` — shows your chat id (**admin only**, ignored for others)
- `/testdaily` — broadcast today's reminder to all subscribers now (**admin only**, ignored for others)

## Admin
`ADMIN_ID` defaults to the owner's Telegram id in the code. Only that user can use `/id` and `/testdaily`; for everyone else these commands are silently ignored. To change the admin, set the `ADMIN_ID` variable in Railway.

## Test the daily reminder instantly (no waiting for the scheduled time)
1. Send `/start` to the bot (you become a subscriber).
2. As the admin, send `/testdaily` — the bot sends today's reminder to **all subscribers** immediately (a real test). You'll see a "sent=N" line in the Railway logs.

## Project files
- `bot.py` — the bot (aiogram v3)
- `requirements.txt` — dependencies
- `Procfile` — Railway start command
- `.env.example` — variable template (for local runs: copy it to `.env`)
- `runtime.txt` — Python version

## Run locally (optional)
```bash
pip install -r requirements.txt
export BOT_TOKEN=xxxx GAME_URL=https://echo-games.netlify.app DATA_DIR=.
python bot.py
```
