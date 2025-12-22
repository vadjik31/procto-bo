import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from sheets import LeadData, SheetsClient
from tunel import BotService, LeadProfile

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("procto")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def must_env(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def extract_sheet_id(sheet_id_or_url: str) -> str:
    s = sheet_id_or_url.strip()
    if "docs.google.com" in s and "/d/" in s:
        # .../spreadsheets/d/<ID>/edit
        return s.split("/d/")[1].split("/")[0]
    return s


def deep_get(d: Any, path: Tuple[str, ...]) -> Optional[Any]:
    cur = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def extract_skillspace_event(payload: Dict[str, Any]) -> str:
    # Try common keys
    for k in ("event", "type", "event_name", "name"):
        v = payload.get(k)
        if isinstance(v, str) and v:
            return v
    # Sometimes nested
    v = deep_get(payload, ("data", "event"))
    return v if isinstance(v, str) else ""


def extract_lesson_score(payload: Dict[str, Any]) -> Optional[float]:
    # Most important key per your spec: lesson.score
    score = deep_get(payload, ("lesson", "score"))
    if score is None:
        score = deep_get(payload, ("data", "lesson", "score"))
    if score is None:
        return None

    try:
        sc = float(score)
    except Exception:
        return None

    # Normalize 0..1 to percent if needed
    if 0.0 <= sc <= 1.0:
        # Only convert if looks like fraction (e.g. 0.8)
        if sc != 1.0:
            sc = sc * 100.0
    return sc


def extract_lesson_id(payload: Dict[str, Any]) -> Optional[str]:
    for path in (("lesson", "id"), ("data", "lesson", "id"), ("lesson", "lesson_id")):
        v = deep_get(payload, path)
        if v is not None:
            return str(v)
    return None


def extract_course_id(payload: Dict[str, Any]) -> Optional[str]:
    for path in (("course", "id"), ("data", "course", "id"), ("course_id",), ("data", "course_id")):
        v = deep_get(payload, path)
        if v is not None:
            return str(v)
    return None


def extract_email(payload: Dict[str, Any]) -> Optional[str]:
    # We rely on email to match Telegram lead with Skillspace event
    for path in (
        ("user", "email"),
        ("student", "email"),
        ("data", "user", "email"),
        ("data", "student", "email"),
        ("email",),
        ("data", "email"),
    ):
        v = deep_get(payload, path)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Init Sheets
    sheet_id = extract_sheet_id(must_env("GOOGLE_SHEET_ID"))
    ws_name = os.getenv("GOOGLE_SHEET_WORKSHEET", "").strip() or None
    sa_json = must_env("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheets = SheetsClient(sheet_id=sheet_id, worksheet_name=ws_name, service_account_json=sa_json)

    pass_thr = float(os.getenv("PASS_THRESHOLD", "50"))
    great_thr = float(os.getenv("GREAT_THRESHOLD", "80"))
    course_url = must_env("SKILLSPACE_COURSE_URL")

    # Callback from bot when lead is fully collected
    async def on_lead_completed(profile: LeadProfile) -> str:
        now = utc_iso()
        lead = LeadData(
            telegram_id=profile.telegram_id,
            email=profile.email,
            age=profile.age,
            gender=profile.gender,
            country=profile.country,
            language=profile.language,
            english_level=profile.english_level,
            amazon_experience=profile.amazon_experience,
            stage="PROFILE_COLLECTED",
        )

        def _sync_upsert():
            return sheets.upsert_lead(lead, now)

        row, action = await anyio.to_thread.run_sync(_sync_upsert)

        return (
            f"Готово ✅ (строка {row}, {action}).\n\n"
            f"Вот доступ к курсу Skillspace:\n{course_url}\n\n"
            "После прохождения теста/ДЗ я получу webhook и напишу тебе результат."
        )

    # Init Bot + background polling
    bot_token = must_env("BOT_TOKEN")
    bot_service = BotService(token=bot_token, on_lead_completed=on_lead_completed)

    app.state.sheets = sheets
    app.state.bot = bot_service
    app.state.pass_thr = pass_thr
    app.state.great_thr = great_thr
    app.state.course_id = os.getenv("SKILLSPACE_COURSE_ID", "").strip()
    app.state.skillspace_token = must_env("SKILLSPACE_WEBHOOK_TOKEN")

    polling_task = asyncio.create_task(bot_service.start_polling())

    logger.info("App started. Bot polling running. Skillspace webhook ready.")

    try:
        yield
    finally:
        logger.info("Shutting down...")
        polling_task.cancel()
        try:
            await bot_service.stop()
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health():
    return {"ok": True, "service": "procto-bo", "time": utc_iso()}


@app.post("/telegram-webhook")
async def telegram_webhook_stub():
    # Polling mode: webhook не используется.
    return JSONResponse({"ok": True, "mode": "polling", "note": "telegram webhook endpoint is not used"}, status_code=200)


@app.post("/skillspace-webhook")
async def skillspace_webhook(request: Request, token: str):
    if token != app.state.skillspace_token:
        raise HTTPException(status_code=401, detail="Bad token")

    payload = await request.json()
    event_name = extract_skillspace_event(payload)

    # We only care about test-end per your spec
    if event_name != "test-end":
        return {"ok": True, "ignored": True, "event": event_name}

    course_id = extract_course_id(payload)
    expected_course_id = app.state.course_id
    if expected_course_id and course_id and str(course_id) != str(expected_course_id):
        return {"ok": True, "ignored": True, "reason": "course_id_mismatch", "course_id": course_id}

    score = extract_lesson_score(payload)
    lesson_id = extract_lesson_id(payload)
    email = extract_email(payload)

    if not email:
        # Without email we can't match lead reliably
        logger.warning("Skillspace test-end received but email not found in payload")
        return {"ok": True, "error": "email_not_found_in_payload"}

    pass_thr = float(app.state.pass_thr)
    great_thr = float(app.state.great_thr)

    stage = "TEST_FAILED"
    if score is not None and score >= great_thr:
        stage = "TEST_GREAT"
    elif score is not None and score >= pass_thr:
        stage = "TEST_PASSED"

    now = utc_iso()

    # Update sheet (sync in thread)
    def _sync_update():
        return app.state.sheets.update_from_skillspace(
            email=email,
            telegram_id=None,
            stage=stage,
            now_iso=now,
            event_name=event_name,
            lesson_score=score,
            lesson_id=lesson_id,
            course_id=course_id,
        )

    row = await anyio.to_thread.run_sync(_sync_update)

    # Notify user in Telegram if we can find telegram_id in sheet:
    # Simplest approach: after update, we try to locate row and fetch telegram_id.
    # For now we just attempt to find telegram_id by email using gspread find+row_values.
    telegram_id: Optional[int] = None
    try:
        def _sync_get_telegram_id():
            ws = app.state.sheets._get_ws()
            cell = ws.find(email)
            if not cell:
                return None
            row_vals = ws.row_values(cell.row)
            # Map headers to index
            headers = ws.row_values(1)
            idx = {h: i for i, h in enumerate(headers)}
            tid = row_vals[idx["telegram_id"]] if "telegram_id" in idx and idx["telegram_id"] < len(row_vals) else ""
            return int(tid) if tid and tid.isdigit() else None

        telegram_id = await anyio.to_thread.run_sync(_sync_get_telegram_id)
    except Exception:
        telegram_id = None

    if telegram_id:
        if stage == "TEST_GREAT":
            text = f"🔥 Отлично! Тест пройден на {score:.0f}% (≥ {great_thr:.0f}%). Скоро с тобой свяжутся."
        elif stage == "TEST_PASSED":
            text = f"✅ Тест пройден на {score:.0f}% (≥ {pass_thr:.0f}%). Скоро с тобой свяжутся."
        else:
            text = f"Пока не прошёл порог: результат {score:.0f}% (нужно ≥ {pass_thr:.0f}%). Попробуй ещё раз."

        try:
            await app.state.bot.send_message(telegram_id, text)
        except Exception as e:
            logger.warning(f"Failed to send telegram message: {e}")

    return {"ok": True, "event": event_name, "email": email, "score": score, "stage": stage, "sheet_row": row}
