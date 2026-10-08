"""Export targets: CSV, JSON, Markdown tables, SQLite — plus dispatch to the
Google Sheets / webhook sinks. All file exports land in ``paths.outputs``."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from core.config import Config


class Destination(str, Enum):
    CSV = "csv"
    JSON = "json"
    MARKDOWN = "markdown"
    SQLITE = "sqlite"
    GOOGLE_SHEETS = "google_sheets"
    WEBHOOK = "webhook"


class ExportError(RuntimeError):
    pass


class ExportResult:
    def __init__(self, fmt: str, path: str | None, rows: int, extra: dict[str, Any] | None = None):
        self.format = fmt
        self.path = path
        self.rows = rows
        self.extra = extra or {}

    def to_dict(self) -> dict[str, Any]:
        return {"format": self.format, "path": self.path, "rows": self.rows, **self.extra}


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _safe_table(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", name.strip()).strip("_").lower()
    return cleaned or "records"


class Exporter:
    def __init__(self, config: Config):
        self.config = config
        self.outputs = config.outputs_dir

    # -- file formats ---------------------------------------------------------
    def export(self, records: list[dict[str, Any]], headers: list[str],
               destination: Destination, job_id: str,
               table_name: str = "records") -> ExportResult:
        if destination is Destination.CSV:
            return self.to_csv(records, headers, job_id)
        if destination is Destination.JSON:
            return self.to_json(records, headers, job_id)
        if destination is Destination.MARKDOWN:
            return self.to_markdown(records, headers, job_id)
        if destination is Destination.SQLITE:
            return self.to_sqlite(records, headers, job_id, table_name)
        raise ExportError(f"{destination.value} is handled by a connector sink, not the file exporter")

    def to_csv(self, records: list[dict[str, Any]], headers: list[str],
               job_id: str) -> ExportResult:
        path = self.outputs / f"omnisearch_{_stamp()}_{job_id[:8]}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            for record in records:
                writer.writerow({h: _flatten(record.get(h)) for h in headers})
        return ExportResult("csv", str(path), len(records))

    def to_json(self, records: list[dict[str, Any]], headers: list[str],
                job_id: str) -> ExportResult:
        path = self.outputs / f"omnisearch_{_stamp()}_{job_id[:8]}.json"
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "job_id": job_id,
            "count": len(records),
            "fields": headers,
            "records": records,
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return ExportResult("json", str(path), len(records))

    def to_markdown(self, records: list[dict[str, Any]], headers: list[str],
                    job_id: str) -> ExportResult:
        path = self.outputs / f"omnisearch_{_stamp()}_{job_id[:8]}.md"
        lines = ["| " + " | ".join(headers) + " |",
                 "|" + "|".join([" --- "] * len(headers)) + "|"]
        for record in records:
            cells = [_escape_md(record.get(h)) for h in headers]
            lines.append("| " + " | ".join(cells) + " |")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ExportResult("markdown", str(path), len(records))

    def to_sqlite(self, records: list[dict[str, Any]], headers: list[str],
                  job_id: str, table_name: str = "records") -> ExportResult:
        db_path = self.config.sqlite_path
        table = _safe_table(table_name)
        columns = ", ".join(f'"{h}" TEXT' for h in headers)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" '
                         f'(id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, '
                         f'source_at TEXT, {columns})')
            existing = {row[1] for row in
                        conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
            for header in headers:  # expand/contract for evolving schemas
                if header not in existing:
                    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{header}" TEXT')
            now = datetime.now(timezone.utc).isoformat()
            placeholders = ", ".join(["?"] * (len(headers) + 2))
            column_sql = ", ".join([f'"{h}"' for h in headers])
            conn.executemany(
                f'INSERT INTO "{table}" (job_id, source_at, {column_sql}) VALUES ({placeholders})',
                [(job_id, now, *[ _flatten(record.get(h)) for h in headers])
                 for record in records],
            )
            conn.commit()
        finally:
            conn.close()
        return ExportResult("sqlite", str(db_path), len(records), {"table": table})

    # -- helper: always keep a local JSON mirror for sink destinations --------
    def mirror_json(self, records: list[dict[str, Any]], headers: list[str],
                    job_id: str) -> ExportResult:
        return self.to_json(records, headers, job_id)


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _escape_md(value: Any) -> str:
    text = _flatten(value)
    return text.replace("|", "\\|").replace("\n", " ")
