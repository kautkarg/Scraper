"""FastAPI backend controller — job API, SSE progress stream, static UI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from connectors.fastcrw_client import FastCRWClient
from connectors.google_sheets import GoogleSheetsSink
from connectors.webhook import WebhookSink
from core.config import get_config
from core.exporter import Destination
from core.orchestrator import BUS, JobRequest, JobStatus, Orchestrator
from core.parser import FieldType, SchemaField

config = get_config()
app = FastAPI(title="Project Omnisearch", version="0.1.0",
              description="Autonomous intent-based data extraction engine")
orchestrator = Orchestrator(config)

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


# --------------------------------------------------------------------------
# request / response models
# --------------------------------------------------------------------------

class SchemaFieldIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    type: FieldType = FieldType.STRING
    required: bool = False
    description: str = ""


class JobCreate(BaseModel):
    intent: str = Field(min_length=3, max_length=2000)
    fields: list[SchemaFieldIn] = Field(min_length=1)
    destination: Destination = Destination.CSV
    max_records: int | None = Field(default=None, ge=1, le=500)
    seed_urls: list[str] = []
    table_name: str = "records"
    webhook_url: str = ""
    spreadsheet_id: str = ""


class JobAccepted(BaseModel):
    job_id: str
    status: str


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.get("/api/health")
async def health() -> dict[str, Any]:
    client = FastCRWClient(config)
    try:
        engine = await client.health()
    finally:
        await client.aclose()
    sheets = await GoogleSheetsSink(config).probe()
    webhook = await WebhookSink(config).probe()
    llm_on = bool(config.get("llm.enabled", False))
    llm_mode = str(config.get("llm.mode", "openai"))
    llm_ready = llm_on
    if llm_on and llm_mode == "opencode":
        import shutil
        llm_ready = shutil.which(str(config.get("llm.opencode_bin", "opencode"))) is not None
    return {
        "status": "ok",
        "engine": engine,
        "sinks": {"google_sheets": sheets, "webhook": webhook},
        "llm": {"enabled": llm_on, "mode": llm_mode,
                "model": config.get("llm.model"), "ready": llm_ready},
    }


@app.post("/api/jobs", response_model=JobAccepted, status_code=202)
async def create_job(body: JobCreate) -> JobAccepted:
    request = JobRequest.from_dict(body.model_dump())
    state = BUS.create(request)
    state.status = JobStatus.QUEUED
    asyncio.create_task(orchestrator.run(state))
    return JobAccepted(job_id=state.id, status=state.status.value)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    state = BUS.get(job_id)
    if state is None:
        raise HTTPException(404, "job not found")
    return {
        "job_id": state.id,
        "status": state.status.value,
        "records": len(state.records),
        "summary": state.summary,
        "events": len(state.history),
    }


@app.get("/api/jobs/{job_id}/records")
async def get_records(job_id: str) -> dict[str, Any]:
    state = BUS.get(job_id)
    if state is None:
        raise HTTPException(404, "job not found")
    headers = [f.name for f in state.request.fields]
    return {"job_id": job_id, "headers": headers, "records": state.records}


@app.get("/api/jobs/{job_id}/download")
async def download(job_id: str, format: str = "csv") -> FileResponse:
    state = BUS.get(job_id)
    if state is None:
        raise HTTPException(404, "job not found")
    summary = state.summary or {}
    for output in summary.get("outputs", []):
        path = output.get("path")
        if output.get("format") == format and path and Path(path).is_file():
            return FileResponse(path, filename=Path(path).name,
                                media_type="application/octet-stream")
    raise HTTPException(404, f"no {format} output available for this job")


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    state = BUS.get(job_id)
    if state is None:
        raise HTTPException(404, "job not found")
    queue = BUS.subscribe(job_id)

    async def stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    state_now = BUS.get(job_id)
                    if state_now is None:
                        break
                    yield _sse("status", {"type": "status",
                                          "status": state_now.status.value})
                    if state_now.done:
                        break
                    continue
                yield _sse(event.get("type", "message"), event)
                # ``summary`` is always the final event of a job — the only safe
                # place to close the stream (``error`` is followed by it).
                if event.get("type") == "summary":
                    break
        finally:
            BUS.unsubscribe(job_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


def _sse(event: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {data}\n\n"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=config.get("server.host", "127.0.0.1"),
                port=int(config.get("server.port", 8000)), reload=False)
