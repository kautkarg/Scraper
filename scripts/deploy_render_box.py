"""Deploy the Render free discovery box + rewire the app — one command.

Usage:
  export RENDER_API_KEY=rnd_xxx          # dashboard → Account Settings → API Keys
  .venv/bin/python scripts/deploy_render_box.py

Optional env:
  RENDER_APP_SERVICE   app service name or id (default: auto-detect from repo)
  RENDER_BOX_NAME      box service name (default: omnisearch-box)
  RENDER_BOX_API_KEY   crw key for the box (default: outputs/box-key.txt, auto-generated)
  RENDER_REGION        default: oregon
  RENDER_GIT_REPO      default: https://github.com/kautkarg/Scraper.git
  RENDER_GIT_BRANCH    default: main
"""
from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path

import httpx

API = "https://api.render.com/v1"
ROOT = Path(__file__).resolve().parent.parent
BOX_NAME = os.environ.get("RENDER_BOX_NAME", "omnisearch-box")
REPO = os.environ.get("RENDER_GIT_REPO", "https://github.com/kautkarg/Scraper").removesuffix(".git")
BRANCH = os.environ.get("RENDER_GIT_BRANCH", "main")
REGION = os.environ.get("RENDER_REGION", "oregon")


def die(msg: str) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def http(client: httpx.Client, method: str, path: str, **kw) -> httpx.Response:
    r = client.request(method, API + path, **kw)
    if r.status_code >= 400:
        die(f"{method} {path} -> HTTP {r.status_code}: {r.text[:500]}")
    return r


def items(resp: httpx.Response, key: str) -> list[dict]:
    """Render v1 list endpoints return [{cursor, <key>: {...}}, ...]."""
    data = resp.json()
    if not isinstance(data, list):
        return []
    return [item[key] for item in data if isinstance(item, dict) and key in item]


def ensure_box_key() -> str:
    env_key = os.environ.get("RENDER_BOX_API_KEY")
    if env_key:
        return env_key
    key_file = ROOT / "outputs" / "box-key.txt"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(24)
    key_file.parent.mkdir(exist_ok=True)
    key_file.write_text(key + "\n")
    key_file.chmod(0o600)
    print(f"generated box API key -> {key_file}")
    return key


def find_app_service(client: httpx.Client) -> dict:
    want = os.environ.get("RENDER_APP_SERVICE")
    services = items(http(client, "GET", "/services?limit=100"), "service")
    apps = [s for s in services
            if s.get("type") == "web_service"
            and (s.get("repo") or "").removesuffix(".git").endswith("kautkarg/Scraper")
            and "render-box" not in (s.get("serviceDetails", {}).get("envSpecificDetails", {}) or {}).get("dockerfilePath", "")]
    if want:
        for s in apps:
            if s["id"] == want or s["name"] == want:
                return s
        die(f"RENDER_APP_SERVICE={want!r} not found among {[(s['id'], s['name']) for s in apps]}")
    if len(apps) != 1:
        die(f"could not uniquely identify the app service: {[(s['id'], s['name']) for s in apps]} "
            f"— set RENDER_APP_SERVICE and re-run")
    return apps[0]


def find_box_service(client: httpx.Client) -> dict | None:
    services = items(http(client, "GET", "/services?limit=100"), "service")
    for s in services:
        if s.get("name") == BOX_NAME and s.get("type") == "web_service":
            return s
    return None


def wait_deploy(client: httpx.Client, service_id: str, label: str,
                timeout_s: int = 600, initial_deploy_id: str | None = None) -> None:
    t0 = time.time()
    last = ""
    seen_ids: set[str] = set()
    if initial_deploy_id:
        seen_ids.add(initial_deploy_id)
    while time.time() - t0 < timeout_s:
        deploys = items(http(client, "GET", f"/services/{service_id}/deploys?limit=5"), "deploy")
        if not deploys:
            time.sleep(10)
            continue
        current = deploys[0]
        status = current.get("status", "?")
        marker = f"{current.get('id')}:{status}"
        if marker != last:
            print(f"  [{label}] deploy {current.get('id')}: {status}")
            last = marker
        if status == "live":
            return
        if status in ("failed", "canceled"):
            die(f"{label} deploy {status} — check the Render dashboard deploy log "
                f"(https://dashboard.render.com)")
        time.sleep(10)
    die(f"{label} deploy still not live after {timeout_s}s")


def create_box(client: httpx.Client, owner_id: str, box_key: str) -> dict:
    print(f"creating box service {BOX_NAME!r} (docker, {REGION}, free)…")
    payload = {
        "type": "web_service",
        "name": BOX_NAME,
        "ownerId": owner_id,
        "repo": REPO,
        "branch": BRANCH,
        "autoDeploy": "yes",
        "envVars": [{"key": "BOX_API_KEY", "value": box_key}],
        "serviceDetails": {
            "runtime": "docker",
            "envSpecificDetails": {
                "dockerfilePath": "deploy/render-box/Dockerfile",
                "dockerContext": ".",
            },
            "healthCheckPath": "/health",
            "plan": "free",
            "region": REGION,
        },
    }
    r = http(client, "POST", "/services", json=payload)
    body = r.json()
    service = body.get("service") or body
    deploy_id = body.get("deployId")
    print(f"  box service id: {service.get('id')}  deploy: {deploy_id}")
    if deploy_id:
        wait_deploy(client, service["id"], "box", initial_deploy_id=deploy_id)
    return service


def upsert_env(client: httpx.Client, service_id: str, updates: dict[str, str]) -> None:
    for key, value in updates.items():
        http(client, "PUT", f"/services/{service_id}/env-vars/{key}", json={"value": value})
        print(f"  env set: {key}")


def service_url(client: httpx.Client, service_id: str) -> str:
    # single-GET returns a bare service object; lists are cursor-wrapped
    s = http(client, "GET", f"/services/{service_id}").json()
    if isinstance(s, dict) and "service" in s:
        s = s["service"]
    return ((s.get("serviceDetails") or {}).get("url") or "").rstrip("/")


def main() -> None:
    api_key = os.environ.get("RENDER_API_KEY", "").strip()
    if not api_key:
        die("RENDER_API_KEY is not set — create one at https://dashboard.render.com/account/#api-keys "
            "(owner account; needs service-create permission) and export it")

    box_key = ensure_box_key()

    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(headers=headers, timeout=60) as client:
        owners = items(http(client, "GET", "/owners"), "owner")
        if not owners:
            die("no Render owners returned for this API key")
        owner_id = owners[0]["id"]
        print(f"Render account: {owners[0].get('email') or owner_id}")

        app = find_app_service(client)
        print(f"app service:   {app['name']} ({app['id']})")

        box = find_box_service(client)
        if box:
            print(f"box service already exists: {box['name']} ({box['id']}) — reusing")
        else:
            box = create_box(client, owner_id, box_key)

        box_url = service_url(client, box["id"])
        if not box_url:
            wait_deploy(client, box["id"], "box")
            box_url = service_url(client, box["id"])
        print(f"box URL: {box_url}")

        # --- verify the box itself -------------------------------------
        with httpx.Client(timeout=90) as plain:
            h = plain.get(f"{box_url}/health")
            print(f"  GET /health -> {h.status_code} {h.text[:80]}")
            if h.status_code != 200:
                die("box /health not 200 — box may still be booting; re-run in a minute")
            s = plain.get(f"{box_url}/searxng/search", params={"q": "test", "format": "json"}, timeout=45)
            n = len(s.json().get("results", [])) if s.status_code == 200 else -1
            print(f"  GET /searxng/search -> {s.status_code}, {n} results")
            v = plain.post(f"{box_url}/v1/search",
                           headers={"Authorization": f"Bearer {box_key}"},
                           json={"query": "test", "limit": 1}, timeout=60)
            ok = v.json().get("success") if v.status_code == 200 else "?"
            print(f"  POST /v1/search -> {v.status_code} success={ok}")

        # --- rewire the app ---------------------------------------------
        print("rewiring app env…")
        upsert_env(client, app["id"], {
            "OMNISEARCH_DISCOVERY_SERVER_BASE_URL": box_url,
            "OMNISEARCH_DISCOVERY_SERVER_API_KEY": box_key,
            "OMNISEARCH_DISCOVERY_SEARXNG_BASE_URL": box_url + "/searxng",
        })

        print("triggering app deploy…")
        d = http(client, "POST", f"/services/{app['id']}/deploys", json={}).json()
        wait_deploy(client, app["id"], "app", timeout_s=420,
                    initial_deploy_id=d.get("deployId") or (d.get("deploy") or {}).get("id"))

        app_url = service_url(client, app["id"])
        print(f"app URL: {app_url}")
        with httpx.Client(timeout=120) as plain:
            r = None
            for attempt in range(6):
                r = plain.get(f"{app_url}/api/health")
                if r.status_code == 200:
                    break
                print(f"  app health {r.status_code}, retrying… ({attempt + 1}/6)")
                time.sleep(10)
            body = r.json()
            engine = body.get("engine", {})  # /api/health nests under "engine"
            engines = engine.get("engines", {})
            print(f"  app /api/health engines={engines} search_ready={engine.get('search_ready')} "
                  f"notes={engine.get('notes')}")
            if not (engines.get("server") and engine.get("search_ready")):
                die("app health does not show server engine ready — inspect /api/health yourself")

    print("\nDONE. Box + app live; discovery no longer needs your laptop.")
    print(f"  box: {box_url}  (key in outputs/box-key.txt)")
    print(f"  app: {app_url}")
    print("Rollback: set the app's three OMNISEARCH_DISCOVERY_* env vars back to the")
    print("laptop/funnel values printed by deploy/discovery/start-local-box.sh.")


if __name__ == "__main__":
    main()
