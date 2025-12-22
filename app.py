import os
import json
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException

from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils import executor

import gspread
from google.oauth2.service_account import Credentials

# ======================
# ENV
# ======================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

SKILLSPACE_COURSE_URL = os.getenv("SKILLSPACE_COURSE_URL")
CONTACT_USERNAME = os.getenv("CONTACT_USERNAME", "@vadjik")

PASS_THRESHOLD = int(os.getenv("PASS_THRESHOLD", 50))
GREAT_THRESHOLD = int(os.getenv("GREAT_THRESHOLD", 80))

if not all([BOT_TOKEN, SHEET_ID, SERVICE_ACCOUNT_JSON]):
    raise RuntimeError("Missing env vars")

# ======================
# GOOGLE SHEETS
# ======================

creds = Credentials.from_service_account_info(
    json.loads(SERVICE_ACCOUNT_JSON),
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(SHEET_ID).sheet1

# ======================
# BOT
# ======================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

user_state = {}

# ======================
# FASTAPI
# ======================

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok"}

# ======================
# TELEGRAM FLOW
# ======================

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    user_state[message.from_user.id] = {"stage": "name"}
    await message.answer("Привет! Как тебя зовут?")

@dp.message_handler()
async def register_flow(message: types.Message):
    uid = message.from_user.id
    state = user_state.get(uid, {})

    if state.get("stage") == "name":
        state["name"] = message.text
        state["stage"] = "age"
        await message.answer("Сколько тебе лет?")
        return

    if state.get("stage") == "age":
        state["age"] = message.text
        state["stage"] = "country"
        await message.answer("В какой ты стране?")
        return

    if state.get("stage") == "country":
        state["country"] = message.text
        state["stage"] = "english"
        await message.answer("Уровень английского? (A1–C2)")
        return

    if state.get("stage") == "english":
        state["english"] = message.text
        state["stage"] = "experience"
        await message.answer("Работал раньше с Amazon?")
        return

    if state.get("stage") == "experience":
        state["experience"] = message.text
        state["stage"] = "done"

        row = [
            uid,
            state["name"],
            state["age"],
            state["country"],
            state["english"],
            state["experience"],
            "REGISTERED",
            datetime.utcnow().isoformat()
        ]
        sheet.append_row(row)

        await message.answer(
            f"Отлично! Вот доступ к курсу:\n{SKILLSPACE_COURSE_URL}\n\n"
            "После выполнения всех заданий ты получишь контакт для связи."
        )
        return

# ======================
# SKILLSPACE WEBHOOK
# ======================

@app.post("/skillspace-webhook")
async def skillspace_webhook(request: Request):
    token = request.query_params.get("token")
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)

    payload = await request.json()
    print(payload)

    event = payload.get("name")
    student = payload.get("student", {})
    lesson = payload.get("lesson", {})

    score = lesson.get("score")
    email = student.get("email", "")

    if event != "test-end" or score is None:
        return {"ok": True}

    if score < PASS_THRESHOLD:
        status = "FAILED"
    elif score < GREAT_THRESHOLD:
        status = "PASSED"
    else:
        status = "GREAT"

    sheet.append_row([
        email,
        score,
        status,
        "SKILLSPACE",
        datetime.utcnow().isoformat()
    ])

    return {"ok": True}

# ======================
# RUN
# ======================

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
