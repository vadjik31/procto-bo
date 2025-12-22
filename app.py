from fastapi import FastAPI, Request
from aiogram.types import Update

from tunel import bot, dp, handle_skillspace_event

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok"}

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.post("/skillspace-webhook")
async def skillspace_webhook(request: Request):
    payload = await request.json()
    return await handle_skillspace_event(request, payload)
