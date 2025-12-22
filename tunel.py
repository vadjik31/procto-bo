import os
import json
from datetime import datetime

from fastapi import HTTPException, Request

# ===================== TELEGRAM =====================
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.filters import Command

# ===================== GOOGLE SHEETS =====================
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
    raise RuntimeError("❌ Missing required ENV variables")

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
# CRM HELPERS
# =====================================================

def find_row_by_telegram_id(telegram_id: str):
    records = sheet.get_all_records()
    for idx, row in enumerate(records, start=2):  # строки с 2-й
        if str(row.get("telegram_id")) == str(telegram_id):
            return idx
    return None


def upsert_lead(data: dict):
    """
    Обновляет или создаёт лида.
    1 человек = 1 строка
    """
    row_index = find_row_by_telegram_id(data["telegram_id"])

    row_values = [
        data.get("telegram_id"),
        data.get("username"),
        data.get("email"),
        data.get("stage"),
        data.get("test_score"),
        data.get("decision"),
        data.get("status"),
        data.get("last_event"),
        datetime.utcnow().isoformat()
    ]

    if row_index:
        sheet.update(f"A{row_index}:I{row_index}", [row_values])
    else:
        sheet.append_row(row_values)

# =====================================================
# TELEGRAM HANDLERS
# =====================================================

@dp.message(Command("start"))
async def start_handler(message: Message):
    telegram_id = message.from_user.id
    username = message.from_user.username or ""

    # Ссылка регистрации ТОЛЬКО через бота
    register_url = (
        "https://855f92.skillspace.ru/school?"
        f"telegram_id={telegram_id}"
    )

    # фиксируем старт в CRM
    upsert_lead({
        "telegram_id": telegram_id,
        "username": username,
        "stage": "START",
        "status": "waiting",
        "last_event": "telegram_start"
    })

    await message.answer(
        "👋 Привет!\n\n"
        "Ты проходишь автоматический отбор.\n\n"
        "❗ Регистрируйся в Skillspace ТОЛЬКО по ссылке ниже,\n"
        "иначе система тебя не увидит.\n\n"
        f"👉 {register_url}"
    )


@dp.message()
async def fallback_handler(message: Message):
    await message.answer(
        "⏳ Сейчас тебе нужно пройти обучение в Skillspace.\n"
        "Система сама решит, что будет дальше."
    )

# =====================================================
# SKILLSPACE WEBHOOK HANDLER
# =====================================================

async def handle_skillspace_event(request: Request, payload: dict):
    token = request.query_params.get("token")
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    print("===== SKILLSPACE EVENT =====")
    print(payload)
    print("============================")

    event_name = payload.get("name")
    student = payload.get("student", {})
    lesson = payload.get("lesson", {})

    email = student.get("email", "")
    custom_fields = student.get("customFields", [])

    telegram_id = None
    username = ""

    for field in custom_fields:
        if field.get("title") == "telegram_id":
            telegram_id = field.get("value")
        if field.get("title") == "username":
            username = field.get("value")

    if not telegram_id:
        return {"ok": True}

    # =================================================
    # ЭТАПЫ (course / hw / test start)
    # =================================================

    event_stage_map = {
        "course-begin": "COURSE_STARTED",
        "homework-begin": "HW_STARTED",
        "test-begin": "TEST_STARTED",
    }

    if event_name in event_stage_map:
        upsert_lead({
            "telegram_id": telegram_id,
            "username": username,
            "email": email,
            "stage": event_stage_map[event_name],
            "status": "waiting",
            "last_event": event_name
        })
        return {"ok": True}

    # =================================================
    # ФИНАЛ ТЕСТА
    # =================================================

    if event_name != "test-end":
        return {"ok": True}

    score = lesson.get("score")
    if score is None:
        return {"ok": True}

    # --------- РЕШЕНИЕ ---------
    if score < PASS_THRESHOLD:
        decision = "FAILED"
        stage = "REJECTED"
        status = "rejected"

    elif score < GREAT_THRESHOLD:
        decision = "PASSED"
        stage = "TEST_PASSED"
        status = "waiting"

    else:
        decision = "GREAT"
        stage = "TEST_GREAT"
        status = "invited"

    # --------- CRM ---------
    upsert_lead({
        "telegram_id": telegram_id,
        "username": username,
        "email": email,
        "stage": stage,
        "test_score": score,
        "decision": decision,
        "status": status,
        "last_event": "test-end"
    })

    # --------- АВТОДОПУСК ---------
    if decision == "GREAT":
        await bot.send_message(
            chat_id=int(telegram_id),
            text=(
                "🔥 Ты прошёл отбор!\n\n"
                "Тест выполнен на высоком уровне.\n\n"
                "👉 Напиши лично: @vadjik"
            )
        )

    return {"ok": True}
