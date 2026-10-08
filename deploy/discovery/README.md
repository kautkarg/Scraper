# Discovery box — fastCRW + SearXNG

Project Omnisearch is split into two halves:

| Half | Runs where | Does what |
|---|---|---|
| **App** (FastAPI + web UI) | Render (or anywhere) | Job API, SSE progress, parsing, exports |
| **Discovery box** (`crw serve` + SearXNG) | Your laptop or a $5 VPS | Search (`/v1/search`) + scrape (`/v1/scrape`) |

The app talks to this box over plain HTTP using
`discovery.server_base_url` (+ optional `discovery.server_api_key`).

> Works equally on a local machine (dev) or a cheap VPS
> (DigitalOcean / Hetzner / AWS Lightsail). For production, prefer the VPS —
> a laptop that sleeps will make Render jobs fail search.

## 1. Install fastCRW

```bash
CRW_INSTALL_DIR="$HOME/.local/bin" curl -fsSL https://fastcrw.com/install | sh
export PATH="$HOME/.local/bin:$PATH"
crw --version
```

## 2. Start SearXNG (search backend for crw)

fastCRW deliberately has no built-in web search — it delegates to a backend
(SearXNG with JSON enabled). From this directory:

```bash
docker compose up -d
curl 'http://127.0.0.1:8888/search?q=test&format=json' | head -c 300
```

## 3. Run `crw serve`

Generate a long random key and keep it with the Render env var
`OMNISEARCH_DISCOVERY_SERVER_API_KEY`:

```bash
openssl rand -hex 24
```

**Option A — systemd (VPS, recommended):** use `omnisearch-crw.service`
(header comment has install steps).

**Option B — foreground / nohup (laptop):**

```bash
CRW_SEARCH_BACKEND_URL=http://127.0.0.1:8888 \
CRW_AUTH__API_KEYS=<your-key> \
CRW_HOST=0.0.0.0 CRW_PORT=3000 \
  crw serve
```

## 4. Verify

```bash
curl http://127.0.0.1:3000/health
# {"active_crawl_jobs":0,"status":"ok","version":"..."}   <- what the app probes

curl http://127.0.0.1:3000/v1/search -H 'content-type: application/json' \
     -H 'Authorization: Bearer <your-key>' \
     -d '{"query":"test","limit":1}'
```

Troubleshooting: `crw doctor` (checks config source, browsers, search
backend reachability). Remember `CRW_*` env vars beat `~/.config/crw/config.toml`.

## 5. Security (if exposed to the internet)

- **Always set `CRW_AUTH__API_KEYS`** when the port is reachable from
  Render/the internet — the API is otherwise completely open.
- `GET /health` stays unauthenticated (the app's probe relies on it).
- Lock down with a firewall if you can (`ufw allow from <Render IP range> to any port 3000`),
  and/or only bind to an interface you trust.

## 6. Point Render at this box

In the Render service env vars (or at blueprint setup):

| Env var | Value |
|---|---|
| `OMNISEARCH_DISCOVERY_SERVER_BASE_URL` | `http://<box-ip-or-dns>:3000` |
| `OMNISEARCH_DISCOVERY_SERVER_API_KEY` | the key from step 3 (if set) |

Then check the app's health: `GET https://<your-app>.onrender.com/api/health`
→ `engine.engines.server` must be `true`.

Note: on Render there is no local `crw` binary or SearXNG, so the `cli` and
`searxng` engine pills are expected to be `false` — only `server` matters.
