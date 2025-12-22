import os
import json
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException

import gspread
from google.oauth2.service_account import Credentials

app = FastAPI()

PASS_THRESHOLD = int(os.getenv("PASS_THRESHOLD", 50))
GREAT_THRESHOLD = int(os.getenv("GREAT_THRESHOLD", 80))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

# --- Google Sheets init ---
creds_info = json.loads(SERVICE_ACCOUNT_JSON)
scopes = ["https://www.googleapis.com/auth/spreadsheets"]
credentials = Credentials.from_service_account_info(creds_info, scopes=scopes)
gc = gspread.authorize(credentials)
sheet = gc.open_by_key(SHEET_ID).sheet1


@app.get("/")
def root():
    return {"status": "ok"}


@app.post("/skillspace-webhook")
async def skillspace_webhook(request: Request):
    token = request.query_params.get("token")
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    payload = await request.json()
    event_name = payload.get("name")

    print("EVENT RECEIVED:", event_name)

    if event_name != "test-end":
        return {"ok": True}

    student = payload.get("student", {})
    lesson = payload.get("lesson", {})

    email = student.get("email", "")
    name = student.get("name", "")
    score = lesson.get("score")

    if score is None:
        print("NO SCORE — SKIP")
        return {"ok": True}

    if score < PASS_THRESHOLD:
        result = "FAILED"
    elif score < GREAT_THRESHOLD:
        result = "PASSED"
    else:
        result = "GREAT"

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
