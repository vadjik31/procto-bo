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
from skillspace import invite_student, SkillspaceError
from tunel import BotService, LeadProfile

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("procto")


# ---------------- utils ----------------
def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def must_env(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def get_env_any(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return default


def extract_sheet_id(sheet_id_or_url: str) -> str:
    s = (sheet_id_or_url or "").strip()
    if "docs.google.com" in s and "/d/" in s:
        return s.split("/d/")[1].split("/")[0]
    return s


def deep_get(d: Any, path: Tuple[str, ...]) -> Optional[Any]:
    cur = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


# ---------------- Skillspace payload parsing ----------------
def extract_skillspace_event(payload: Dict[str, Any]) -> str:
    for k in ("event", "type", "event_name", "name"):
        v = payload.get(k)
        if isinstance(v, str) and v:
            return v
    v = deep_get(payload, ("data", "event"))
    return v if isinstance(v, str) else ""


def extract_email(payload: Dict[str, Any]) -> Optional[str]:
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


def extract_lesson_score(payload: Dict[str, Any]) -> Optional[float]:
    score = deep_get(payload, ("lesson", "score"))
    if score is None:
        score = deep_get(payload, ("data", "lesson", "score"))
    if score is None:
        return None

    try:
        sc = float(score)
    except Exception:
        return None

    # normalize 0..1 -> percent (except exactly 1.0)
    if 0.0 <= sc <= 1.0 and sc != 1.0:
        sc *= 100.0
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


def thresholds_line(pass_thr: float, great_thr: float) -> str:
    return f"🎯 Порог: {pass_thr:.0f}%. 🔥 Отлично: {great_thr:.0f}%."


# ---------------- app lifespan ----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Required
    bot_token = get_env_any("BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("Missing env var: BOT_TOKEN (or TELEGRAM_BOT_TOKEN)")

    webhook_secret = get_env_any("SKILLSPACE_WEBHOOK_TOKEN", "WEBHOOK_SECRET")
    if not webhook_secret:
        raise RuntimeError("Missing env var: SKILLSPACE_WEBHOOK_TOKEN (or WEBHOOK_SECRET)")

    sheet_id = extract_sheet_id(must_env("GOOGLE_SHEET_ID"))
    sa_json = must_env("GOOGLE_SERVICE_ACCOUNT_JSON")

    # Optional / recommended
    ws_name = os.getenv("GOOGLE_SHEET_WORKSHEET", "").strip() or None

    # Skillspace API (for invite) — optional: if missing, auto-invite disabled
    skillspace_api_key = os.getenv("SKILLSPACE_API_KEY", "").strip()
    skillspace_base_url = os.getenv("SKILLSPACE_BASE_URL", "https://skillspace.ru").strip()

    course_url = os.getenv("SKILLSPACE_COURSE_URL", "").strip()
    expected_course_id = os.getenv("SKILLSPACE_COURSE_ID", "").strip()
    group_id = os.getenv("SKILLSPACE_GROUP_ID", "").strip()

    pass_thr = float(os.getenv("PASS_THRESHOLD", "50"))
    great_thr = float(os.getenv("GREAT_THRESHOLD", "80"))

    # Contact texts (easy to change via env)
    contact_line = os.getenv("CONTACT_LINE", "").strip()  # user set this
    fail_line = os.getenv("FAIL_LINE", "").strip()        # optional override for fail case
    contact_tg = os.getenv("CONTACT_TG", "").strip()      # optional
    contact_label = os.getenv("CONTACT_LABEL", "").strip()  # optional

    # Create Sheets client
    sheets = SheetsClient(sheet_id=sheet_id, worksheet_name=ws_name, service_account_json=sa_json)

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

        # Upsert in a worker thread
        def _sync_upsert():
            return sheets.upsert_lead(lead, now)

        await anyio.to_thread.run_sync(_sync_upsert)

        # Auto-invite (only if both course_id and api_key set)
        invite_ok = False
        invite_reason = ""

        if expected_course_id and skillspace_api_key:
            try:
                await invite_student(
                    api_key=skillspace_api_key,
                    email=profile.email,
                    name=f"tg:{profile.telegram_id}",
                    course_id=expected_course_id,
                    group_id=group_id,
                    base_url=skillspace_base_url,
                )
                invite_ok = True

                def _sync_stage_invited():
                    sheets.upsert_lead(
                        LeadData(
                            telegram_id=profile.telegram_id,
                            email=profile.email,
                            age=profile.age,
                            gender=profile.gender,
                            country=profile.country,
                            language=profile.language,
                            english_level=profile.english_level,
                            amazon_experience=profile.amazon_experience,
                            stage="INVITED_TO_COURSE",
                        ),
                        now,
                    )

                await anyio.to_thread.run_sync(_sync_stage_invited)

            except (SkillspaceError, Exception) as e:
                invite_reason = str(e)
        else:
            if not expected_course_id:
                invite_reason = "Не задан SKILLSPACE_COURSE_ID"
            elif not skillspace_api_key:
                invite_reason = "Не задан SKILLSPACE_API_KEY"

        # Build a lively final message (includes CONTACT_LINE)
        lines = []
        lines.append("🎉 Супер! Данные приняты ✅")
        lines.append(f"📩 Email для Skillspace: {profile.email}")

        if expected_course_id and skillspace_api_key:
            if invite_ok:
                lines.append("🎟️ Я отправил(а) приглашение на курс в Skillspace!")
                lines.append("Если письма не видно — проверь «Спам»/«Промоакции» и попробуй войти по ссылке ниже 😉")
            else:
                lines.append("⚠️ Не получилось отправить приглашение автоматически.")
                lines.append("Ничего страшного — подключим вручную 🙌")
                if invite_reason:
                    lines.append(f"🔧 Причина: {invite_reason}")
        else:
            # Auto-invite disabled (your earlier message)
            lines.append("ℹ️ Авто-инвайт сейчас выключен.")
            if invite_reason:
                lines.append(f"🔧 Причина: {invite_reason}")

        if course_url:
            lines.append("")
            lines.append("🔗 Ссылка на курс:")
            lines.append(course_url)

        lines.append("")
        lines.append("✅ Что дальше:")
        lines.append("1) Пройди обучение в Skillspace 📚")
        lines.append("2) Выполни домашние задания ✍️")
        lines.append("3) Я получу результат по webhook и напишу сюда 🤖")
        lines.append(thresholds_line(pass_thr, great_thr))

        # Contact line (you set CONTACT_LINE) — shown in the same message
        if contact_line:
            lines.append("")
            lines.append(contact_line)
        else:
            # fallback if CONTACT_LINE not set
            if contact_label or contact_tg:
                lines.append("")
                label = contact_label or contact_tg
                lines.append(f"💬 После полного прохождения обучения и ДЗ — напиши в Telegram: {label}")
                if contact_tg and not label.startswith("http"):
                    lines.append(contact_tg)

        return "\n".join(lines)

    bot_service = BotService(token=bot_token, on_lead_completed=on_lead_completed)

    # Store shared state
    app.state.sheets = sheets
    app.state.bot = bot_service
    app.state.pass_thr = pass_thr
    app.state.great_thr = great_thr
    app.state.expected_course_id = expected_course_id
    app.state.webhook_secret = webhook_secret

    app.state.contact_line = contact_line
    app.state.fail_line = fail_line
    app.state.contact_tg = contact_tg
    app.state.contact_label = contact_label

    # Start polling (can be disabled via env if you ever switch to webhook)
    enable_polling = os.getenv("ENABLE_POLLING", "1").strip() == "1"
    polling_task = None
    if enable_polling:
        polling_task = asyncio.create_task(bot_service.start_polling())
        logger.info("Started. Bot polling is running. Skillspace webhook is ready.")
    else:
        logger.info("Started. ENABLE_POLLING=0, polling is disabled. Skillspace webhook is ready.")

    try:
        yield
    finally:
        logger.info("Shutting down")
        if polling_task:
            polling_task.cancel()
        try:
            await bot_service.stop()
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)


# ---------------- routes ----------------
@app.get("/")
async def root():
    return {"ok": True, "service": "procto-bo", "time": utc_iso()}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/telegram-webhook")
async def telegram_webhook_stub():
    # Polling mode: webhook is not used; keep endpoint to avoid 404 if something hits it.
    return JSONResponse({"ok": True, "mode": "polling"}, status_code=200)


@app.post("/skillspace-webhook")
async def skillspace_webhook(request: Request, token: str):
    if token != app.state.webhook_secret:
        raise HTTPException(status_code=401, detail="Bad token")

    payload = await request.json()
    event_name = extract_skillspace_event(payload)

    # We only care about test-end
    if event_name != "test-end":
        return {"ok": True, "ignored": True, "event": event_name}

    email = extract_email(payload)
    if not email:
        logger.warning("Skillspace test-end received but email not found in payload")
        return {"ok": True, "error": "email_not_found_in_payload"}

    # Optional course filter (only if payload contains course_id and expected is set)
    expected = (app.state.expected_course_id or "").strip()
    course_id = extract_course_id(payload)
    if expected and course_id and str(course_id) != str(expected):
        return {"ok": True, "ignored": True, "reason": "course_id_mismatch", "course_id": course_id}

    score = extract_lesson_score(payload)
    lesson_id = extract_lesson_id(payload)

    pass_thr = float(app.state.pass_thr)
    great_thr = float(app.state.great_thr)

    stage = "TEST_FAILED"
    if score is not None and score >= great_thr:
        stage = "TEST_GREAT"
    elif score is not None and score >= pass_thr:
        stage = "TEST_PASSED"

    now = utc_iso()

    def _sync_update():
        return app.state.sheets.update_from_skillspace(
            email=email,
            stage=stage,
            now_iso=now,
            event_name=event_name,
            lesson_score=score,
            lesson_id=lesson_id,
            course_id=course_id,
        )

    await anyio.to_thread.run_sync(_sync_update)

    # Notify user in Telegram
    telegram_id = await anyio.to_thread.run_sync(app.state.sheets.get_telegram_id_by_email, email)

    if telegram_id:
        # Custom contact message (use FAIL_LINE first, then CONTACT_LINE)
        contact_fallback = (app.state.fail_line or "").strip() or (app.state.contact_line or "").strip()
        if not contact_fallback:
            label = (app.state.contact_label or "").strip() or (app.state.contact_tg or "").strip()
            if label:
                contact_fallback = f"💬 После того как пройдёте обучение и выполните домашние задания — напишите: {label}"
            else:
                contact_fallback = "💬 После обучения и домашних заданий — напишите в наш Telegram."

        if score is None:
            text = (
                "✅ Я получил(а) событие о завершении, но балл в webhook не нашёлся 🤔\n\n"
                f"{contact_fallback}"
            )
        else:
            sc = float(score)
            if stage == "TEST_GREAT":
                text = (
                    f"🔥 Отлично! Результат теста: {sc:.0f}%\n\n"
                    "Это очень сильный результат — красавчик(ца)! 💪\n"
                    "Дальше с вами свяжутся по следующим шагам 🙌"
                )
            elif stage == "TEST_PASSED":
                text = (
                    f"✅ Тест пройден! Результат: {sc:.0f}%\n\n"
                    "Проходной порог взят — супер! 🎯\n"
                    "Дальше с вами свяжутся и подскажут следующий шаг 🙌"
                )
            else:
                # Instead of “не дотянули...” — your contact instruction
                text = contact_fallback

        try:
            await app.state.bot.send_message(telegram_id, text)
        except Exception as e:
            logger.warning(f"Failed to send telegram message: {e}")

    return {"ok": True, "event": event_name, "email": email, "score": score, "stage": stage, "lesson_id": lesson_id}
