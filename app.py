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


def extract_skillspace_event(payload: Dict[str, Any]) -> str:
    # purely for logging
    for k in ("event", "type", "event_name", "name"):
        v = payload.get(k)
        if isinstance(v, str) and v:
            return v
    v = deep_get(payload, ("data", "event"))
    return v if isinstance(v, str) else ""


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

    # Optional
    ws_name = os.getenv("GOOGLE_SHEET_WORKSHEET", "").strip() or None

    # Skillspace API for invite (if missing → auto-invite disabled)
    skillspace_api_key = os.getenv("SKILLSPACE_API_KEY", "").strip()
    skillspace_base_url = os.getenv("SKILLSPACE_BASE_URL", "https://skillspace.ru").strip()

    course_url = os.getenv("SKILLSPACE_COURSE_URL", "").strip()
    expected_course_id = os.getenv("SKILLSPACE_COURSE_ID", "").strip()
    group_id = os.getenv("SKILLSPACE_GROUP_ID", "").strip()

    # Contact text (you set this)
    contact_line = os.getenv("CONTACT_LINE", "").strip()

    # Sheets
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

        # ---------- YOUR FINAL MESSAGE (NO TEST CHECKS) ----------
        lines = []
        lines.append("✅ Отлично, данные приняты.")
        lines.append(f"📩 Email для Skillspace: {profile.email}")

        if expected_course_id and skillspace_api_key and invite_ok:
            lines.append("🎟️ Я отправил приглашение на курс в Skillspace.")
            lines.append("Если письма нет — проверь «Спам»/«Промоакции» и попробуй зайти по ссылке ниже под этим email.")
        elif expected_course_id and skillspace_api_key and not invite_ok:
            lines.append("⚠️ Приглашение не отправилось автоматически (бывает).")
            lines.append("Если письма нет — проверь «Спам»/«Промоакции» и попробуй зайти по ссылке ниже под этим email.")
            if invite_reason:
                lines.append(f"🔧 Тех. причина: {invite_reason}")
        else:
            lines.append("ℹ️ Авто-инвайт выключен (нужны SKILLSPACE_COURSE_ID и SKILLSPACE_API_KEY).")
            if invite_reason:
                lines.append(f"🔧 Причина: {invite_reason}")

        if course_url:
            lines.append("")
            lines.append("🔗 Ссылка на курс:")
            lines.append(course_url)
            lines.append("Главное заходить с того же браузерного профиля где установлена указанная почта.")

        lines.append("")
        lines.append("Что дальше:")
        lines.append("1.Проходи обучение.")
        lines.append("2.Сделай домашнее задание.")
        lines.append("3.Как сделаешь напиши по указанному телеграмму ниже.")
        if contact_line:
            lines.append(contact_line)

        lines.append("")
        lines.append("Важно, пиши только если просмотрел видео-уроки и выполнил домашнее задание.")
        lines.append("")
        lines.append('Вопросы по поводу "условий" работы ты можешь посмотреть на сайте https://procto13llcwork.work/')
        lines.append("")
        lines.append("А как будет выглядеть процесс работы ты сможешь узнать на курсе, не бойся , курс не длинный и достаточно интересный.")

        return "\n".join(lines)

    bot_service = BotService(token=bot_token, on_lead_completed=on_lead_completed)

    # Store shared state
    app.state.sheets = sheets
    app.state.bot = bot_service
    app.state.webhook_secret = webhook_secret

    # Start polling
    enable_polling = os.getenv("ENABLE_POLLING", "1").strip() == "1"
    polling_task = None
    if enable_polling:
        polling_task = asyncio.create_task(bot_service.start_polling())
        logger.info("Started. Bot polling is running. Skillspace webhook is ready (stub mode).")
    else:
        logger.info("Started. ENABLE_POLLING=0, polling is disabled. Skillspace webhook is ready (stub mode).")

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
    """
    STUB endpoint:
    Skillspace can keep sending webhooks here, but we DO NOT evaluate tests and DO NOT message user.
    We just return 200 OK to acknowledge receipt.
    """
    if token != app.state.webhook_secret:
        raise HTTPException(status_code=401, detail="Bad token")

    try:
        payload = await request.json()
        event_name = extract_skillspace_event(payload)
        logger.info(f"Skillspace webhook received (ignored): event={event_name}")
    except Exception:
        logger.info("Skillspace webhook received (ignored): non-json payload")

    return {"ok": True, "ignored": True}
