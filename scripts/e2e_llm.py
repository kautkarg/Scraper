"""Live LLM verification: OpenCode Zen free tier (MiMo) through the CLI.

Runs the two production LLM entry points against the real config:
  1. llm_queries  — search-query planner (must return 3-5 queries)
  2. llm_records  — markdown -> JSON extraction (schema-shaped records)

No server, no fixtures, no mocks — each call spawns `opencode run`.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path

ROOT = Path("/home/gaurav/Documents/Data Scraper/omnisearch-engine")
sys.path.insert(0, str(ROOT))

from core.config import load_config            # noqa: E402
from core.orchestrator import llm_queries, llm_records  # noqa: E402
from core.parser import FieldType, SchemaField          # noqa: E402

FIELDS = [
    SchemaField(name="company_name", type=FieldType.STRING, required=True),
    SchemaField(name="email", type=FieldType.STRING, required=False),
    SchemaField(name="website", type=FieldType.STRING, required=False),
    SchemaField(name="location", type=FieldType.STRING, required=False),
]

MARKDOWN = """# Top SaaS Startups

## Zoho Corporation
Chennai, India. Website: https://www.zoho.com
Contact: info@zoho.com

## Freshworks Inc.
San Mateo / Chennai. Website: https://www.freshworks.com
Email: hello@freshworks.com
"""


async def main() -> int:
    config = load_config(ROOT / "config.yaml")
    ok = True

    print("CONFIG:",
          "enabled=", config.get("llm.enabled"),
          "mode=", config.get("llm.mode"),
          "model=", config.get("llm.model"))
    assert config.get("llm.enabled"), "llm.enabled must be true in config.yaml"
    assert config.get("llm.mode") == "opencode", "expected opencode mode"
    assert shutil.which(config.get("llm.opencode_bin", "opencode")), \
        "opencode binary not on PATH"
    print("✓ config: opencode mode, binary on PATH")

    logs: list[tuple[str, str]] = []

    def log(level: str, msg: str) -> None:
        logs.append((level, msg))
        print(f"  [{level}] {msg}")

    # 1. planner ----------------------------------------------------------
    t0 = time.monotonic()
    queries = await llm_queries(
        config, "Find B2B SaaS startups in India with founder emails",
        FIELDS, log)
    dt = time.monotonic() - t0
    print(f"QUERIES ({dt:.1f}s):", queries)
    assert queries and len(queries) >= 3, f"planner returned {queries}"
    assert len(queries) <= 5
    assert all(isinstance(q, str) and q.strip() for q in queries)
    assert not any(level == "warn" for level, _ in logs), \
        f"planner fell back to heuristics: {logs}"
    print(f"✓ llm_queries: {len(queries)} queries from live MiMo")

    # 2. record extraction ------------------------------------------------
    t0 = time.monotonic()
    records = await llm_records(config, MARKDOWN, "https://example.com/list",
                                FIELDS)
    dt = time.monotonic() - t0
    print(f"RECORDS ({dt:.1f}s):", records)
    assert records, "extractor returned no records"
    names = [str(r.get("company_name", "")) for r in records]
    assert any("Zoho" in n for n in names), f"missing Zoho in {names}"
    assert any("Freshworks" in n for n in names), f"missing Freshworks in {names}"
    print(f"✓ llm_records: {len(records)} schema-shaped records from live MiMo")

    # 3. full pipeline job (in-process orchestrator, fixture site, live LLM)
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from core.exporter import Destination
    from core.orchestrator import BUS, JobRequest, Orchestrator

    page = """<!DOCTYPE html><html><head><title>Directory</title></head><body>
    <h1>India SaaS Directory</h1>
    <table>
    <tr><th>Company</th><th>Email</th><th>Website</th><th>City</th></tr>
    <tr><td>Zoho Corporation</td><td>info@zoho.com</td><td>https://www.zoho.com</td><td>Chennai</td></tr>
    <tr><td>Freshworks Inc</td><td>hello@freshworks.com</td><td>https://www.freshworks.com</td><td>Chennai</td></tr>
    </table></body></html>"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    site = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    site_url = f"http://127.0.0.1:{site.server_address[1]}/directory.html"

    # keep the job offline + bounded; only the LLM is live
    config.raw["discovery"]["mode"] = "fallback"
    config.raw["execution"]["max_records_per_job"] = 10
    config.raw["execution"]["timeout_seconds"] = 240
    config.raw["execution"]["request_delay_seconds"] = 0.01

    request = JobRequest(intent="Find B2B SaaS startups in India with founder emails",
                         fields=FIELDS, destination=Destination.CSV,
                         seed_urls=[site_url])
    state = BUS.create(request)
    t0 = time.monotonic()
    await Orchestrator(config).run(state)
    dt = time.monotonic() - t0
    summary = state.summary or {}
    print(f"JOB ({dt:.1f}s): status={state.status.value} "
          f"exported={summary.get('records_exported')} "
          f"validity={summary.get('validity_ratio')} "
          f"queries={summary.get('queries')}")
    site.shutdown()
    assert state.status.value == "completed", summary.get("error")
    assert summary.get("records_exported", 0) >= 1, summary
    assert summary.get("validity_ratio", 0) >= 0.8, summary
    assert state.records, "no records captured"
    print(f"✓ full job: {len(state.records)} records, "
          f"{summary.get('queries') and len(summary['queries'])} LLM queries")

    print("\nLIVE LLM E2E: PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except AssertionError as exc:
        print(f"\nLIVE LLM E2E: FAILED — {exc}")
        raise SystemExit(1)
