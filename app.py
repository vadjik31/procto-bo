import os
import json
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException

from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher.filters.state import State, StatesGroup

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
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# ======================
# STATES
# ======================

class Reg(StatesGroup):
    name = State()
    age = State()
    country = State()
    english = State()
    experience = State()

# ======================
# HANDLERS
# ======================

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    await Reg.name.set()
    await message.answer("Привет! Как тебя зовут?")

@dp.message_handler(state=Reg.name)
async def step_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await Reg.age.set()
    await message.answer("Сколько тебе лет?")

@dp.message_handler(state=Reg.age)
async def step_age(message: types.Message, state: FSMContext):
    await state.update_data(age=message.text)
    await Reg.country.set()
    await message.answer("В какой ты стране?")

@dp.message_handler(state=Reg.country)
async def step_country(message: types.Message, state: FSMContext):
    await state.update_data(country=message.text)
    await Reg.english.set()
    await message.answer("Уровень английского? (A1–C2)")

@dp.message_handler(state=Reg.english)
async def step_english(message: types.Message, state: FSMContext):
    await state.update_data(english=message.text)
    await Reg.experience.set()
    await message.answer("Работал раньше с Amazon?")

@dp.message_handler(state=Reg.experience)
async def step_exp(message: types.Message, state: FSMContext):
    data = await state.get_data()
    data["experience"] = message.text

    sheet.append_row([
        message.from_user.id,
        data["name"],
        data["age"],
        data["country"],
        data["english"],
        data["experience"],
        "REGISTERED",
        datetime.utcnow().isoformat()
    ])

    await message.answer(
        f"Отлично! Вот доступ к курсу:\n{SKILLSPACE_COURSE_URL}\n\n"
        "После выполнения всех заданий ты получишь контакт для связи."
    )
    await state.finish()

# ======================
# FASTAPI
# ======================

app = FastAPI()

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    update = types.Update(**await request.json())
    await dp.process_update(update)
    return {"ok": True}

@app.get("/")
def root():
    return {"status": "ok"}

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
    lesson = payload.get("lesson", {})
    student = payload.get("student", {})

    score = lesson.get("score")
    email = student.get("email")

    if event != "test-end" or score is None:
        return {"ok": True}

    status = "FAILED" if score < PASS_THRESHOLD else "PASSED"
    if score >= GREAT_THRESHOLD:
        status = "GREAT"

    sheet.append_row([
        email,
        score,
        status,
        "SKILLSPACE",
        datetime.utcnow().isoformat()
    ])

    return {"ok": True}
