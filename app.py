import os
import json
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException

import gspread
from google.oauth2.service_account import Credentials

# ======================
# CONFIG
# ======================

PASS_THRESHOLD = int(os.getenv("PASS_THRESHOLD", 50))
GREAT_THRESHOLD = int(os.getenv("GREAT_THRESHOLD", 80))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

if not all([WEBHOOK_SECRET, GOOGLE_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON]):
    raise RuntimeError("Missing required environment variables")

# ======================
# GOOGLE SHEETS INIT
# ======================

creds_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)

scopes = ["https://www.googleapis.com/auth/spreadsheets"]
credentials = Credentials.from_service_account_info(
    creds_info,
    scopes=scopes
)

gc = gspread.authorize(credentials)
sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1

# ======================
# FASTAPI
# ======================

app = FastAPI()


@app.get("/")
def root():
    return {"status": "ok"}


@app.post("/skillspace-webhook")
async def skillspace_webhook(request: Request):
    # --- security ---
    token = request.query_params.get("token")
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    # --- payload ---
    payload = await request.json()

    print("========== FULL PAYLOAD ==========")
    print(payload)
    print("==================================")

    event_name = payload.get("name")
    print("EVENT RECEIVED:", event_name)

    # интересует только завершение теста
    if event_name != "test-end":
        return {"ok": True}

    # --- data extraction ---
    student = payload.get("student", {})
    lesson = payload.get("lesson", {})

    email = student.get("email", "")
    name = student.get("name", "")
    score = lesson.get("score")

    if score is None:
        print("NO SCORE FOUND — SKIP")
        return {"ok": True}

    # --- result logic ---
    if score < PASS_THRESHOLD:
        result = "FAILED"
    elif score < GREAT_THRESHOLD:
        result = "PASSED"
    else:
        result = "GREAT"

    # --- save to sheet ---
    row = [
        email,
        name,
        score,
        result,
        event_name,
        datetime.utcnow().isoformat()
    ]

    sheet.append_row(row)

    print(f"LEAD SAVED → {email} | {score} | {result}")

    return {"ok": True}
