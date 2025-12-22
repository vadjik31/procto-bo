import base64
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials


HEADERS = [
    "created_at",
    "updated_at",
    "telegram_id",
    "email",
    "age",
    "gender",
    "country",
    "language",
    "english_level",
    "amazon_experience",
    "stage",
    "last_event",
    "lesson_score",
    "lesson_id",
    "course_id",
]


@dataclass
class LeadData:
    telegram_id: int
    email: str
    age: str = ""
    gender: str = ""
    country: str = ""
    language: str = ""
    english_level: str = ""
    amazon_experience: str = ""
    stage: str = "NEW"


class SheetsClient:
    def __init__(self, sheet_id: str, worksheet_name: Optional[str], service_account_json: str):
        self.sheet_id = sheet_id
        self.worksheet_name = worksheet_name
        self.service_account_json = service_account_json
        self._gc = self._make_client()

    def _decode_service_json(self) -> Dict[str, Any]:
        raw = (self.service_account_json or "").strip()
        if not raw:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is empty")

        if raw.startswith("{") and raw.endswith("}"):
            return json.loads(raw)

        try:
            decoded = base64.b64decode(raw).decode("utf-8")
            return json.loads(decoded)
        except Exception as e:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON must be JSON string or base64-encoded JSON"
            ) from e

    def _make_client(self) -> gspread.Client:
        info = self._decode_service_json()
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        return gspread.authorize(creds)

    def _get_ws(self):
        sh = self._gc.open_by_key(self.sheet_id)
        if self.worksheet_name:
            ws = sh.worksheet(self.worksheet_name)
        else:
            ws = sh.get_worksheet(0)
        self._ensure_headers(ws)
        return ws

    def _ensure_headers(self, ws) -> None:
        first_row = ws.row_values(1)
        if first_row:
            return
        ws.append_row(HEADERS, value_input_option="RAW")

    def _header_index(self, ws) -> Dict[str, int]:
        headers = ws.row_values(1)
        return {h: i + 1 for i, h in enumerate(headers)}

    def _find_row_by_email(self, ws, email: str) -> Optional[int]:
        if not email:
            return None
        try:
            cell = ws.find(email)
            return cell.row if cell else None
        except Exception:
            return None

    def _find_row_by_telegram(self, ws, telegram_id: int) -> Optional[int]:
        try:
            cell = ws.find(str(telegram_id))
            return cell.row if cell else None
        except Exception:
            return None

    def upsert_lead(self, lead: LeadData, now_iso: str) -> Tuple[int, str]:
        """
        Returns (row_number, action) where action is 'insert' or 'update'
        """
        ws = self._get_ws()
        row = self._find_row_by_email(ws, lead.email) or self._find_row_by_telegram(ws, lead.telegram_id)

        values_map = {
            "created_at": now_iso,
            "updated_at": now_iso,
            "telegram_id": str(lead.telegram_id),
            "email": lead.email,
            "age": lead.age,
            "gender": lead.gender,
            "country": lead.country,
            "language": lead.language,
            "english_level": lead.english_level,
            "amazon_experience": lead.amazon_experience,
            "stage": lead.stage,
            "last_event": "",
            "lesson_score": "",
            "lesson_id": "",
            "course_id": "",
        }

        if row is None:
            row_values = [values_map.get(h, "") for h in HEADERS]
            ws.append_row(row_values, value_input_option="RAW")
            new_row = len(ws.get_all_values())
            return new_row, "insert"

        idx = self._header_index(ws)
        # created_at не трогаем при апдейте
        values_map["created_at"] = ws.cell(row, idx["created_at"]).value or values_map["created_at"]

        for key, val in values_map.items():
            col = idx.get(key)
            if col:
                ws.update_cell(row, col, val)

        return row, "update"

    def update_from_skillspace(
        self,
        *,
        email: str,
        stage: str,
        now_iso: str,
        event_name: str,
        lesson_score: Optional[float],
        lesson_id: Optional[str],
        course_id: Optional[str],
    ) -> Optional[int]:
        ws = self._get_ws()
        row = self._find_row_by_email(ws, email)
        if row is None:
            return None

        idx = self._header_index(ws)
        updates = {
            "updated_at": now_iso,
            "stage": stage,
            "last_event": event_name,
            "lesson_score": "" if lesson_score is None else str(lesson_score),
            "lesson_id": lesson_id or "",
            "course_id": course_id or "",
        }

        for key, val in updates.items():
            col = idx.get(key)
            if col:
                ws.update_cell(row, col, val)

        return row

    def get_telegram_id_by_email(self, email: str) -> Optional[int]:
        ws = self._get_ws()
        row = self._find_row_by_email(ws, email)
        if row is None:
            return None

        idx = self._header_index(ws)
        col = idx.get("telegram_id")
        if not col:
            return None

        val = (ws.cell(row, col).value or "").strip()
        return int(val) if val.isdigit() else None
