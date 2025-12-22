import os
import json
from datetime import datetime

from fastapi import HTTPException, Request

# ---------------- TELEGRAM ----------------
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.filters import Command

# ---------------- GOOGLE SHEETS ----------------
import gspread
from google.oauth2.service_account import Credentials

# =====================================================
# CONFIG
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
    raise RuntimeError("Missing ENV variables")

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
# TELEGRAM INIT
# =====================================================

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# =====================================================
# TELEGRAM FLOW (ВОРОНКА)
# =====================================================

@dp.message(Command("start"))
async def start_handler(message: Message):
    await message.answer(
        "👋 Привет!\n\n"
        "Это отбор в команду PROCTO.\n\n"
        "📚 Обучение проходит в Skillspace.\n"
        "Доступ к личному контакту открывается ТОЛЬКО после успешного прохождения."
    )

@dp.message()
async def fallback(message: Message):
    await message.answer(
        "⏳ Сейчас тебе нужно закончить обучение в Skillspace.\n"
        "Система сама решит, что будет дальше."
    )

# =====================================================
# SKILLSPACE LOGIC
# =====================================================

async def handle_skillspace_event(request: Request, payload: dict):
    token = request.query_params.get("token")
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    print("===== SKILLSPACE EVENT =====")
    print(payload)
    print("============================")

    event_name = payload.get("name")

    # Нас интересует ТОЛЬКО финал теста
    if event_name != "test-end":
        return {"ok": True}

    student = payload.get("student", {})
    lesson = payload.get("lesson", {})

    email = student.get("email", "")
    name = student.get("name", "")
    score = lesson.get("score")

    if score is None:
        return {"ok": True}

    # ---------------- РЕШЕНИЕ ----------------
    if score < PASS_THRESHOLD:
        decision = "FAILED"
    elif score < GREAT_THRESHOLD:
        decision = "PASSED"
    else:
        decision = "GREAT"

    # ---------------- SAVE TO SHEET ----------------
    row = [
        email,
        name,
        score,
        decision,
        event_name,
        datetime.utcnow().isoformat()
    ]
    sheet.append_row(row)

    print(f"✅ DECISION: {email} | {score} | {decision}")

    # ---------------- TELEGRAM ACTION ----------------
    if decision == "GREAT":
        # здесь дальше будет привязка email → telegram_id
        print("🔥 USER DESERVES PERSONAL CONTACT")

    return {"ok": True}
