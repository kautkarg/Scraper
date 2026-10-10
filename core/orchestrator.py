"""Mimo prompt-to-plan execution loop.

Pipeline:  Query Formulation -> fastCRW Discovery -> Scrape/Extract ->
           Schema Normalization -> Export. Every step publishes SSE events
           (``stage`` / ``record`` / ``summary`` / ``error``) so the web UI
           can stream a live terminal and data grid.

Resilience contract (PRD §5.3): blocked domains are logged as skips and the
job continues from alternative sources; sink failures degrade to a local
mirror instead of failing the job.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from connectors.fastcrw_client import (BlockedError, DiscoveryError, FastCRWClient,
                                       ScrapeError, SearchResult)
from connectors.google_sheets import GoogleSheetsError, GoogleSheetsSink
from connectors.webhook import WebhookError, WebhookSink
from core.llm import complete as llm_complete
from core.llm import extract_json
from core.config import Config
from core.exporter import Destination, Exporter
from core.parser import RecordParser, SchemaField, is_error_page_title

TERMINAL_STATES = {"completed", "failed"}


class JobStatus(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    DISCOVERING = "discovering"
    EXTRACTING = "extracting"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    FAILED = "failed"


class JobRequest:
    """User intent + schema + routing (kept as a plain dataclass for speed)."""

    def __init__(self, intent: str, fields: list[SchemaField],
                 destination: Destination = Destination.CSV, max_records: int | None = None,
                 seed_urls: list[str] | None = None, table_name: str = "records",
                 webhook_url: str = "", spreadsheet_id: str = ""):
        self.intent = intent.strip()
        self.fields = fields
        self.destination = destination
        self.max_records = max_records
        self.seed_urls = [u.strip() for u in (seed_urls or []) if u.strip()]
        self.table_name = table_name
        self.webhook_url = webhook_url
        self.spreadsheet_id = spreadsheet_id

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobRequest":
        fields = [SchemaField(**f) for f in data.get("fields", [])]
        return cls(
            intent=data.get("intent", ""),
            fields=fields,
            destination=Destination(data.get("destination", "csv")),
            max_records=data.get("max_records"),
            seed_urls=data.get("seed_urls") or [],
            table_name=data.get("table_name", "records"),
            webhook_url=data.get("webhook_url", ""),
            spreadsheet_id=data.get("spreadsheet_id", ""),
        )


@dataclass
class SkipInfo:
    url: str
    reason: str


@dataclass
class JobState:
    id: str
    request: JobRequest
    status: JobStatus = JobStatus.QUEUED
    records: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""

    @property
    def done(self) -> bool:
        return self.status.value in TERMINAL_STATES


class EventBus:
    """In-process pub/sub with history replay so late SSE clients miss nothing."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}

    def create(self, request: JobRequest) -> JobState:
        job_id = uuid.uuid4().hex[:12]
        state = JobState(id=job_id, request=request,
                         created_at=_now(), started_at=_now())
        self._jobs[job_id] = state
        return state

    def get(self, job_id: str) -> JobState | None:
        return self._jobs.get(job_id)

    def publish(self, job_id: str, event: dict[str, Any]) -> None:
        state = self._jobs.get(job_id)
        if state is None:
            return
        event.setdefault("ts", _now())
        state.history.append(event)
        for queue in state.subscribers:
            queue.put_nowait(event)

    def subscribe(self, job_id: str) -> asyncio.Queue | None:
        state = self._jobs.get(job_id)
        if state is None:
            return None
        queue: asyncio.Queue = asyncio.Queue()
        for past in state.history:          # replay backlog first
            queue.put_nowait(past)
        state.subscribers.append(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        state = self._jobs.get(job_id)
        if state and queue in state.subscribers:
            state.subscribers.remove(queue)


BUS = EventBus()


# --------------------------------------------------------------------------
# query formulation (LLM optional, deterministic fallback)
# --------------------------------------------------------------------------

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "find", "get", "list", "collect", "scrape", "extract", "whose", "have",
    "has", "into", "our", "all", "any", "some", "who", "which", "their", "them",
    "they", "you", "your", "about", "over", "than", "then", "when", "where",
    "why", "how", "what", "can", "could", "would", "should", "will", "not",
    "but", "its", "also", "via", "per", "one", "two", "new", "top", "best",
}


def heuristic_queries(intent: str, fields: list[SchemaField], limit: int = 8) -> list[str]:
    base = re.sub(r"\s+", " ", intent).strip().rstrip(".!?")
    quoted = [m.group(1) or m.group(2) for m in
              re.finditer(r'"([^"]+)"|“([^”]+)”', intent)]
    tokens = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9+.#-]+", base)
              if t.lower() not in _STOPWORDS]
    queries: list[str] = []
    if quoted:
        queries.append(f"{quoted[0]} list")
    queries.append(base[:150])
    core = " ".join(tokens[:8]) or base[:80]
    queries.append(f"{core} directory")
    wants_email = any(f.type.value == "email" for f in fields)
    wants_url = any(f.type.value == "url" for f in fields)
    if wants_email:
        queries.append(f"{core} contact email")
    if wants_url:
        queries.append(f"{core} official website")
    seen: set[str] = set()
    unique: list[str] = []
    for q in queries:
        key = q.lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(q)
    return unique[:limit]


async def llm_queries(config: Config, intent: str, fields: list[SchemaField],
                      log) -> list[str] | None:
    if not config.get("llm.enabled", False):
        return None
    schema_hint = ", ".join(f"{f.name}:{f.type.value}" for f in fields)
    prompt = (
        "You are a search-query planner for a data harvesting engine. "
        "Given a user intent and target schema, produce 6-10 diverse web search "
        "queries that would surface pages containing those fields. "
        "Prefer directory/list/database pages that publish many entries with "
        "contact details (e.g. 'X directory', 'list of X with email', "
        "'top X companies contact list'). "
        "Never target news articles, blog posts, social feed URLs (LinkedIn "
        "posts/pulse, Medium, X/Twitter), videos, or encyclopedias — they never "
        "contain structured records. Do not use site: operators on those hosts. "
        'Respond with JSON only: {"queries": ["..."]}\n\n'
        f"Intent: {intent}\nSchema: {schema_hint}"
    )
    try:
        content = await llm_complete(config, prompt)
        payload = extract_json(content or "")
        queries = [str(q) for q in payload.get("queries", []) if q]
        if queries:
            log("info", f"Mimo planner produced {len(queries)} queries")
            return queries[:10]
    except Exception as exc:  # noqa: BLE001 — LLM is strictly optional
        log("warn", f"Mimo planner unavailable, using heuristic queries ({exc})")
    return None


async def llm_records(config: Config, markdown: str, source_url: str,
                      fields: list[SchemaField]) -> list[dict[str, Any]] | None:
    if not config.get("llm.enabled", False):
        return None
    schema = [{"name": f.name, "type": f.type.value, "required": f.required,
               "description": f.description} for f in fields]
    prompt = (
        "Extract records from the markdown below that match this JSON schema. "
        "Only use facts present in the markdown — never invent values. "
        'Respond with JSON only: {"records": [ {...} ]}\n\n'
        f"Schema: {json.dumps(schema)}\n"
        f"Source URL: {source_url}\n\n---\n{markdown[:12000]}"
    )
    try:
        content = await llm_complete(config, prompt)
        payload = extract_json(content or "")
        records = payload.get("records", payload if isinstance(payload, list) else [])
        if isinstance(records, list):
            return [r for r in records if isinstance(r, dict)]
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------
# the job runner
# --------------------------------------------------------------------------

class Orchestrator:
    def __init__(self, config: Config, bus: EventBus = BUS):
        self.config = config
        self.bus = bus

    # -- event helpers -------------------------------------------------------
    def _stage(self, job_id: str, stage: str, message: str, level: str = "info") -> None:
        self.bus.publish(job_id, {"type": "stage", "stage": stage,
                                  "level": level, "message": message})

    def _record(self, job_id: str, values: dict[str, Any]) -> None:
        self.bus.publish(job_id, {"type": "record", "values": values})

    def _finish(self, state: JobState, status: JobStatus, summary: dict[str, Any]) -> None:
        state.status = status
        state.summary = summary
        state.finished_at = _now()
        self.bus.publish(state.id, {"type": "summary", "summary": summary})

    # -- main loop -----------------------------------------------------------
    async def run(self, state: JobState) -> None:
        t0 = time.monotonic()
        request = state.request
        job_id = state.id
        log = lambda level, msg: self._stage(job_id, state.status.value, msg, level)  # noqa: E731
        skipped: list[SkipInfo] = []
        notes: list[str] = []
        queries: list[str] = []
        scraped_urls: list[str] = []

        if not request.intent:
            self._finish(state, JobStatus.FAILED, _summary(state, [], [], [], skipped,
                                                            notes + ["intent is empty"], t0, error="intent is empty"))
            self.bus.publish(job_id, {"type": "error", "message": "Intent is empty — describe what you want to find."})
            return

        client = FastCRWClient(self.config, log=log)
        parser = RecordParser(request.fields)
        max_records = int(request.max_records or self.config.get("execution.max_records_per_job", 50))
        try:
            # ---- stage 1: query formulation --------------------------------
            state.status = JobStatus.PLANNING
            self._stage(job_id, "planning", f"Intent received: {request.intent[:160]}")
            health = await client.health()
            if health["search_ready"]:
                engines = [k for k, v in health["engines"].items() if v and k != "http_fallback"]
                self._stage(job_id, "planning", f"Discovery engines online: {', '.join(engines)}")
            else:
                self._stage(job_id, "planning",
                            "No search engine online — relying on seed URLs. "
                            "Start fastCRW (`crw serve` or `docker compose up`) for autonomous discovery.",
                            "warn")
                notes.append("no search engine reachable; used seed URLs only")
            queries = await llm_queries(self.config, request.intent, request.fields, log) \
                or heuristic_queries(request.intent, request.fields)
            for i, q in enumerate(queries, 1):
                self._stage(job_id, "planning", f"Query {i}/{len(queries)}: {q}")

            # ---- stage 2: multi-source discovery ----------------------------
            state.status = JobStatus.DISCOVERING
            candidates: list[SearchResult] = [SearchResult(title="seed", url=u,
                                                           engine="seed", query="seed")
                                              for u in request.seed_urls]
            discovery_failed: str | None = None
            for q in queries:
                try:
                    results = await client.search(
                        q, int(self.config.get("execution.max_results_per_query", 15)))
                    candidates.extend(results)
                    self._stage(job_id, "discovering", f"{len(results)} results for: {q[:90]}")
                except DiscoveryError as exc:
                    discovery_failed = str(exc)
                    self._stage(job_id, "discovering", f"Query skipped: {exc}", "warn")
                await asyncio.sleep(self.config.get("execution.request_delay_seconds", 0.2))

            before_rank = len(candidates)
            candidates = rank_search_results(candidates, request.intent)
            if len(candidates) < before_rank:
                self._stage(job_id, "discovering",
                            f"intent filter: {before_rank} results -> "
                            f"{len(candidates)} on-topic")

            urls = _dedupe_urls(candidates, self.config)
            if not urls:
                reason = discovery_failed or (
                    "No seed URLs supplied. Provide seed URLs or start the fastCRW "
                    "search backend (`docker compose up -d` bundles SearXNG).")
                raise DiscoveryError(reason)
            urls = urls[: int(self.config.get("execution.max_pages_per_job", 60))]
            self._stage(job_id, "discovering",
                        f"{len(urls)} unique source pages queued "
                        f"({len(candidates)} raw candidates)")

            # ---- stage 3+4: scrape -> normalize (streaming) -----------------
            state.status = JobStatus.EXTRACTING
            semaphore = asyncio.Semaphore(
                int(self.config.get("execution.max_concurrent_scrapes", 6)))
            delay = float(self.config.get("execution.request_delay_seconds", 0.2))
            stop = asyncio.Event()

            async def worker(url: str) -> None:
                if stop.is_set():
                    return
                async with semaphore:
                    if stop.is_set():
                        return
                    try:
                        await _harvest_one(url)
                    except BlockedError as exc:
                        skipped.append(SkipInfo(url=exc.url,
                                                reason=f"blocked ({exc.reason})"))
                        self._stage(job_id, "extracting",
                                    f"SKIP {url} — {exc.reason}", "warn")
                    except ScrapeError as exc:
                        skipped.append(SkipInfo(url=url, reason=str(exc)[:200]))
                        self._stage(job_id, "extracting",
                                    f"SKIP {url} — {exc}", "warn")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 — a single odd URL (LLM,
                        # parser, transport edge cases) must never abort the job
                        skipped.append(SkipInfo(
                            url=url,
                            reason=f"{type(exc).__name__}: {exc}"[:200]))
                        self._stage(job_id, "extracting",
                                    f"SKIP {url} — {type(exc).__name__}: {exc}", "warn")

            async def _harvest_one(url: str) -> None:
                result = await client.scrape(url)
                if stop.is_set():
                    return
                # Interstitial / bot-challenge / error pages are worthless —
                # gate BEFORE the LLM path (which would happily extract a
                # fake record from "REQUEST DENIED!" pages).
                if is_error_page_title(result.title):
                    skipped.append(SkipInfo(url=url,
                                            reason=f"error page ({result.title[:60]})"))
                    self._stage(job_id, "extracting",
                                f"SKIP {url} — error page ({result.title[:60]})",
                                "warn")
                    return
                parser.mark_page()
                scraped_urls.append(result.url)
                self._stage(job_id, "extracting",
                            f"scraped {result.url} [{result.engine}, {result.elapsed_ms}ms]"
                            f" — {len(state.records)}/{max_records} records")
                llm = await llm_records(self.config, result.markdown, result.url,
                                        request.fields)
                candidates_records = llm if llm is not None else parser.heuristic_records(
                    result.markdown, result.url, result.title)
                fresh = parser.ingest(candidates_records, result.url,
                                      result.markdown)
                for record in fresh:
                    if len(state.records) >= max_records:
                        stop.set()
                        return
                    state.records.append(record)
                    self._record(job_id, record)
                if len(state.records) >= max_records:
                    stop.set()
                await asyncio.sleep(delay)

            tasks = [asyncio.create_task(worker(u)) for u in urls]
            await asyncio.gather(*tasks)
            await client.aclose()

            stats = parser.stats
            self._stage(job_id, "extracting",
                        f"Extraction done: {stats.valid} valid / {stats.candidates} candidates "
                        f"from {stats.pages} pages ({stats.duplicates} duplicates, "
                        f"{len(skipped)} skipped)")
            if stats.candidates and stats.validity_ratio < 0.9:
                notes.append(f"validity ratio {stats.validity_ratio:.0%} below 90% target")
                self._stage(job_id, "extracting",
                            f"Validity {stats.validity_ratio:.0%} below the 90% target — "
                            "consider refining the intent or schema.", "warn")
            if not state.records:
                msg = ("No valid records matched the schema. Sources were reached but "
                       "content did not yield the requested fields — try a more specific "
                       "intent, looser required flags, or extra seed URLs.")
                raise RuntimeError(msg)

            # ---- stage 5: export --------------------------------------------
            state.status = JobStatus.EXPORTING
            self._stage(job_id, "exporting",
                        f"Normalizing {len(state.records)} records -> {request.destination.value}")
            outputs = await self._export(state, parser.headers)

            summary = _summary(state, queries, scraped_urls, outputs, skipped, notes, t0,
                               status=JobStatus.COMPLETED)
            summary["stats"] = stats.model_dump()
            summary["validity_ratio"] = stats.validity_ratio
            self._stage(job_id, "exporting",
                        f"Done: {len(state.records)} records, validity "
                        f"{stats.validity_ratio:.0%}, {len(skipped)} domains skipped")
            self._finish(state, JobStatus.COMPLETED, summary)

        except (DiscoveryError, RuntimeError) as exc:
            message = str(exc) or f"{type(exc).__name__} (no detail)"
            self.bus.publish(job_id, {"type": "error", "message": message})
            self._stage(job_id, "failed", message, "error")
            self._finish(state, JobStatus.FAILED,
                         _summary(state, queries, scraped_urls, [], skipped,
                                  notes, t0, error=message))
        except Exception as exc:  # noqa: BLE001 — never leave a job hanging
            # str(exc) can be '' (httpx timeouts) — include the type so the
            # failure is never blank, and dump the traceback to stdout so the
            # Render log carries the real cause.
            detail = str(exc) or repr(exc)
            message = f"unexpected error: {type(exc).__name__}: {detail}"
            traceback.print_exc()
            self.bus.publish(job_id, {"type": "error", "message": message})
            self._stage(job_id, "failed", message, "error")
            self._finish(state, JobStatus.FAILED,
                         _summary(state, queries, scraped_urls, [], skipped,
                                  notes, t0, error=message))
        finally:
            await client.aclose()

    # -- export ---------------------------------------------------------------
    async def _export(self, state: JobState, headers: list[str]) -> list[dict[str, Any]]:
        request = state.request
        exporter = Exporter(self.config)
        outputs: list[dict[str, Any]] = []

        if request.destination in {Destination.CSV, Destination.JSON,
                                   Destination.MARKDOWN, Destination.SQLITE}:
            result = exporter.export(state.records, headers, request.destination,
                                     state.id, request.table_name)
            outputs.append(result.to_dict())
            self._stage(state.id, "exporting",
                        f"Wrote {result.rows} rows -> {result.path}")
            return outputs

        # sinks keep a local JSON mirror so data is never lost remotely
        mirror = exporter.mirror_json(state.records, headers, state.id)
        outputs.append(mirror.to_dict())
        self._stage(state.id, "exporting", f"Local mirror -> {mirror.path}")

        if request.destination is Destination.GOOGLE_SHEETS:
            try:
                sink = GoogleSheetsSink(self.config)
                res = await sink.append(state.records, headers, request.spreadsheet_id or None)
                outputs.append(res)
                self._stage(state.id, "exporting",
                            f"Appended {res['rows']} rows to Google Sheets")
            except GoogleSheetsError as exc:
                outputs.append({"format": "google_sheets", "error": str(exc), "rows": 0})
                self._stage(state.id, "exporting", f"Google Sheets failed: {exc}", "warn")

        if request.destination is Destination.WEBHOOK:
            try:
                sink = WebhookSink(self.config)
                res = await sink.push(state.records, state.id, request.webhook_url or None)
                outputs.append(res)
                self._stage(state.id, "exporting",
                            f"Pushed {res['rows']} rows in {res['batches']} batch(es)")
            except WebhookError as exc:
                outputs.append({"format": "webhook", "error": str(exc), "rows": 0})
                self._stage(state.id, "exporting", f"Webhook failed: {exc}", "warn")
        return outputs


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _dedupe_urls(results: list[SearchResult], config: Config) -> list[str]:
    patterns = [p.lower() for p in config.get("execution.blocked_domain_patterns", [])]
    seen: set[str] = set()
    out: list[str] = []
    for item in results:
        url = item.url.strip()
        if not url.startswith(("http://", "https://")):
            continue
        low = url.lower()
        if any(p in low for p in patterns):
            continue
        if re.search(r"\.(pdf|zip|exe|png|jpe?g|gif|svg|mp4)(\?|$)", low):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


# Pages that actually publish lead tables: directories, databases, "top N"
# listicles, contact/email lists. Scraped first so records land early and a
# run can stop at max_records before burning the queue on article pages.
_DESK_HINTS = re.compile(
    r"(director|database|list[-_ ]of|/list[-_]|top[- ]?\d|contact[-_ ]?list"
    r"|email[-_ ]?list|companies|startups|start-ups)", re.I)


def rank_search_results(results: list[SearchResult], intent: str) -> list[SearchResult]:
    """Drop off-topic SERP noise and prioritise directory-like pages.

    A result survives when its title/snippet/url mention at least two of the
    intent's content tokens ("History of India" shares only *india* with a
    SaaS-founders intent and is dropped; a "Top SaaS companies in India"
    page shares *saas* + *india* and stays). Seed URLs always survive.
    Survivors are ordered by intent hits, then directory-page hints, then
    original rank — stable, so equal scores keep SERP order.
    """
    tokens = {t for t in re.findall(r"[a-z0-9]{3,}", (intent or "").lower())
              if t not in _STOPWORDS}
    if len(tokens) < 3:
        return list(results)  # too little signal to judge relevance

    def hits(text: str) -> int:
        low = text.lower()
        # crude stem: "startups" also matches "startup", "companies"/"company"
        return sum(1 for t in tokens if t in low or (len(t) >= 5 and t[:5] in low))

    scored: list[tuple[int, int, int, SearchResult]] = []
    for i, item in enumerate(results):
        if item.engine == "seed":
            scored.append((-10_000, 0, i, item))  # user-supplied: always first
            continue
        text = f"{item.title} {item.snippet} {item.url}"
        score = hits(text)
        if score < 2:
            continue
        scored.append((-score, -int(bool(_DESK_HINTS.search(text))), i, item))
    scored.sort(key=lambda row: (row[0], row[1], row[2]))
    return [row[3] for row in scored]


def _summary(state: JobState, queries: list[str], sources: list[str],
             outputs: list[dict[str, Any]], skipped: list[SkipInfo],
             notes: list[str], t0: float, error: str | None = None,
             status: JobStatus | None = None) -> dict[str, Any]:
    final_status = status or (JobStatus.FAILED if error else state.status)
    return {
        "job_id": state.id,
        "status": final_status.value,
        "intent": state.request.intent,
        "queries": queries,
        "records_exported": len(state.records),
        "validity_ratio": None,
        "sources_scraped": sources,
        "pages_scraped": len(sources),
        "skipped": [{"url": s.url, "reason": s.reason} for s in skipped],
        "outputs": outputs,
        "notes": notes,
        "error": error,
        "duration_seconds": round(time.monotonic() - t0, 2),
        "started_at": state.started_at,
        "finished_at": _now(),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
