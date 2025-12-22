import os
import json
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException

# --- Telegram ---
from aiogram import Bot, Dispatcher
from aiogram.types import Message, Update
from aiogram.filters import Command

# --- Google Sheets ---
import gspread
from google.oauth2.service_account import Credentials

# =====================================================
# CONFIG (ENV)
# =====================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

PASS_THRESHOLD = int(os.getenv("PASS_THRESHOLD", 50))
GREAT_THRESHOLD = int(os.getenv("GREAT_THRESHOLD", 80))

if not all([
    TELEGRAM_BOT_TOKEN,
    WEBHOOK_SECRET,
    GOOGLE_SHEET_ID,
    GOOGLE_SERVICE_ACCOUNT_JSON
]):
    raise RuntimeError("❌ Missing ENV variables")

# =====================================================
# GOOGLE SHEETS INIT
# =====================================================

creds_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)

credentials = Credentials.from_service_account_info(
    creds_info,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)

gc = gspread.authorize(credentials)
sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1

# =====================================================
# TELEGRAM BOT INIT
# =====================================================

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# =====================================================
# TELEGRAM HANDLERS
# =====================================================

@dp.message(Command("start"))
async def start_handler(message: Message):
    await message.answer(
        "👋 Привет!\n\n"
        "Ты в системе PROCTO.\n\n"
        "📚 Обучение проходит в Skillspace.\n"
        "После выполнения заданий ты получишь доступ к следующему этапу."
    )

@dp.message()
async def fallback_handler(message: Message):
    await message.answer(
        "ℹ️ Пожалуйста, пройди обучение в Skillspace.\n"
        "Дальнейшие инструкции появятся автоматически."
    )

# =====================================================
# FASTAPI INIT
# =====================================================

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok"}

# =====================================================
# TELEGRAM WEBHOOK
# =====================================================

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}

# =====================================================
# SKILLSPACE WEBHOOK
# =====================================================

@app.post("/skillspace-webhook")
async def skillspace_webhook(request: Request):
    token = request.query_params.get("token")
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    payload = await request.json()

    print("===== SKILLSPACE PAYLOAD =====")
    print(payload)
    print("==============================")

    event_name = payload.get("name")

    # интересует только завершение теста
    if event_name != "test-end":
        return {"ok": True}

    student = payload.get("student", {})
    lesson = payload.get("lesson", {})

    email = student.get("email", "")
    name = student.get("name", "")
    score = lesson.get("score")

    if score is None:
        return {"ok": True}

    # --- логика оценки ---
    if score < PASS_THRESHOLD:
        result = "FAILED"
    elif score < GREAT_THRESHOLD:
        result = "PASSED"
    else:
        result = "GREAT"

    # --- запись в таблицу ---
    row = [
        email,
        name,
        score,
        result,
        event_name,
        datetime.utcnow().isoformat()
    ]

    sheet.append_row(row)

    print(f"✅ SAVED: {email} | {score} | {result}")

    # --- Telegram уведомление при GREAT ---
    if result == "GREAT":
        # здесь позже можно связать email → telegram_id
        pass

    return {"ok": True}

# =====================================================
# DEBUG ENDPOINT (РУЧНАЯ ПРОВЕРКА)
# =====================================================

@app.get("/debug-test")
def debug_test(token: str):
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    fake_payload = {
        "name": "test-end",
        "student": {
            "email": "debug@example.com",
            "name": "Debug User"
        },
        "lesson": {
            "score": 90
        }
    }

    score = fake_payload["lesson"]["score"]

    if score < PASS_THRESHOLD:
        result = "FAILED"
    elif score < GREAT_THRESHOLD:
        result = "PASSED"
    else:
        result = "GREAT"

    row = [
        fake_payload["student"]["email"],
        fake_payload["student"]["name"],
        score,
        result,
        "test-end",
        datetime.utcnow().isoformat()
    ]

    sheet.append_row(row)

    return {"ok": True, "saved": row}
