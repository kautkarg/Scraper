"""End-to-end API verification: fixture server + uvicorn + job + SSE + download."""

import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path("/home/gaurav/Documents/Data Scraper/omnisearch-engine")
PY = str(ROOT / ".venv" / "bin" / "python")


def hermetic_env() -> dict:
    """Fixture-only config: live discovery engines are disabled so this
    script stays deterministic even when crw/SearXNG are installed."""
    import os
    import yaml
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    cfg["discovery"]["mode"] = "fallback"
    cfg["llm"]["enabled"] = False  # fixture tests stay LLM-free
    path = Path("/tmp/opencode/config-hermetic.yaml")
    path.write_text(yaml.safe_dump(cfg))
    return dict(os.environ, OMNISEARCH_CONFIG=str(path))

GOOD = """<!DOCTYPE html><html><head><title>Dir</title></head><body>
<nav>HOME LOGIN</nav><h1>India B2B SaaS Directory</h1>
<table>
<tr><th>Company Name</th><th>email</th><th>website</th><th>location</th></tr>
<tr><td>Zeta Labs</td><td>founder@zetalabs.io</td><td>https://zetalabs.io</td><td>Bangalore</td></tr>
<tr><td>Nimbus HQ</td><td>hello@nimbushq.com</td><td>https://nimbushq.com</td><td>Pune</td></tr>
<tr><td>Orbit CRM</td><td>team@orbitcrm.in</td><td>https://orbitcrm.in</td><td>Gurgaon</td></tr>
</table><footer>copyright 2026</footer></body></html>"""

KV = """<html><body><article><h1>Falcon Metrics</h1>
<p><strong>Email:</strong> ceo@falconmetrics.dev</p>
<p><strong>Location:</strong> Mumbai</p></article></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/blocked"):
            self.send_response(403)
            self.end_headers()
            return
        body = GOOD if "directory" in self.path else KV
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def main() -> int:
    # 1. fixture site
    fx = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=fx.serve_forever, daemon=True).start()
    fx_port = fx.server_address[1]
    fx_base = f"http://127.0.0.1:{fx_port}"

    # 2. FastAPI app under uvicorn as a managed subprocess
    port = free_port()
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=hermetic_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    ok = True
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            # wait for boot
            for _ in range(60):
                try:
                    r = await c.get(f"{base}/api/health")
                    if r.status_code == 200:
                        break
                except Exception:
                    await asyncio.sleep(0.25)
            else:
                print("FAIL: server never became healthy")
                return 1

            health = await c.get(f"{base}/api/health")
            h = health.json()
            print("HEALTH:", json.dumps(h["engine"]), "llm:", h["llm"])
            assert h["engine"]["scrape_ready"], "no scrape engine"

            # UI served
            ui = await c.get(f"{base}/")
            assert ui.status_code == 200 and "Omnisearch" in ui.text, "UI not served"
            print("UI: served OK (glassmorphic index.html)")

            # 3. submit job
            job = {
                "intent": "Find B2B SaaS startups in India with founder emails",
                "fields": [
                    {"name": "company_name", "type": "string", "required": True},
                    {"name": "email", "type": "email", "required": True},
                    {"name": "website", "type": "url"},
                    {"name": "location", "type": "string"},
                ],
                "destination": "csv",
                "max_records": 10,
                "seed_urls": [f"{fx_base}/directory.html", f"{fx_base}/about.html",
                              f"{fx_base}/blocked-page.html"],
            }
            resp = await c.post(f"{base}/api/jobs", json=job)
            assert resp.status_code == 202, resp.text
            job_id = resp.json()["job_id"]
            print(f"JOB: {job_id} accepted")

            # 4. consume SSE
            stages, records, summary, errors = [], [], None, []
            async with c.stream("GET", f"{base}/api/jobs/{job_id}/events") as stream:
                current_event = None
                async for line in stream.aiter_lines():
                    if line.startswith("event: "):
                        current_event = line[7:].strip()
                    elif line.startswith("data: "):
                        payload = json.loads(line[6:])
                        if current_event == "stage":
                            stages.append(payload)
                            print(f"  [{payload.get('stage')}] {payload.get('level')}: "
                                  f"{payload.get('message')[:110]}")
                        elif current_event == "record":
                            records.append(payload["values"])
                        elif current_event == "summary":
                            summary = payload["summary"]
                        elif current_event == "error":
                            errors.append(payload.get("message"))
                    if summary is not None:
                        break

            print(f"\nSSE: {len(stages)} stage events, {len(records)} record events")
            assert summary, "no summary event"
            print("SUMMARY:",
                  json.dumps({k: summary[k] for k in
                              ("status", "records_exported", "validity_ratio",
                               "pages_scraped", "duration_seconds")}, indent=2))
            assert summary["status"] == "completed", summary.get("error")
            assert summary["records_exported"] >= 4, summary
            assert summary["validity_ratio"] >= 0.9, summary
            skipped = summary.get("skipped", [])
            assert any("blocked" in s["url"] for s in skipped), skipped
            print(f"RESILIENCE: {len(skipped)} skip(s) logged, run continued ->",
                  [s["url"].rsplit('/', 1)[-1] for s in skipped])

            # grid data endpoint
            recs = await c.get(f"{base}/api/jobs/{job_id}/records")
            body = recs.json()
            assert len(body["records"]) == summary["records_exported"]
            print("RECORDS endpoint:", body["headers"])

            # download produced CSV
            csv_resp = await c.get(f"{base}/api/jobs/{job_id}/download",
                                   params={"format": "csv"})
            assert csv_resp.status_code == 200, csv_resp.text
            csv_text = csv_resp.content.decode()
            print("CSV download:", csv_text.splitlines()[0])
            print("CSV rows:", len(csv_text.strip().splitlines()) - 1)
            assert "founder@zetalabs.io" in csv_text

            # validation errors are surfaced
            bad = await c.post(f"{base}/api/jobs",
                               json={"intent": "x", "fields": []})
            assert bad.status_code == 422, bad.status_code
            print("VALIDATION: empty schema rejected with 422")

            print("\nALL E2E CHECKS PASSED")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        fx.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
