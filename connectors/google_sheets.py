"""Google Sheets sink — appends record batches via the Sheets REST API.

Uses a service-account JSON + google-auth for JWT signing; rows are pushed
with plain httpx (no heavyweight Google client libraries). When credentials
are missing the connector reports ``dry_run`` so the rest of the pipeline
still completes locally.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from core.config import Config

SHEETS_VALUES_URL = "https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{rng}:append"


class GoogleSheetsError(RuntimeError):
    pass


class GoogleSheetsSink:
    def __init__(self, config: Config):
        self.credentials_file = config.get("integrations.google_sheets.credentials_file", "")
        self.spreadsheet_id = config.get("integrations.google_sheets.spreadsheet_id", "")
        self._creds = None
        self._error: str | None = None

    # -- auth ---------------------------------------------------------------
    def _load_credentials(self, spreadsheet_id: str) -> Any:
        if self._creds is not None:
            return self._creds
        path = Path(self.credentials_file)
        if not path.is_file():
            raise GoogleSheetsError(
                f"service-account file not found: {path or '(unset)'} — "
                "set integrations.google_sheets.credentials_file in config.yaml"
            )
        try:
            from google.oauth2 import service_account
        except ImportError as exc:
            raise GoogleSheetsError("google-auth is not installed (pip install google-auth)") from exc
        info = json.loads(path.read_text(encoding="utf-8"))
        self._creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        return self._creds

    def _token(self, spreadsheet_id: str) -> str:
        creds = self._load_credentials(spreadsheet_id)
        if not creds.valid:
            creds.refresh(httpx.Request("POST", creds.token_uri))
        return creds.token

    # -- append ---------------------------------------------------------------
    async def append(self, records: list[dict[str, Any]], headers: list[str],
                     spreadsheet_id: str | None = None,
                     sheet_name: str = "Omnisearch") -> dict[str, Any]:
        sid = spreadsheet_id or self.spreadsheet_id
        if not sid:
            raise GoogleSheetsError("no spreadsheet_id provided (UI field or config)")
        if not records:
            return {"sink": "google_sheets", "rows": 0, "dry_run": False}

        rows = [[rec.get(h, "") for h in headers] for rec in records]
        payload: dict[str, Any] = {"values": [headers, *rows]} if rows else {"values": []}
        url = SHEETS_VALUES_URL.format(sid=sid, rng=sheet_name)
        token = self._token(sid)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url, params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
                json=payload, headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code >= 400:
            raise GoogleSheetsError(f"sheets append HTTP {resp.status_code}: {resp.text[:300]}")
        return {"sink": "google_sheets", "rows": len(rows), "spreadsheet_id": sid,
                "sheet": sheet_name, "dry_run": False}

    async def probe(self) -> dict[str, Any]:
        """Non-throwing status check for /api/health."""
        path = Path(self.credentials_file) if self.credentials_file else None
        ok = bool(path and path.is_file())
        return {
            "sink": "google_sheets",
            "ready": ok,
            "credentials_file": str(path) if path else None,
            "error": None if ok else "service-account credentials file missing",
        }
