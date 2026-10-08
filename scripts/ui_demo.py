"""Browser demo + QA pass for Omnisearch UI."""
import asyncio, json, re, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path("/home/gaurav/Documents/Data Scraper/omnisearch-engine")
SHOTS = ROOT / "outputs" / "ui-demo"
SHOTS.mkdir(parents=True, exist_ok=True)
API = "http://127.0.0.1:8199"

GOOD_PAGE = """<!DOCTYPE html><html><head><title>Directory</title></head><body>
<nav>HOME ABOUT LOGIN</nav><h1>India SaaS Directory</h1>
<table>
<tr><th>Company Name</th><th>email</th><th>website</th><th>founded</th><th>employees</th></tr>
<tr><td>Zeta Labs</td><td>founder@zetalabs.io</td><td>https://zetalabs.io</td><td>2021-04-02</td><td>42</td></tr>
<tr><td>Nimbus HQ</td><td>hello@nimbushq.com</td><td>https://nimbushq.com</td><td>2019</td><td>120</td></tr>
<tr><td>Orbit CRM</td><td>team@orbitcrm.in</td><td>https://orbitcrm.in</td><td>2022-03-15</td><td>18</td></tr>
</table><footer>© 2026 directory</footer></body></html>"""

KV_PAGE = """<html><body><article><h1>Falcon Metrics</h1>
<p><strong>Email:</strong> ceo@falconmetrics.dev</p>
<p><strong>Website:</strong> https://falconmetrics.dev</p>
<p><strong>Founded:</strong> 2020-11-07</p></article></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/blocked"):
            self.send_response(403); self.end_headers(); return
        body = GOOD_PAGE if "directory" in self.path else KV_PAGE
        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers(); self.wfile.write(payload)


def wait_health(timeout=30):
    import urllib.request
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(API + "/api/health", timeout=2); return True
        except Exception:
            time.sleep(0.3)
    return False


def hermetic_env() -> dict:
    """Fixture-only config — keeps the demo deterministic with live engines installed."""
    import os
    import yaml
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    cfg["discovery"]["mode"] = "fallback"
    cfg["llm"]["enabled"] = False  # fixture tests stay LLM-free
    path = Path("/tmp/opencode/config-hermetic-ui.yaml")
    path.write_text(yaml.safe_dump(cfg))
    return dict(os.environ, OMNISEARCH_CONFIG=str(path))


def main():
    fx = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=fx.serve_forever, daemon=True).start()
    fx_url = f"http://127.0.0.1:{fx.server_address[1]}"

    proc = subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", "8199", "--log-level", "warning"],
        cwd=ROOT, env=hermetic_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_health(), "server did not come up"
        from playwright.sync_api import sync_playwright

        console_errors, failed_reqs = [], []
        report = {}

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            page.on("console", lambda m: console_errors.append(
                f"{m.text} @ {m.location.get('url', '?')}")
                if m.type == "error" else None)
            page.on("requestfailed",
                    lambda r: failed_reqs.append(f"{r.method} {r.url}"))

            # --- landing ---
            page.goto(API, wait_until="networkidle")
            page.wait_for_timeout(700)  # health pills settle
            page.screenshot(path=str(SHOTS / "01-landing-desktop.png"))
            report["title"] = page.title()
            report["pills"] = page.eval_on_selector_all(
                ".pill", "els => els.map(e => e.textContent.trim())")
            report["default_fields"] = page.locator("#fieldRows .frow").count()

            # --- validation: empty intent ---
            page.fill("#intent", "")
            page.click("#runBtn")
            page.wait_for_selector("#termBody .logline.error", timeout=3000)
            report["empty_intent_error"] = page.locator("#termBody .logline.error").first.text_content()
            page.screenshot(path=str(SHOTS / "02-validation-empty-intent.png"))

            # --- happy path run ---
            page.fill("#intent", "Find B2B SaaS startups in India with founder emails")
            page.fill("#seedUrls", f"{fx_url}/directory.html\n{fx_url}/about.html\n{fx_url}/blocked.html")
            page.click("#runBtn")
            page.wait_for_selector("#banner.show", timeout=60_000)
            page.wait_for_timeout(400)
            page.screenshot(path=str(SHOTS / "03-results-desktop.png"))

            report["banner"] = page.text_content("#bannerMsg")
            report["job_status"] = page.text_content("#jobStatus")
            report["grid_rows"] = page.locator("#gridBody tr").count()
            report["grid_headers"] = page.eval_on_selector_all(
                "#gridHead th", "els => els.map(e => e.textContent.trim())")
            report["log_lines"] = page.locator("#termBody .logline").count()
            report["warn_skips"] = page.locator("#termBody .logline.warn").all_text_contents()
            report["metrics"] = {k: page.text_content(f"#{k}")
                                 for k in ("mRecords", "mValidity", "mPages", "mSkipped")}
            report["banner_links"] = page.eval_on_selector_all(
                "#bannerFiles a", "els => els.map(e => e.textContent.trim())")
            report["run_again_label"] = page.text_content("#runBtn .txt")

            # --- download link from banner actually works ---
            href = page.get_attribute("#bannerFiles a", "href")
            if href:
                resp = page.request.get(API + href)
                report["download_status"] = resp.status
                report["download_csv_head"] = resp.text().splitlines()[0]

            # --- mobile viewport ---
            page.set_viewport_size({"width": 375, "height": 812})
            page.wait_for_timeout(300)
            page.screenshot(path=str(SHOTS / "04-mobile-after-run.png"))
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(500)
            page.screenshot(path=str(SHOTS / "05-mobile-landing.png"))

            browser.close()

        report["console_errors"] = console_errors
        report["failed_requests"] = failed_reqs
        print(json.dumps(report, indent=2, default=str))

        ok = (
            report["grid_rows"] > 0
            and report["banner"] and report["banner"].startswith("✔")
            and report["download_status"] == 200
            and not console_errors
        )
        print("\nDEMO:", "PASSED" if ok else "FAILED")
        return 0 if ok else 1
    finally:
        proc.terminate(); proc.wait(timeout=10)
        fx.shutdown()


sys.exit(main())
