#!/usr/bin/env bash
# Start the LOCAL discovery box for Project Omnisearch:
#   crw serve (:3001)  +  cloudflared quick tunnel (public HTTPS URL)
#
# Run from anywhere:  ./deploy/discovery/start-local-box.sh
# Idempotent — restarts only what is down. Prints the Render env vars.
#
# NOTE: a quick-tunnel URL changes whenever cloudflared restarts.
# If it changed, update OMNISEARCH_DISCOVERY_SERVER_BASE_URL in Render.
set -euo pipefail
cd "$(dirname "$0")/../.."          # repo root (omnisearch-engine/)

KEY_FILE=outputs/box-key.txt
LOG_SERVE=outputs/crw-serve.log
LOG_TUNNEL=outputs/cloudflared.log
mkdir -p outputs

# 1. crw serve ----------------------------------------------------------
if ! curl -sf -m 3 http://127.0.0.1:3001/health >/dev/null 2>&1; then
  if [ ! -f "$KEY_FILE" ]; then
    umask 077; openssl rand -hex 24 > "$KEY_FILE"; umask 022
  fi
  setsid env CRW_SEARCH_BACKEND_URL=http://127.0.0.1:8888 \
    CRW_AUTH__API_KEYS="$(cat "$KEY_FILE")" \
    CRW_HOST=0.0.0.0 CRW_PORT=3001 \
    "$HOME/.local/bin/crw" serve >> "$LOG_SERVE" 2>&1 < /dev/null &
  disown || true
  sleep 5
fi
curl -sf -m 5 http://127.0.0.1:3001/health >/dev/null \
  || { echo "crw serve failed — see $LOG_SERVE" >&2; exit 1; }

# 2. cloudflared quick tunnel -------------------------------------------
if ! pgrep -f 'cloudflared tunnel --url http://127.0.0.1:3001' >/dev/null 2>&1; then
  setsid "$HOME/.local/bin/cloudflared" tunnel --url http://127.0.0.1:3001 \
    > "$LOG_TUNNEL" 2>&1 < /dev/null &
  disown || true
  sleep 8
fi
URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_TUNNEL" | head -1 || true)
[ -n "$URL" ] || { echo "tunnel URL not found — see $LOG_TUNNEL" >&2; exit 1; }

echo "box health : $(curl -sf -m 5 http://127.0.0.1:3001/health)"
echo "public URL : $URL"
echo
echo "Set these in Render (service → Environment):"
echo "  OMNISEARCH_DISCOVERY_SERVER_BASE_URL=$URL"
echo "  OMNISEARCH_DISCOVERY_SERVER_API_KEY=$(cat "$KEY_FILE")"
