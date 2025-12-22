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

    # Если пришло 0..1 — переведём в проценты (кроме ровно 1.0)
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


def pretty_thresholds(pass_thr: float, great_thr: float) -> str:
    return f"Проходной порог — {pass_thr:.0f}%, отличный результат — {great_thr:.0f}%."


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- ENV ---
    bot_token = must_env("BOT_TOKEN")

    webhook_secret = must_env("SKILLSPACE_WEBHOOK_TOKEN")  # ?token=...
    skillspace_api_key = must_env("SKILLSPACE_API_KEY")    # API ключ школы
    skillspace_base_url = os.getenv("SKILLSPACE_BASE_URL", "https://skillspace.ru").strip()

    course_url = os.getenv("SKILLSPACE_COURSE_URL", "").strip()
    skillspace_course_id = os.getenv("SKILLSPACE_COURSE_ID", "").strip()  # для инвайта и валидации webhook
    skillspace_group_id = os.getenv("SKILLSPACE_GROUP_ID", "").strip()

    pass_thr = float(os.getenv("PASS_THRESHOLD", "50"))
    great_thr = float(os.getenv("GREAT_THRESHOLD", "80"))

    contact_username = os.getenv("CONTACT_USERNAME", "").strip()  # например @manager

    # --- Sheets ---
    sheet_id = extract_sheet_id(must_env("GOOGLE_SHEET_ID"))
    ws_name = os.getenv("GOOGLE_SHEET_WORKSHEET", "").strip() or None
    sa_json = must_env("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheets = SheetsClient(sheet_id=sheet_id, worksheet_name=ws_name, service_account_json=sa_json)

    # --- Bot callback ---
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

        await anyio.to_thread.run_sync(_sync_upsert)

        # --- Invite in Skillspace ---
        invite_ok = False
        invite_error = ""

        if skillspace_course_id:
            try:
                await invite_student(
                    api_key=skillspace_api_key,
                    email=profile.email,
                    name=f"tg:{profile.telegram_id}",
                    course_id=skillspace_course_id,
                    group_id=skillspace_group_id,
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
                invite_error = str(e)

        # --- Message text (final, clean, human) ---
        lines = []
        lines.append("✅ Отлично, данные приняты.")
        lines.append(f"📩 Email для Skillspace: {profile.email}")

        if skillspace_course_id:
            if invite_ok:
                lines.append("🎟️ Я отправил приглашение на курс в Skillspace.")
                lines.append("Если письма нет — проверь «Спам»/«Промоакции» и попробуй зайти по ссылке ниже под этим email.")
            else:
                lines.append("⚠️ Приглашение в Skillspace отправить не получилось автоматически.")
                if contact_username:
                    lines.append(f"Напиши {contact_username}, мы подключим тебя вручную.")
                else:
                    lines.append("Напиши в поддержку/менеджеру — подключим вручную.")
                if invite_error:
                    lines.append(f"(техническая причина: {invite_error})")
        else:
            lines.append("ℹ️ Авто-инвайт выключен: не задан SKILLSPACE_COURSE_ID.")
            if contact_username:
                lines.append(f"Если нужно — напиши {contact_username}.")

        if course_url:
            lines.append("")
            lines.append("🔗 Ссылка на курс:")
            lines.append(course_url)

        lines.append("")
        lines.append("Что дальше:")
        lines.append("1) Открой курс и пройди вводный урок.")
        lines.append("2) Сдай тест/ДЗ внутри Skillspace.")
        lines.append(f"3) Как только придёт результат — я сразу напишу сюда. {pretty_thresholds(pass_thr, great_thr)}")

        return "\n".join(lines)

    # --- Init bot ---
    bot_service = BotService(token=bot_token, on_lead_completed=on_lead_completed)

    app.state.sheets = sheets
    app.state.bot = bot_service
    app.state.pass_thr = pass_thr
    app.state.great_thr = great_thr
    app.state.course_id = skillspace_course_id
    app.state.webhook_secret = webhook_secret

    polling_task = asyncio.create_task(bot_service.start_polling())
    logger.info("Started. Bot polling is running. Skillspace webhook is ready.")

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
    # Мы в polling. Это просто чтобы не ловить 404, если кто-то куда-то стучится.
    return JSONResponse({"ok": True, "mode": "polling"}, status_code=200)


@app.post("/skillspace-webhook")
async def skillspace_webhook(request: Request, token: str):
    if token != app.state.webhook_secret:
        raise HTTPException(status_code=401, detail="Bad token")

    payload = await request.json()
    event_name = extract_skillspace_event(payload)

    # интересует test-end
    if event_name != "test-end":
        return {"ok": True, "ignored": True, "event": event_name}

    email = extract_email(payload)
    if not email:
        logger.warning("Skillspace test-end received but email not found in payload")
        return {"ok": True, "error": "email_not_found_in_payload"}

    # (опционально) фильтр по курсу — если course_id приходит
    expected_course_id = (app.state.course_id or "").strip()
    course_id = extract_course_id(payload)
    if expected_course_id and course_id and str(course_id) != str(expected_course_id):
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

    # notify telegram
    telegram_id = await anyio.to_thread.run_sync(app.state.sheets.get_telegram_id_by_email, email)

    if telegram_id:
        if score is None:
            text = (
                "✅ Результат теста получен, но балл в webhook не найден.\n"
                "Напиши менеджеру — проверим вручную."
            )
        else:
            sc = score
            if stage == "TEST_GREAT":
                text = (
                    f"🔥 Супер! Тест засчитан на {sc:.0f}%.\n\n"
                    "Это сильный результат — фиксирую тебя как «отлично прошёл».\n"
                    "Дальше с тобой свяжутся по следующим шагам."
                )
            elif stage == "TEST_PASSED":
                text = (
                    f"✅ Тест пройден на {sc:.0f}%.\n\n"
                    "Проходной порог взят — отличная работа.\n"
                    "Дальше с тобой свяжутся и подскажут следующий шаг."
                )
                    else:
            text = (
                "После того как пройдёте полностью обучение и выполните домашние задания, "
                "напишите в телеграмм https://t.me/CREAT113"
            )


        try:
            await app.state.bot.send_message(telegram_id, text)
        except Exception as e:
            logger.warning(f"Failed to send telegram message: {e}")

    return {"ok": True, "event": event_name, "email": email, "score": score, "stage": stage}
