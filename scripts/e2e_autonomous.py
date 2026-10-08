"""Autonomous e2e: real crw search discovery + real web scraping, no seed URLs."""
import asyncio, json, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path("/home/gaurav/Documents/Data Scraper/omnisearch-engine")
API = "http://127.0.0.1:8207"

import yaml
cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
cfg["llm"]["enabled"] = False
cfg["execution"].update({"max_pages_per_job": 5, "max_records_per_job": 6,
                         "max_results_per_query": 3, "max_concurrent_scrapes": 3,
                         "timeout_seconds": 25, "retry_attempts": 1})
cfg["paths"]["outputs"] = "/tmp/opencode/autonomous-outputs"
tmp_cfg = Path("/tmp/opencode/config-autonomous.yaml")
tmp_cfg.write_text(yaml.safe_dump(cfg))


def wait_health(timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(API + "/api/health", timeout=2)
            return True
        except Exception:
            time.sleep(0.3)
    return False


async def consume(job_id: str):
    import httpx
    stages, records, summary = [], [], None
    async with httpx.AsyncClient(timeout=120) as c:
        async with c.stream("GET", f"{API}/api/jobs/{job_id}/events") as r:
            r.raise_for_status()
            current = None
            async for line in r.aiter_lines():
                if line.startswith("event: "):
                    current = line[7:].strip()
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                    if current == "stage":
                        stages.append(data)
                        print(f"  [{data['stage']}] {data['level']}: {data['message']}")
                    elif current == "record":
                        records.append(data["values"])
                        print(f"  record: {data['values']}")
                    elif current == "error":
                        print(f"  ERROR: {data['message']}")
                    elif current == "summary":
                        summary = data["summary"]
                        return stages, records, summary
    return stages, records, summary


def main():
    env = dict(__import__("os").environ, OMNISEARCH_CONFIG=str(tmp_cfg))
    proc = subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", "8207", "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_health(), "server did not come up"
        health = json.load(urllib.request.urlopen(API + "/api/health"))
        print("HEALTH:", json.dumps(health))
        engines = health["engine"]["engines"]
        assert engines["cli"] is True, "crw cli not detected"
        assert engines["searxng"] is True, "searxng not detected"
        assert health["engine"]["search_ready"] is True, "search not ready"

        body = json.dumps({
            "intent": "Top B2B SaaS companies in India with websites and contact emails",
            "fields": [
                {"name": "company_name", "type": "string", "required": True},
                {"name": "website", "type": "url", "required": False},
                {"name": "email", "type": "email", "required": False},
            ],
            "destination": "csv",
            "seed_urls": [],
        }).encode()
        req = urllib.request.Request(API + "/api/jobs", data=body,
                                     headers={"Content-Type": "application/json"})
        job = json.load(urllib.request.urlopen(req))
        job_id = job["job_id"]
        print(f"JOB: {job_id} accepted (autonomous — zero seed URLs)\n")

        stages, records, summary = asyncio.run(consume(job_id))
        print("\nSUMMARY:", json.dumps(summary, indent=2))

        engines_used = {}
        for s in stages:
            msg = s["message"]
            if msg.startswith("scraped ") and "[" in msg:
                eng = msg.rsplit("[", 1)[1].split(",")[0]
                engines_used[eng] = engines_used.get(eng, 0) + 1
        real_domains = {u.split("/")[2] for u in summary.get("sources_scraped", [])}

        checks = {
            "search queries ran": len(summary.get("queries", [])) > 0,
            "search returned real results": any("results for:" in s["message"]
                                                for s in stages),
            "real pages scraped": len(summary.get("sources_scraped", [])) > 0,
            "no localhost sources": all(not d.startswith("127.0.0.1")
                                        for d in real_domains),
            "records extracted": summary.get("records_exported", 0) > 0,
            "status completed": summary.get("status") == "completed",
            "cli engine did the scraping": engines_used.get("cli", 0) > 0,
        }
        print("\nENGINE USAGE:", engines_used)
        print("DOMAINS:", sorted(real_domains))
        for name, ok in checks.items():
            print(f"  {'✓' if ok else '✗'} {name}")
        passed = all(checks.values())
        print("\nAUTONOMOUS E2E:", "PASSED" if passed else "FAILED")
        return 0 if passed else 1
    finally:
        proc.terminate(); proc.wait(timeout=10)


sys.exit(main())
