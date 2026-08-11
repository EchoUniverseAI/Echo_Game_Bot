# ECHO Universe — بوت اللعبة (Python / aiogram v3)

بوت تيليجرام مخصّص للعبة ECHO: زر **Play ECHO** يفتح الـMini App، و**إشعار يومي** يرجّع اللاعبين لكلمة ECHO اليومية. يعمل على Railway كخدمة طويلة التشغيل (long-polling).

---

## 1) أنشئ البوت في BotFather
1. افتح **@BotFather** → `/newbot` → اختر اسماً و username (مثلاً `EchoUniverseGameBot`).
2. انسخ **التوكن** (BOT_TOKEN) — ستضعه في Railway.
3. (اختياري) `/setdescription` و`/setuserpic` لهوية البوت. زر «Play ECHO» يضبطه الكود تلقائياً عند التشغيل.

## 2) انشر على Railway
1. Railway → **New Service** → من مستودع Git يحتوي هذه الملفات (أو ارفع المجلد).
2. Railway يكتشف Python تلقائياً عبر `requirements.txt`، ويشغّل `Procfile` (`worker: python bot.py`).
3. **Variables** — أضِف:

| المتغيّر | القيمة |
|---|---|
| `BOT_TOKEN` | توكن BotFather (إلزامي) |
| `GAME_URL` | `https://echo-games.netlify.app` |
| `DAILY_HOUR_UTC` | `6`  ← 6 UTC = 9:00 صباحاً بتوقيت الرياض |
| `DAILY_MINUTE_UTC` | `0` |
| `DATA_DIR` | `/data` |

4. **مهم — أضِف Volume للاستمرارية:** Railway → Service → **Volumes** → أنشئ Volume ووصّله على المسار `/data`. بدونه تُفقد قائمة المشتركين عند كل إعادة نشر (لأن نظام ملفات Railway مؤقّت).

## 3) وقت الإشعار اليومي
الوقت بتوقيت **UTC**. الرياض = UTC+3، فاطرح 3 ساعات:
- 9:00 صباحاً الرياض → `DAILY_HOUR_UTC=6`
- 8:00 مساءً الرياض → `DAILY_HOUR_UTC=17`

## 4) ادعُ الجروب للاختبار
- شارك رابط البوت: `https://t.me/EchoUniverseGameBot` (استبدله باسم بوتك).
- كل من يضغط **Start** يشترك تلقائياً في الكلمة اليومية، ويظهر له زر **Play ECHO** أسفل المحادثة.
- ملاحظة: أزرار Mini App تعمل في المحادثات الخاصة؛ في الجروب شارك رابط البوت لا زر WebApp.

## الأوامر
- `/start` — ترحيب + زر اللعب + اشتراك في الكلمة اليومية
- `/play` — إعادة إرسال زر اللعب
- `/stop` — إيقاف التذكير اليومي

## ملفات المشروع
- `bot.py` — البوت (aiogram v3)
- `requirements.txt` — الاعتماديات
- `Procfile` — أمر التشغيل على Railway
- `.env.example` — نموذج المتغيّرات (للتشغيل محلياً: انسخه إلى `.env`)
- `runtime.txt` — إصدار Python

## تشغيل محلي (اختياري)
```bash
pip install -r requirements.txt
export BOT_TOKEN=xxxx GAME_URL=https://echo-games.netlify.app DATA_DIR=.
python bot.py
```
