# Project Omnisearch — Autonomous Intent-Based Data Engine

Turn a natural-language goal into validated, structured records — fully locally.

```
[ User Intent (OpenCode + Mimo) ]
        │
        ▼
 Step 1  Query Formulation ............ deterministic heuristics / Mimo LLM planner
        │
        ▼
 Step 2  fastCRW Discovery & Scrape .... crw CLI · crw serve · SearXNG · HTTP fallback
        │
        ▼
 Step 3  Schema Normalization ......... typed coercion, dedupe, ≥90% validity tracking
        │
        ▼
 Step 4  Local Storage ................ CSV / JSON / Markdown / SQLite
         + Sinks ...................... Google Sheets · CRM webhooks
```

## Quick start

```bash
cd omnisearch-engine
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

uvicorn main:app --host 127.0.0.1 --port 8000
# or: python main.py
```

Open <http://127.0.0.1:8000> — the glassmorphic UI ships with a live log
terminal, streaming data grid, and engine status pills.

### Enabling autonomous discovery (fastCRW)

The pipeline runs in **fallback mode** (seed URLs + local httpx extraction) out
of the box. For full autonomous search, install any one of:

| Engine | How | Config key |
|---|---|---|
| `crw` CLI | `curl -fsSL https://fastcrw.com/install \| sh` (set `CRW_INSTALL_DIR=~/.local/bin` to avoid sudo) | `discovery.crw_bin` |
| `crw serve` API | `crw serve --port 3000` | `discovery.server_base_url` |
| Standalone SearXNG | point at your instance | `discovery.searxng_base_url` |

`discovery.mode: auto` probes them in order: **cli → server → searxng → http fallback**.

#### Verified local stack (this machine)

```bash
# 1. fastCRW CLI (search + scrape)
CRW_INSTALL_DIR="$HOME/.local/bin" curl -fsSL https://fastcrw.com/install | sh

# 2. SearXNG search backend (JSON API enabled for crw + our client)
docker run -d --name omnisearch-searxng -p 8888:8080 \
  -v "$PWD/deploy/searxng-settings.yml:/etc/searxng/settings.yml:ro" \
  -e SEARXNG_BASE_URL=http://127.0.0.1:8888/ searxng/searxng
# (already created with --restart unless-stopped)

# 3. tell crw about the backend (one line in ~/.config/crw/config.toml)
#    [search]
#    search_backend_url = "http://127.0.0.1:8888"
```

Verified: `crw search … --json` returns live results; an autonomous job
(zero seed URLs) discovered 12 candidates from 4 queries, scraped 4 real
domains via the `cli` engine, and exported 6 records at 100% validity in ~13s
(`scripts/e2e_autonomous.py`).

### Mimo LLM planning (live & free)

```yaml
# config.yaml — the shipped default
llm:
  enabled: true
  mode: opencode          # shells out to `opencode run --pure -m <model>`
  model: opencode/mimo-v2.6-flash-free
  timeout_seconds: 240    # slow-box headroom; heuristics take over on expiry
```

- **`mode: opencode`** — uses OpenCode Zen's *free* MiMo tier through the
  opencode CLI (≥ 1.18 on PATH). Zen rejects direct HTTP calls with
  `403 FreeTierError: can only be used from within OpenCode`, so the CLI
  transport attaches the client ticket for you. Cost: **$0**.
- **`mode: openai`** — plain HTTP to any OpenAI-compatible server
  (`base_url` + `api_key`): a provider key (OpenAI/Groq/DeepSeek/…),
  Ollama, llama.cpp, **or this laptop's free MiMo via the funnel** —
  see [Large runs](#large-runs-500-records) below.
- **`enabled: false`** — deterministic heuristics only (still fully functional).

LLM output is always gated by `parser.ingest()` schema validation, so
hallucinated or incomplete rows are rejected before export. Failures at any
point fall back to heuristic queries/extraction — the job never dies because
the LLM did. Verified live: planner queries, record extraction, and a full
end-to-end job (`scripts/e2e_llm.py`).

### Sinks requiring your credentials

| Sink | What to provide | Where |
|---|---|---|
| Google Sheets | service-account JSON | `secrets/google_service_account.json` |
| CRM webhook | default endpoint (optional) | `integrations.webhook.url` in `config.yaml` |

`GET /api/health` reports each sink's readiness and the exact missing input.

## Deploy on Render

```text
┌──────────────────────────────┐        ┌─────────────────────────────────┐
│  Render — Web Service (free) │  HTTP  │  Discovery box (laptop / $5 VPS)│
│  FastAPI + web UI            │ ─────▶ │  crw serve  +  SearXNG          │
│  OMNISEARCH_* env vars       │        │  /v1/search · /v1/scrape        │
└──────────────────────────────┘        └─────────────────────────────────┘
```

The repo ships a [`render.yaml`](render.yaml) blueprint — no dashboard
twiddling beyond two env vars:

1. **Set up the discovery box first** — follow
   [`deploy/discovery/README.md`](deploy/discovery/README.md)
   (`crw serve` + SearXNG, with an API key). Note the box's `http://<ip>:3000`.
2. **Push this repo to GitHub** (`git init && git add -A && git commit`;
   create an empty repo and `git remote add origin … && git push -u origin main`).
3. **Render → New → Blueprint** → select the repo. The blueprint reads
   `render.yaml`; when prompted, fill:
   - `OMNISEARCH_DISCOVERY_SERVER_BASE_URL` = `http://<box-ip>:3000`
   - `OMNISEARCH_DISCOVERY_SERVER_API_KEY` = the key you set on the box
4. **Verify**: `https://<app>.onrender.com/api/health` →
   `engine.engines.server` is `true`. Jobs now search/scrape via the box.

### Environment variables

Any `config.yaml` key can be overridden with `OMNISEARCH_<KEY>` (dotted path,
underscores ignored — parsed as YAML, file < env):

| Variable | Sets | Typical value |
|---|---|---|
| `OMNISEARCH_DISCOVERY_SERVER_BASE_URL` | `discovery.server_base_url` | `https://<machine>.<tailnet>.ts.net` |
| `OMNISEARCH_DISCOVERY_SERVER_API_KEY` | `discovery.server_api_key` | box key (Bearer) |
| `OMNISEARCH_LLM_ENABLED` | `llm.enabled` | `true` / `false` |
| `OMNISEARCH_LLM_MODE` | `llm.mode` | `openai` / `opencode` |
| `OMNISEARCH_LLM_BASE_URL` | `llm.base_url` | `https://…ts.net:8443/v1` or provider `/v1` |
| `OMNISEARCH_LLM_API_KEY` | `llm.api_key` | proxy key or provider key |
| `OMNISEARCH_LLM_MODEL` | `llm.model` | `opencode/mimo-v2.6-flash-free` |
| `OMNISEARCH_EXECUTION_MAX_RECORDS_PER_JOB` | `execution.max_records_per_job` | `500` |

Unknown `OMNISEARCH_*` names are reported on stderr (typo visibility in
deploy logs). `OMNISEARCH_CONFIG` still selects a config *file* instead.

### Free-plan notes

- **LLM is opt-in on Render**: with `OMNISEARCH_LLM_MODE=openai` (the
  blueprint default) no extra binary is installed — point the app at the
  funnel proxy or a provider key. Only `MODE=opencode` downloads the
  ~185 MB binary at build time. Heuristics work either way; confirm
  `llm.ready: true` in `/api/health` after flipping the vars.
- **Spin-down**: free services sleep after 15 min — first request takes
  ~1 min (the SSE stream heartbeats every 15 s to stay under proxy limits).
- **Ephemeral disk**: `outputs/` and SQLite reset on every redeploy/restart —
  download CSVs or hook a sink for anything you need to keep.
- The SSE stream sends `: keep-alive` heartbeats, so long Mimo calls don't
  trip reverse-proxy idle timeouts.

## Large runs (500 records)

One run is expected to return **hundreds of precise rows**, not a handful:

- **Target records** — the UI's *Target records* field (default `500`,
  max `2000`) is sent as `max_records`. API callers can set it directly,
  or state the count in the intent (`"Find 20 …"`) and it is honored when
  `max_records` is omitted.
- **Ceilings** — `execution.max_records_per_job: 500`,
  `max_pages_per_job: 900`, `max_results_per_query: 25`; the heuristic
  planner fans out up to 8 queries (LLM planner: 6–10). Override any of
  them with `OMNISEARCH_*` env vars.
- **Dedupe** — records collapse only on *identical full rows*, so several
  companies sharing one contact email or a cross-filled source URL all
  count as separate records.
- **Keep the page open** — the job streams over SSE from the browser;
  closing the tab or letting the Render free plan spin down mid-run stops
  it. Long runs: keep the tab awake (or run headless via
  `scripts/e2e_*.py` against the API).

### Choosing the LLM transport (Render)

| Option | When | Env vars |
|---|---|---|
| **A. Laptop tunnel (free)** | box script is running | `ENABLED=true`, `MODE=openai`, `BASE_URL=https://<machine>.<tailnet>.ts.net:8443/v1`, `API_KEY=<outputs/llm-proxy-key.txt>`, `MODEL=opencode/mimo-v2.6-flash-free` |
| **B. Provider key** | you have an API key | `ENABLED=true`, `MODE=openai`, `BASE_URL=<provider>/v1`, `API_KEY=<key>`, `MODEL=<model>` |
| **C. On-Render opencode** | no laptop, slow is fine | `ENABLED=true`, `MODE=opencode` (build installs the binary) |

Option A is wired automatically: `deploy/discovery/start-local-box.sh`
starts `deploy/discovery/llm_proxy.py` (OpenAI-compatible, bearer-key
protected) and publishes it on funnel port **8443** next to the discovery
box on **443**.

## Project layout

```text
omnisearch-engine/
├── core/
│   ├── config.py          # typed config.yaml loader with defaults
│   ├── orchestrator.py    # prompt-to-plan execution loop + SSE event bus
│   ├── parser.py          # schema validation, coercion, dedupe, stats
│   └── exporter.py        # CSV / JSON / Markdown / SQLite writers
├── connectors/
│   ├── fastcrw_client.py  # crw CLI / crw serve / SearXNG / HTTP fallback
│   ├── google_sheets.py   # service-account append via Sheets REST API
│   └── webhook.py         # batched CRM webhook POSTs with retry
├── web/static/index.html  # vanilla JS glassmorphic UI (SSE + data grid)
├── main.py                # FastAPI control plane
├── config.yaml            # execution limits, engines, sinks
├── outputs/               # harvested data lands here
└── tests/                 # pytest suite
```

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | engine + sink + LLM availability |
| `POST` | `/api/jobs` | submit `{intent, fields[], destination, seed_urls[]}` |
| `GET` | `/api/jobs/{id}/events` | SSE stream: `stage` / `record` / `summary` / `error` |
| `GET` | `/api/jobs/{id}/records` | normalized records for the grid |
| `GET` | `/api/jobs/{id}/download?format=csv` | fetch a produced file |
| `GET` | `/api/jobs/{id}` | status + summary |

### Example

```bash
curl -s -X POST localhost:8000/api/jobs -H 'Content-Type: application/json' -d '{
  "intent": "Find SaaS startups with founder emails",
  "fields": [
    {"name": "company_name", "type": "string", "required": true},
    {"name": "email", "type": "email", "required": true},
    {"name": "website", "type": "url"}
  ],
  "destination": "csv",
  "seed_urls": ["https://example.com/directory"]
}'
```

## Acceptance criteria → where enforced

1. **Zero external cloud costs** — all engines are local binaries/services;
   LLM planning is optional (a local OpenAI-compatible endpoint or the free
   OpenCode Zen tier); sinks only fire when *you* configure them.
2. **Schema compliance ≥ 90%** — `core/parser.py` coerces + validates every
   record; jobs log a warning and surface `validity_ratio` in the summary when
   the run falls below 0.90.
3. **Resilience** — blocked domains (403/429/…) raise `BlockedError`, are
   logged as `SKIP <domain>` events, and harvesting continues from remaining
   sources (`core/orchestrator.py` worker pool).

## Tests

```bash
pytest -q                    # 28 unit/integration tests
python scripts/e2e_api.py    # live server: job → SSE → CSV → validation
python scripts/e2e_autonomous.py  # real crw search + real web scrape (no seed URLs)
python scripts/e2e_llm.py    # live MiMo: planner + extractor + full job (3 phases)
python scripts/ui_demo.py    # Playwright browser demo + QA (screenshots in outputs/ui-demo/)
```
