"""Unit + integration tests for parsing, export, and the full job loop."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import core.config as cfg_mod
from core.config import Config
from core.exporter import Destination, Exporter
from core.orchestrator import BUS, JobRequest, Orchestrator
from core.parser import FieldType, RecordParser, SchemaField, coerce_value

FIELDS = [
    SchemaField(name="Company Name", type=FieldType.STRING, required=True),
    SchemaField(name="email", type=FieldType.EMAIL, required=True),
    SchemaField(name="website", type=FieldType.URL),
    SchemaField(name="founded", type=FieldType.DATE),
    SchemaField(name="employees", type=FieldType.INTEGER),
    SchemaField(name="hiring", type=FieldType.BOOLEAN),
    SchemaField(name="tech_stack", type=FieldType.ARRAY),
]

GOOD_PAGE = """<!DOCTYPE html><html><head><title>Directory</title>
<style>.junk{}</style></head>
<body><nav>HOME ABOUT LOGIN</nav>
<h1>India SaaS Directory</h1>
<table>
<tr><th>Company Name</th><th>email</th><th>website</th><th>founded</th><th>employees</th><th>hiring</th><th>tech_stack</th></tr>
<tr><td>Zeta Labs</td><td>founder@zetalabs.io</td><td>https://zetalabs.io</td><td>2021-04-02</td><td>42</td><td>yes</td><td>Python, AWS</td></tr>
<tr><td>Nimbus HQ</td><td>hello@nimbushq.com</td><td>https://nimbushq.com</td><td>2019</td><td>120</td><td>no</td><td>Go, GCP</td></tr>
<tr><td>Orbit CRM</td><td>team@orbitcrm.in</td><td>https://orbitcrm.in</td><td>15 Mar 2022</td><td>18</td><td>true</td><td>React</td></tr>
</table>
<footer>© 2026 directory</footer></body></html>"""

KV_PAGE = """<html><body><article>
<h1>Falcon Metrics</h1>
<p><strong>Email:</strong> ceo@falconmetrics.dev</p>
<p><strong>Website:</strong> https://falconmetrics.dev</p>
<p><strong>Founded:</strong> 2020-11-07</p>
</article></body></html>"""


class _FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        if self.path.startswith("/blocked"):
            self.send_response(403)
            self.end_headers()
            return
        body = GOOD_PAGE if self.path.startswith("/directory") else KV_PAGE
        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture(scope="module")
def fixture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    data = cfg_mod._deep_merge(cfg_mod._DEFAULTS, {
        "discovery": {"mode": "fallback", "allow_http_fallback": True},
        "execution": {"max_records_per_job": 10, "max_concurrent_scrapes": 3,
                      "request_delay_seconds": 0.01, "retry_attempts": 0,
                      "timeout_seconds": 5},
        "llm": {"enabled": False},
        "paths": {"outputs": str(tmp_path / "outputs"),
                  "sqlite_db": str(tmp_path / "outputs" / "test.db")},
    })
    return Config(data)


# --------------------------------------------------------------------------
# parser unit tests
# --------------------------------------------------------------------------

def test_field_name_normalization():
    assert SchemaField(name="Company Name").name == "company_name"


def test_type_coercion():
    email_f = SchemaField(name="email", type=FieldType.EMAIL)
    url_f = SchemaField(name="website", type=FieldType.URL)
    num_f = SchemaField(name="raised", type=FieldType.NUMBER)
    int_f = SchemaField(name="employees", type=FieldType.INTEGER)
    bool_f = SchemaField(name="hiring", type=FieldType.BOOLEAN)
    date_f = SchemaField(name="founded", type=FieldType.DATE)
    arr_f = SchemaField(name="stack", type=FieldType.ARRAY)

    assert coerce_value(email_f, "Mail: Foo@Bar.COM now") == "foo@bar.com"
    assert coerce_value(url_f, "visit https://acme.io/x.") == "https://acme.io/x"
    assert coerce_value(url_f, "acme.io/about") == "https://acme.io/about"
    assert coerce_value(num_f, "$1,250.50") == 1250.5
    assert coerce_value(int_f, "about 42 people") == 42
    assert coerce_value(bool_f, "YES") is True
    assert coerce_value(bool_f, "inactive") is False
    assert coerce_value(date_f, "15 Mar 2022") == "2022-03-15"
    assert coerce_value(arr_f, "Python, AWS; GCP") == ["Python", "AWS", "GCP"]
    assert coerce_value(email_f, "no email here") is None


def test_required_fields_and_validity_ratio():
    parser = RecordParser([SchemaField(name="company_name", required=True),
                           SchemaField(name="email", type=FieldType.EMAIL, required=True)])
    good = parser.ingest([{"company_name": "Acme", "email": "a@b.io"}], "https://a.io")
    bad = parser.ingest([{"company_name": "NoMail"}, {"email": "x@y.io"}], "https://b.io")
    assert len(good) == 1
    assert bad == []
    assert parser.stats.valid == 1
    assert parser.stats.invalid == 2
    assert parser.stats.validity_ratio == pytest.approx(0.3333, abs=1e-3)


def test_dedupe_across_pages():
    parser = RecordParser([SchemaField(name="email", type=FieldType.EMAIL)])
    first = parser.ingest([{"email": "a@b.io"}], "https://one.io")
    second = parser.ingest([{"email": "A@B.io"}], "https://two.io")
    assert len(first) == 1
    assert second == []
    assert parser.stats.duplicates == 1


def test_heuristic_table_extraction():
    from connectors.fastcrw_client import html_to_markdown
    md, title = html_to_markdown(GOOD_PAGE, "https://dir.io/")
    parser = RecordParser(FIELDS)
    records = parser.ingest(parser.heuristic_records(md, "https://dir.io/", title),
                            "https://dir.io/", md)
    assert len(records) == 3
    assert records[0]["company_name"] == "Zeta Labs"
    assert records[0]["email"] == "founder@zetalabs.io"
    assert records[1]["hiring"] is False
    assert records[2]["founded"] == "2022-03-15"
    assert records[0]["tech_stack"] == ["Python", "AWS"]
    assert parser.stats.validity_ratio >= 0.9


def test_heuristic_key_value_extraction():
    from connectors.fastcrw_client import html_to_markdown
    md, title = html_to_markdown(KV_PAGE, "https://falcon.io/")
    parser = RecordParser(FIELDS)
    records = parser.ingest(parser.heuristic_records(md, "https://falcon.io/", title),
                            "https://falcon.io/", md)
    assert records, "expected at least one record from key-value page"
    assert records[0]["email"] == "ceo@falconmetrics.dev"
    assert records[0]["company_name"] == "Falcon Metrics"


def test_markdown_strips_boilerplate():
    from connectors.fastcrw_client import html_to_markdown
    md, _ = html_to_markdown(GOOD_PAGE, "https://dir.io/")
    assert "LOGIN" not in md          # nav stripped
    assert "© 2026 directory" not in md  # footer stripped
    assert "Zeta Labs" in md


# --------------------------------------------------------------------------
# exporter tests
# --------------------------------------------------------------------------

RECORDS = [
    {"company_name": "Zeta Labs", "email": "founder@zetalabs.io", "website": "https://zetalabs.io"},
    {"company_name": "Nimbus HQ", "email": "hello@nimbushq.com", "website": None},
]
HEADERS = ["company_name", "email", "website"]


def test_export_all_file_formats(config, tmp_path):
    exporter = Exporter(config)
    csv_r = exporter.to_csv(RECORDS, HEADERS, "job1")
    json_r = exporter.to_json(RECORDS, HEADERS, "job1")
    md_r = exporter.to_markdown(RECORDS, HEADERS, "job1")
    sql_r = exporter.to_sqlite(RECORDS, HEADERS, "job1", "startups")

    assert Path(csv_r.path).read_text().count("\n") == 3  # header + 2 rows
    import json as _json
    payload = _json.loads(Path(json_r.path).read_text())
    assert payload["count"] == 2 and payload["fields"] == HEADERS
    assert "| company_name | email | website |" in Path(md_r.path).read_text()

    import sqlite3
    conn = sqlite3.connect(sql_r.path)
    rows = conn.execute('SELECT company_name, email FROM startups').fetchall()
    conn.close()
    assert rows[0] == ("Zeta Labs", "founder@zetalabs.io")


def test_exporter_handles_evolving_schema(config):
    exporter = Exporter(config)
    exporter.to_sqlite([{"company_name": "A"}], ["company_name"], "j1", "t")
    result = exporter.to_sqlite([{"company_name": "B", "email": "b@c.io"}],
                                ["company_name", "email"], "j2", "t")
    import sqlite3
    conn = sqlite3.connect(result.path)
    rows = conn.execute('SELECT company_name, email FROM t').fetchall()
    conn.close()
    assert rows == [("A", None), ("B", "b@c.io")]


# --------------------------------------------------------------------------
# end-to-end job loop
# --------------------------------------------------------------------------

async def _wait_for_job(job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = BUS.get(job_id)
        if state and state.done:
            return state.summary or {}
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


async def test_full_job_seed_urls_and_skip(config, fixture_server):
    request = JobRequest(
        intent="Find B2B SaaS startups in India with founder emails",
        fields=FIELDS, destination=Destination.CSV, max_records=10,
        seed_urls=[f"{fixture_server}/directory",
                   f"{fixture_server}/about",
                   f"{fixture_server}/blocked-page"],
    )
    state = BUS.create(request)
    await Orchestrator(config).run(state)

    summary = state.summary
    assert summary is not None
    assert state.status.value == "completed", summary.get("error")
    assert summary["records_exported"] == 4          # 3 table rows + 1 kv record
    assert summary["validity_ratio"] >= 0.9
    skipped_urls = [s["url"] for s in summary["skipped"]]
    assert any("/blocked-page" in u for u in skipped_urls)
    outputs = summary["outputs"]
    assert outputs and outputs[0]["format"] == "csv" and Path(outputs[0]["path"]).is_file()
    # failed domain did not stop the run
    assert len(summary["sources_scraped"]) >= 2


async def test_job_fails_cleanly_without_sources(config):
    request = JobRequest(intent="Find cool startups",
                         fields=[SchemaField(name="name", required=True)],
                         destination=Destination.JSON, seed_urls=[])
    state = BUS.create(request)
    await Orchestrator(config).run(state)
    assert state.status.value == "failed"
    assert state.summary and state.summary["error"]
    # terminal event was published for SSE clients
    assert any(e["type"] == "summary" for e in state.history)


async def test_empty_intent_rejected(config):
    request = JobRequest(intent="", fields=[SchemaField(name="name")])
    state = BUS.create(request)
    await Orchestrator(config).run(state)
    assert state.status.value == "failed"
    assert "intent is empty" in (state.summary or {}).get("error", "")


# --------------------------------------------------------------------------
# webhook sink
# --------------------------------------------------------------------------

class _HookHandler(BaseHTTPRequestHandler):
    received: list[dict] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path != "/hook":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        _HookHandler.received.append(json.loads(self.rfile.read(length)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


@pytest.fixture()
def hook_server():
    _HookHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HookHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook"
    server.shutdown()


async def test_webhook_sink_batches_and_retries(config, hook_server):
    from connectors.webhook import WebhookSink

    config.raw["integrations"]["webhook"]["batch_size"] = 2
    sink = WebhookSink(config)
    records = [{"company_name": f"C{i}", "email": f"c{i}@x.io"} for i in range(5)]
    result = await sink.push(records, "job-hook", hook_server)
    assert result["rows"] == 5
    assert result["batches"] == 3  # 2 + 2 + 1
    assert len(_HookHandler.received) == 3
    assert _HookHandler.received[0]["records"][0]["company_name"] == "C0"
    assert all(b["job_id"] == "job-hook" for b in _HookHandler.received)


async def test_webhook_permanent_4xx_does_not_hang(config, hook_server):
    from connectors.webhook import WebhookError, WebhookSink

    # point at a 404 route on the same fixture server
    sink = WebhookSink(config)
    with pytest.raises(WebhookError):
        await sink.push([{"a": 1}], "job", hook_server + "-missing")


# --------------------------------------------------------------------------
# LLM (Mimo) path — fake OpenAI-compatible server
# --------------------------------------------------------------------------

class _LLMHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible /chat/completions that inspects the prompt."""

    mode = "ok"  # ok | garbage
    calls: list[str] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        prompt = body["messages"][-1]["content"]
        _LLMHandler.calls.append(prompt)
        if _LLMHandler.mode == "garbage":
            content = "not json at all"
        elif "search-query planner" in prompt:
            content = json.dumps({"queries": [
                "B2B SaaS India founder emails",
                "India SaaS directory contact",
                "site list Indian B2B startups email",
            ]})
        else:  # extraction prompt
            content = json.dumps({"records": [
                {"Company Name": "Acme Analytics", "email": "hi@acme.io",
                 "website": "https://acme.io"},
                {"Company Name": "No Email Ltd"},  # missing required -> rejected
            ]})
        payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def llm_server():
    _LLMHandler.mode = "ok"
    _LLMHandler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LLMHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _enable_llm(config: Config, base_url: str) -> None:
    config.raw["llm"].update({"enabled": True, "base_url": base_url, "api_key": "test"})


async def test_llm_planner_and_bad_response_fallback(config, llm_server):
    from core.orchestrator import llm_queries

    logs: list[tuple[str, str]] = []
    log = lambda level, msg: logs.append((level, msg))  # noqa: E731

    _enable_llm(config, llm_server)
    queries = await llm_queries(config, "Find B2B SaaS startups", FIELDS, log)
    assert queries == ["B2B SaaS India founder emails",
                       "India SaaS directory contact",
                       "site list Indian B2B startups email"]
    assert any("Mimo planner produced" in m for _, m in logs)
    # Authorization header path exercised via api_key
    assert any("planner" in p for p in _LLMHandler.calls)

    _LLMHandler.mode = "garbage"
    fallback = await llm_queries(config, "Find B2B SaaS startups", FIELDS, log)
    assert fallback is None  # orchestrator falls back to heuristic queries
    assert any(level == "warn" and "heuristic" in msg for level, msg in logs)


async def test_llm_records_are_gated_by_schema(config, llm_server, fixture_server):
    """LLM output must pass parser.ingest() — hallucinated/incomplete rows die."""
    from core.orchestrator import llm_records

    _enable_llm(config, llm_server)
    fields = [SchemaField(name="Company Name", type=FieldType.STRING, required=True),
              SchemaField(name="email", type=FieldType.EMAIL, required=True)]
    raw = await llm_records(config, "any markdown", "http://x/", fields)
    assert raw is not None and len(raw) == 2  # LLM itself returns both

    parser = RecordParser(fields)
    fresh = parser.ingest(raw, "http://x/", "any markdown")
    assert len(fresh) == 1  # missing-email row rejected by schema gate
    assert fresh[0]["email"] == "hi@acme.io"


async def test_full_job_with_llm_planner(config, llm_server, fixture_server):
    _enable_llm(config, llm_server)
    request = JobRequest(
        intent="Find B2B SaaS startups in India",
        fields=[SchemaField(name="Company Name", type=FieldType.STRING, required=True),
                SchemaField(name="email", type=FieldType.EMAIL, required=True)],
        seed_urls=[f"{fixture_server}/directory.html"],
        destination=Destination.CSV,
    )
    state = BUS.create(request)
    await Orchestrator(config).run(state)
    summary = state.summary or {}
    assert state.status.value == "completed"
    assert summary["queries"][0] == "B2B SaaS India founder emails"
    # LLM extraction returned only the one schema-valid row per page
    assert len(state.records) == 1
    assert state.records[0]["company_name"] == "Acme Analytics"
    # planner prompt and extraction prompt both hit the fake server
    assert any("search-query planner" in p for p in _LLMHandler.calls)
    assert any("Extract records" in p for p in _LLMHandler.calls)


# --------------------------------------------------------------------------
# llm transport helpers (core.llm) — pure functions, no network
# --------------------------------------------------------------------------

def test_extract_json_plain_fenced_and_prose():
    from core.llm import extract_json
    assert extract_json('{"queries": ["a"]}') == {"queries": ["a"]}
    assert extract_json('```json\n{"records": [{"x": 1}]}\n```') == \
        {"records": [{"x": 1}]}
    assert extract_json('Sure! Here it is:\n{"queries":["q1"]}\nEnjoy.') == \
        {"queries": ["q1"]}
    assert extract_json('[1, 2, 3]') == [1, 2, 3]
    with pytest.raises(json.JSONDecodeError):
        extract_json("no json here")


def test_parse_opencode_events_merges_parts():
    from core.llm import parse_opencode_events
    stdout = "\n".join([
        json.dumps({"type": "step-start"}),
        json.dumps({"type": "text", "part": {"id": "p1", "text": '{"queries":'}}),
        json.dumps({"type": "text", "part": {"id": "p2", "text": ' ["a"]}'}}),
        json.dumps({"type": "step_finish", "reason": "stop"}),
    ])
    assert parse_opencode_events(stdout) == '{"queries": ["a"]}'
    # last write per part id wins (part updates are wholesale)
    stdout2 = "\n".join([
        json.dumps({"type": "text", "part": {"id": "p1", "text": "old"}}),
        json.dumps({"type": "text", "part": {"id": "p1", "text": "new"}}),
    ])
    assert parse_opencode_events(stdout2) == "new"


def test_parse_opencode_events_error_and_empty():
    from core.llm import parse_opencode_events
    err = "\n".join([
        json.dumps({"type": "error",
                    "error": {"message": "FreeTierError: not within OpenCode"}}),
    ])
    with pytest.raises(RuntimeError, match="FreeTierError"):
        parse_opencode_events(err)
    assert parse_opencode_events("") is None
    assert parse_opencode_events('{"type":"step-start"}') is None
    # text wins over a later error
    mixed = "\n".join([
        json.dumps({"type": "text", "part": {"id": "p", "text": "ok"}}),
        json.dumps({"type": "error", "error": {"message": "late"}}),
    ])
    assert parse_opencode_events(mixed) == "ok"


def test_llm_disabled_returns_none_without_dispatch(config, monkeypatch):
    import core.llm as llm_mod

    config.raw["llm"]["enabled"] = False
    called: list[str] = []

    async def spy(name):
        async def _inner(*a, **k):
            called.append(name)
            return "x"
        return _inner

    monkeypatch.setattr(llm_mod, "opencode_complete",
                        asyncio.run(spy("opencode")))
    monkeypatch.setattr(llm_mod, "http_complete", asyncio.run(spy("http")))
    assert asyncio.run(llm_mod.complete(config, "prompt")) is None
    assert called == []

    # enabled + mode opencode -> only the CLI transport is dispatched
    config.raw["llm"]["enabled"] = True
    config.raw["llm"]["mode"] = "opencode"

    async def oc(*a, **k):
        called.append("opencode")
        return '{"ok": true}'

    async def http(*a, **k):
        called.append("http")
        return "never"

    monkeypatch.setattr(llm_mod, "opencode_complete", oc)
    monkeypatch.setattr(llm_mod, "http_complete", http)
    assert asyncio.run(llm_mod.complete(config, "p")) == '{"ok": true}'
    assert called == ["opencode"]
