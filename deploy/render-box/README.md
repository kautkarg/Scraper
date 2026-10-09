# Render free-tier discovery box

One Render **free** web service that hosts the whole discovery side of
Omnisearch — SearXNG (search) + `crw serve` (scrape) behind a single
public port — so the Render app no longer depends on your laptop.

Why one combined service: Render's Hobby plan allots **750 free
instance-hours per workspace per month**. Two separate always-on
services would need ~1460 and would get the whole workspace suspended;
one service stays inside the budget even at 24/7 (≈730 h).

## What you get

| Route (public) | Backend |
|---|---|
| `https://<box>.onrender.com/health` | crw health |
| `https://<box>.onrender.com/v1/*` | crw serve (search, scrape, crawl) |
| `https://<box>.onrender.com/searxng/search?q=…&format=json` | SearXNG |

Memory footprint measured locally: **~135–170 MB** (limit: 512 MB).

## Deploy (once, ~10 minutes)

**Automated (preferred):** create a Render API key
(dashboard → Account Settings → API Keys; owner account so it can
create services), then:

```bash
export RENDER_API_KEY=rnd_xxx
.venv/bin/python scripts/deploy_render_box.py
```

The script creates the box service (Docker, free), waits for the
deploy, smoke-tests `/health` + `/searxng/search` + `/v1/search`,
rewires the app's three `OMNISEARCH_DISCOVERY_*` env vars, redeploys
the app, and verifies `/api/health` end-to-end. The box API key lives
in `outputs/box-key.txt` (gitignored; override with
`RENDER_BOX_API_KEY`).

**Manual (dashboard):**

1. Push this repo to GitHub (Render builds from it).
2. Render dashboard → **New → Web Service** → connect the repo.
   - Runtime: **Docker**
   - **Dockerfile path:** `deploy/render-box/Dockerfile`
   - Plan: **Free**
   - Region: any (pick one close to you; capacity for free instances
     varies by region)
3. Environment variable:
   - `BOX_API_KEY` = `openssl rand -hex 24` (generate locally; this is
     the crw API key — keep it secret)
4. Create the service and wait for the first deploy (~2–3 min).
5. Note the URL, e.g. `https://omnisearch-box-xxxx.onrender.com`.

### Point the app at the box

In the **app** service (`omnisearch-2fry`) → Environment, set:

```
OMNISEARCH_DISCOVERY_SERVER_BASE_URL=https://<box>.onrender.com
OMNISEARCH_DISCOVERY_SERVER_API_KEY=<same value as BOX_API_KEY>
OMNISEARCH_DISCOVERY_SEARXNG_BASE_URL=https://<box>.onrender.com/searxng
```

Save → app redeploys. Check `https://omnisearch-2fry.onrender.com/api/health`:
`"engines": {"server": true, "searxng": true}, "search_ready": true, "notes": []`.

**Rollback:** restore the three laptop/funnel values from
`deploy/discovery/start-local-box.sh` output — nothing else changes.

## Behavior notes (free tier)

- **Spin-down / cold start.** Free instances idle out after ~15 min.
  The first search/scrape call of a job then pays a one-time ~30–60 s
  wake (config timeout is 60 s + one retry, so it recovers
  automatically). Jobs themselves keep the box warm while running.
- **Bandwidth:** 5 GB/month outbound on Hobby. A 500-record run moves
  roughly 100–300 MB (pages fetched by the box + markdown returned to
  the app). Occasional big runs fit; several daily 500s will not.
- **Google is disabled** in `settings.yml` — datacenter IPs get
  captcha'd immediately. Bing, DuckDuckGo, Brave, Mojeek, Startpage
  etc. carry the search load (verified: 20 results/query locally).
- **No JS rendering.** The free 512 MB budget can't fit a headless
  Chromium next to SearXNG, so crw scrapes via plain HTTP. Server-
  rendered pages (directories, blogs, listings) work fine — the E2E
  test pulled 10/10 records from real startup directories. JS-only
  pages fall back to whatever raw HTML they ship.
- **No keep-alive ping** on purpose: a cron keeping the box warm 24/7
  would consume the entire monthly hour budget by itself.

## Rebuild / run locally (for testing)

```bash
# uses your local crw binary (gitignored), no GitHub download needed
cp ~/.local/bin/crw deploy/render-box/crw
docker build -f deploy/render-box/Dockerfile --build-arg CRW_SOURCE=local \
  -t omnisearch-box:test .
docker run --rm -p 10000:10000 -e BOX_API_KEY=testkey omnisearch-box:test
curl http://127.0.0.1:10000/health
curl "http://127.0.0.1:10000/searxng/search?q=test&format=json" | head -c 200
```

Render builds use the default (`CRW_SOURCE=github`) and download the
official fastCRW release.
