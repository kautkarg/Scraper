"""Interface wrapper around the local fastCRW engine.

Supports, in order of preference:

* ``cli``     — the local ``crw`` binary (``crw search`` / ``crw <url>``)
* ``server``  — a self-hosted ``crw serve`` API (Firecrawl-compatible /v1/*)
* ``searxng`` — a local SearXNG instance (search only)
* ``http``    — plain local httpx fetch for scraping + seed URLs for discovery

``mode: auto`` probes each available engine and degrades gracefully. Every
blocked request (403/429/…) raises :class:`BlockedError` so the orchestrator
can log a skip and keep harvesting from alternative sources.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

LogFn = Callable[[str, str], None]  # (level, message)


class EngineKind(str, Enum):
    CLI = "cli"
    SERVER = "server"
    SEARXNG = "searxng"
    HTTP = "http"
    NONE = "none"


class DiscoveryError(RuntimeError):
    """No discovery engine could produce results for a query."""


class ScrapeError(RuntimeError):
    """The page could not be fetched or parsed."""


class BlockedError(ScrapeError):
    """Target refused the request — caller should skip the domain."""

    def __init__(self, url: str, status_code: int, reason: str = ""):
        self.url = url
        self.status_code = status_code
        self.reason = reason or f"HTTP {status_code}"
        super().__init__(f"blocked {self.reason}: {url}")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    engine: str = ""
    query: str = ""


@dataclass
class ScrapeResult:
    url: str
    markdown: str
    status_code: int = 200
    engine: str = ""
    elapsed_ms: int = 0
    title: str = ""


@dataclass
class EngineReport:
    """Snapshot used by /api/health and the UI status pills."""

    mode: str = "auto"
    cli: bool = False
    server: bool = False
    searxng: bool = False
    http_fallback: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def search_ready(self) -> bool:
        return self.server or self.cli or self.searxng

    @property
    def scrape_ready(self) -> bool:
        return self.cli or self.server or self.http_fallback


# --------------------------------------------------------------------------
# Minimal HTML -> markdown (fallback path; no external services involved)
# --------------------------------------------------------------------------

_SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "form", "template",
              "nav", "footer", "header", "aside", "button", "select", "textarea"}
_BLOCK_HINTS = ("cookie", "consent", "banner", "advert", "sponsor", "newsletter")


class _MarkdownHTMLParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.skip_depth = 0
        self.list_depth = 0
        self.href: str | None = None
        self.table_rows: list[list[str]] | None = None
        self.current_row: list[str] | None = None
        self.current_cell: list[str] | None = None
        self.title = ""
        self._in_title = False

    # -- helpers ---------------------------------------------------------
    def _emit(self, text: str) -> None:
        if self.skip_depth:
            return
        if self.current_cell is not None:
            self.current_cell.append(text)
        else:
            self.parts.append(text)

    # -- parser callbacks ------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = {k: (v or "") for k, v in attrs}
        cls_id = f"{attrs_d.get('class', '')} {attrs_d.get('id', '')}".lower()
        if tag in _SKIP_TAGS or (tag == "div" and any(h in cls_id for h in _BLOCK_HINTS)):
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._emit("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "p":
            self._emit("\n\n")
        elif tag in {"br"}:
            self._emit("\n")
        elif tag in {"strong", "b"}:
            self._emit("**")
        elif tag in {"em", "i"}:
            self._emit("*")
        elif tag == "code":
            self._emit("`")
        elif tag == "pre":
            self._emit("\n\n```\n")
        elif tag == "li":
            self._emit("\n" + "  " * self.list_depth + "- ")
        elif tag in {"ul", "ol"}:
            self.list_depth += 1
            self._emit("\n")
        elif tag == "a":
            self.href = urljoin(self.base_url, attrs_d.get("href", ""))
            self._emit("[")
        elif tag == "img":
            alt = attrs_d.get("alt", "")
            if alt:
                self._emit(f"![{alt}]")
        elif tag == "table":
            self.table_rows = []
            self._emit("\n\n")
        elif tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"}:
            self.current_cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self._in_title = False
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "pre"}:
            self._emit("\n\n")
        elif tag in {"strong", "b"}:
            self._emit("**")
        elif tag in {"em", "i"}:
            self._emit("*")
        elif tag == "code":
            self._emit("`")
        elif tag in {"ul", "ol"}:
            self.list_depth = max(0, self.list_depth - 1)
            self._emit("\n")
        elif tag == "a":
            href = self.href or ""
            self.href = None
            self._emit(f"]({href})" if href else "]")
        elif tag in {"td", "th"} and self.current_cell is not None and self.current_row is not None:
            self.current_row.append(" ".join("".join(self.current_cell).split()))
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None and self.table_rows is not None:
            if any(cell.strip() for cell in self.current_row):
                self.table_rows.append(self.current_row)
            self.current_row = None
        elif tag == "table":
            if self.table_rows:
                self._emit(self._render_table(self.table_rows))
            else:
                self._emit("\n\n")
            self.table_rows = None

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        self._emit(data)

    @staticmethod
    def _render_table(rows: list[list[str]]) -> str:
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        norm = [list(r) + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(norm[0]) + " |",
                 "|" + "|".join([" --- "] * width) + "|"]
        for row in norm[1:]:
            lines.append("| " + " | ".join(row) + " |")
        return "\n" + "\n".join(lines) + "\n"


def html_to_markdown(raw_html: str, base_url: str) -> tuple[str, str]:
    """Return ``(markdown, title)`` with boilerplate stripped."""
    parser = _MarkdownHTMLParser(base_url)
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:  # malformed HTML must never kill a job
        text = re.sub(r"<[^>]+>", " ", raw_html)
        return re.sub(r"\s+", " ", text), ""
    markdown = html.unescape("".join(parser.parts))
    markdown = re.sub(r"[ \t]+", " ", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip(), parser.title.strip()


def _canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    keep = [(k, v) for k, v in parse_qsl(parts.query)
            if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref"))]
    clean = urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/") or "/",
                        urlencode(keep), ""))
    return clean


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class FastCRWClient:
    def __init__(self, config: Any, log: LogFn | None = None):
        self.config = config
        self.log = log or (lambda level, msg: None)
        self.block_status = set(config.get("execution.block_status_codes", [403, 429]))
        self.block_patterns = [p.lower() for p in
                                config.get("execution.blocked_domain_patterns", [])]
        self.ua = config.get("execution.user_agent", "OmnisearchBot/1.0")
        self.timeout = float(config.get("execution.timeout_seconds", 20))
        self.retries = int(config.get("execution.retry_attempts", 2))
        self.backoff = float(config.get("execution.retry_backoff_seconds", 1.5))
        self.mode = str(config.get("discovery.mode", "auto")).lower()
        self.crw_bin = str(config.get("discovery.crw_bin", "crw"))
        self.server_url = str(config.get("discovery.server_base_url", "")).rstrip("/")
        self.server_api_key = str(config.get("discovery.server_api_key", "") or "")
        self.searxng_url = str(config.get("discovery.searxng_base_url", "")).rstrip("/")
        self.allow_http = bool(config.get("discovery.allow_http_fallback", True))
        self._http = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True,
                                       headers={"User-Agent": self.ua})
        self.report = EngineReport(mode=self.mode)
        self._detected = False

    def _server_auth(self) -> dict[str, str]:
        """Bearer header for `crw serve` when the box requires CRW_AUTH__API_KEYS.

        Scoped strictly to `server_url` requests — never attached to the http
        fallback, so the key cannot leak to third-party sites.
        """
        if self.server_api_key:
            return {"Authorization": f"Bearer {self.server_api_key}"}
        return {}

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- engine detection -------------------------------------------------
    async def detect(self) -> EngineReport:
        if self._detected:
            return self.report
        want = self.mode
        if want in {"auto", "cli"}:
            self.report.cli = shutil.which(self.crw_bin) is not None
            if not self.report.cli and want == "cli":
                self.report.notes.append(f"binary '{self.crw_bin}' not found on PATH")
        if want in {"auto", "server"} and self.server_url:
            try:
                r = await self._http.get(f"{self.server_url}/health",
                                         headers=self._server_auth())
                # require a fastCRW-shaped body so random services on :3000
                # are not mistaken for `crw serve`
                body = r.json() if "json" in r.headers.get("content-type", "") else {}
                self.report.server = (r.status_code == 200
                                      and str(body.get("status", "")).lower() in {"ok", "healthy"})
            except Exception:
                self.report.server = False
            if not self.report.server and want in {"auto", "server"}:
                # note in auto mode too — a dead server_url (env var missing
                # or box unreachable) was previously an invisible failure
                self.report.notes.append(f"crw server unreachable at {self.server_url}")
        if want in {"auto", "searxng"} and self.searxng_url:
            try:
                r = await self._http.get(f"{self.searxng_url}/search",
                                         params={"q": "test", "format": "json"})
                self.report.searxng = r.status_code == 200
            except Exception:
                self.report.searxng = False
            if not self.report.searxng and want == "searxng":
                self.report.notes.append(f"SearXNG unreachable at {self.searxng_url}")
        self.report.http_fallback = bool(self.allow_http)
        if want == "fallback":
            self.report.cli = self.report.server = self.report.searxng = False
        self._detected = True
        return self.report

    # -- search ------------------------------------------------------------
    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        report = await self.detect()
        engines: list[EngineKind] = []
        if report.server:
            engines.append(EngineKind.SERVER)
        if report.cli:
            engines.append(EngineKind.CLI)
        if report.searxng:
            engines.append(EngineKind.SEARXNG)
        if not engines:
            raise DiscoveryError(
                "No search engine reachable (crw binary, crw serve, or SearXNG). "
                "Add seed URLs to the job or start fastCRW: `crw serve` / "
                "`docker compose up -d`."
            )
        last_error: Exception | None = None
        for engine in engines:
            try:
                if engine is EngineKind.SERVER:
                    results = await self._search_server(query, limit)
                elif engine is EngineKind.CLI:
                    results = await self._search_cli(query, limit)
                else:
                    results = await self._search_searxng(query, limit)
                if results:
                    return results
                last_error = DiscoveryError(f"{engine.value} returned 0 results")
            except DiscoveryError as exc:
                last_error = exc
                self.log("warn", f"search engine {engine.value} failed: {exc}")
            except Exception as exc:  # noqa: BLE001 — degrade to next engine
                last_error = exc
                self.log("warn", f"search engine {engine.value} error: {exc}")
        raise DiscoveryError(str(last_error or "all search engines failed"))

    async def _search_server(self, query: str, limit: int) -> list[SearchResult]:
        r = await self._http.post(f"{self.server_url}/v1/search",
                                  json={"query": query, "limit": limit},
                                  headers=self._server_auth())
        if r.status_code >= 400:
            raise DiscoveryError(f"server search HTTP {r.status_code}")
        return self._parse_results(r.json(), EngineKind.SERVER.value, query)

    async def _search_cli(self, query: str, limit: int) -> list[SearchResult]:
        proc = await asyncio.create_subprocess_exec(
            self.crw_bin, "search", query, "--json",
            "--fields", "title,url,snippet", "--limit", str(limit),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), self.timeout * 2)
        except asyncio.TimeoutError:
            proc.kill()
            raise DiscoveryError(f"crw search timed out for query: {query!r}")
        if proc.returncode != 0:
            detail = (stderr or b"").decode(errors="replace").strip()[:300]
            raise DiscoveryError(f"crw search exited {proc.returncode}: {detail}")
        payload = stdout.decode(errors="replace")
        return self._parse_results(_loads_lenient(payload), EngineKind.CLI.value, query)

    async def _search_searxng(self, query: str, limit: int) -> list[SearchResult]:
        r = await self._http.get(f"{self.searxng_url}/search",
                                 params={"q": query, "format": "json"})
        if r.status_code >= 400:
            raise DiscoveryError(f"searxng HTTP {r.status_code}")
        return self._parse_results(r.json(), EngineKind.SEARXNG.value, query)

    def _parse_results(self, payload: Any, engine: str, query: str) -> list[SearchResult]:
        items = payload
        if isinstance(payload, dict):
            items = (payload.get("results") or payload.get("data")
                     or payload.get("organic") or payload.get("pages") or [])
        if isinstance(items, dict):
            items = items.get("results", [])
        out: list[SearchResult] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("link") or item.get("href") or ""
            if not url or not url.startswith(("http://", "https://")):
                continue
            out.append(SearchResult(
                title=str(item.get("title") or item.get("name") or url),
                url=_canonical_url(url),
                snippet=str(item.get("snippet") or item.get("description") or ""),
                engine=engine, query=query,
            ))
        return out

    # -- scrape ------------------------------------------------------------
    async def scrape(self, url: str) -> ScrapeResult:
        report = await self.detect()
        url = _canonical_url(url)
        if self._is_blocked_domain(url):
            raise BlockedError(url, 0, "domain pattern blocked")
        engines: list[EngineKind] = []
        if report.cli:
            engines.append(EngineKind.CLI)
        if report.server:
            engines.append(EngineKind.SERVER)
        if report.http_fallback:
            engines.append(EngineKind.HTTP)
        if not engines:
            raise ScrapeError("no scrape engine available")
        attempts = self.retries + 1
        last_error: Exception | None = None
        for engine in engines:
            for attempt in range(attempts):
                try:
                    if engine is EngineKind.CLI:
                        return await self._scrape_cli(url)
                    if engine is EngineKind.SERVER:
                        return await self._scrape_server(url)
                    return await self._scrape_http(url)
                except BlockedError:
                    raise  # the target refused us — every engine would too
                except ScrapeError as exc:
                    last_error = exc
                    if attempt < attempts - 1:
                        await asyncio.sleep(self.backoff * (attempt + 1))
                    else:
                        self.log("warn", f"engine {engine.value} failed for {url}: {exc} "
                                         f"— trying next engine")
                        break
        raise last_error or ScrapeError(f"failed to scrape {url}")

    def _is_blocked_domain(self, url: str) -> bool:
        low = url.lower()
        return any(pat in low for pat in self.block_patterns)

    async def _scrape_cli(self, url: str) -> ScrapeResult:
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            self.crw_bin, url, "-f", "markdown", "--stealth",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), self.timeout * 3)
        except asyncio.TimeoutError:
            proc.kill()
            raise ScrapeError(f"crw scrape timed out: {url}")
        if proc.returncode != 0:
            detail = (stderr or b"").decode(errors="replace")
            status = _extract_status(detail)
            if status in self.block_status:
                raise BlockedError(url, status, detail.strip()[:160])
            raise ScrapeError(f"crw exited {proc.returncode}: {detail.strip()[:200]}")
        text = stdout.decode(errors="replace")
        if not text.strip():
            raise ScrapeError(f"empty markdown from crw: {url}")
        return ScrapeResult(url=url, markdown=text.strip(), engine=EngineKind.CLI.value,
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                            title=_first_heading(text))

    async def _scrape_server(self, url: str) -> ScrapeResult:
        started = time.monotonic()
        r = await self._http.post(f"{self.server_url}/v1/scrape",
                                  json={"url": url, "formats": ["markdown"]},
                                  headers=self._server_auth())
        if r.status_code in self.block_status:
            raise BlockedError(url, r.status_code)
        if r.status_code >= 400:
            raise ScrapeError(f"server scrape HTTP {r.status_code} for {url}")
        body = r.json()
        data = body.get("data") if isinstance(body, dict) else None
        markdown = ""
        if isinstance(data, dict):
            markdown = data.get("markdown") or data.get("content") or ""
        elif isinstance(body, dict):
            markdown = body.get("markdown") or ""
        if not markdown:
            if isinstance(body, dict) and body.get("success") is False:
                raise ScrapeError(str(body.get("error", "scrape failed"))[:200])
            raise ScrapeError(f"no markdown in response for {url}")
        return ScrapeResult(url=url, markdown=markdown, engine=EngineKind.SERVER.value,
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                            title=_first_heading(markdown))

    async def _scrape_http(self, url: str) -> ScrapeResult:
        started = time.monotonic()
        r = await self._http.get(url)
        if r.status_code in self.block_status:
            raise BlockedError(url, r.status_code)
        if r.status_code >= 400:
            raise ScrapeError(f"HTTP {r.status_code} for {url}")
        content_type = r.headers.get("content-type", "")
        if "html" in content_type or "<" in r.text[:512]:
            markdown, title = html_to_markdown(r.text, str(r.url))
        else:
            markdown, title = r.text.strip(), ""
        if not markdown:
            raise ScrapeError(f"no extractable content: {url}")
        return ScrapeResult(url=url, markdown=markdown, status_code=r.status_code,
                            engine=EngineKind.HTTP.value,
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                            title=title or _first_heading(markdown))

    # -- health -------------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        report = await self.detect()
        return {
            "mode": report.mode,
            "engines": {"cli": report.cli, "server": report.server,
                        "searxng": report.searxng, "http_fallback": report.http_fallback},
            "search_ready": report.search_ready,
            "scrape_ready": report.scrape_ready,
            "notes": report.notes,
        }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _loads_lenient(payload: str) -> Any:
    text = payload.strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # NDJSON fallback
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if rows:
        return rows
    # last resort: pull URLs out of free text
    return [{"url": u} for u in re.findall(r"https?://[^\s\"'<>]+", text)]


def _extract_status(text: str) -> int:
    match = re.search(r"\b(40[0-9]|429|50[0-3]|999)\b", text)
    return int(match.group(1)) if match else 0


def _first_heading(markdown: str) -> str:
    match = re.search(r"^#{1,3}\s+(.+)$", markdown, re.MULTILINE)
    if match:
        return match.group(1).strip()
    first = next((ln.strip() for ln in markdown.splitlines() if ln.strip()), "")
    return first[:120]
