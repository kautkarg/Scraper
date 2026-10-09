#!/bin/sh
# Omnisearch Render-box entrypoint — runs SearXNG + crw serve + the public
# proxy as one free instance. POSIX sh (the searxng image has no bash).
set -eu

KEY="${BOX_API_KEY:-}"
if [ -z "$KEY" ]; then
  KEY="$(openssl rand -hex 24)"
  echo "[box] WARNING: BOX_API_KEY is not set — generated a key for THIS boot only:"
  echo "[box]   BOX_API_KEY=$KEY"
  echo "[box] Set BOX_API_KEY in the Render dashboard to keep it stable across redeploys."
fi
export CRW_AUTH__API_KEYS="$KEY"

# SearXNG binds loopback only (public access goes through box_proxy).
export GRANIAN_HOST=127.0.0.1
# crw's built-in /v1/search forwards to the in-box SearXNG.
export CRW_SEARCH__SEARXNG_URL=http://127.0.0.1:8080

/usr/local/searxng/entrypoint.sh &
SEARX_PID=$!
/opt/crw/crw serve &
CRW_PID=$!
PORT="${PORT:-10000}" python3 /usr/local/bin/box_proxy.py &
PROXY_PID=$!

term() {
  echo "[box] shutting down"
  kill "$SEARX_PID" "$CRW_PID" "$PROXY_PID" 2>/dev/null || true
}
trap term TERM INT

# Exit (so Render restarts us) if any child dies.
while kill -0 "$SEARX_PID" 2>/dev/null \
   && kill -0 "$CRW_PID" 2>/dev/null \
   && kill -0 "$PROXY_PID" 2>/dev/null; do
  sleep 5
done
echo "[box] a child process exited unexpectedly" >&2
term
exit 1
