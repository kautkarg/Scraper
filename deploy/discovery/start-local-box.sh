#!/usr/bin/env bash
# Start the LOCAL discovery box for Project Omnisearch:
#   crw serve (:3001)  +  Tailscale Funnel (stable public HTTPS URL)
#
# Run from anywhere:  ./deploy/discovery/start-local-box.sh
# Idempotent — restarts only what is down. Prints the Render env vars.
#
# One-time setup on a new machine:
#   curl -fsSL https://tailscale.com/install.sh | sh
#   sudo tailscale up                 # click the login URL
#   sudo tailscale funnel 3001        # approve the browser prompt once
# The funnel URL is permanent — set it in Render once and forget it.
set -euo pipefail
cd "$(dirname "$0")/../.."          # repo root (omnisearch-engine/)

KEY_FILE=outputs/box-key.txt
LOG_SERVE=outputs/crw-serve.log
mkdir -p outputs

# tailscale CLI — fall back to sudo when the daemon socket needs it
TS() { tailscale "$@" 2>/dev/null || sudo tailscale "$@"; }

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

# 2. Tailscale Funnel ---------------------------------------------------
if ! TS status >/dev/null 2>&1; then
  cat >&2 <<'MSG'
Tailscale is not installed/authenticated on this machine.
  curl -fsSL https://tailscale.com/install.sh | sh
  sudo tailscale up                # click the login URL it prints
  sudo tailscale funnel 3001       # approve the browser prompt once
MSG
  exit 1
fi
# idempotent: re-running keeps an existing funnel; first run may open approval
TS funnel 3001 >/dev/null 2>&1 || true

URL=$(TS status --json | python3 -c 'import json,sys; print("https://"+json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
[ -n "$URL" ] || { echo "tailscale URL not found" >&2; exit 1; }
curl -sf -m 8 "$URL/health" >/dev/null || {
  echo "funnel not published yet — run once:  sudo tailscale funnel 3001 (approve in browser)" >&2
  exit 1
}

echo "box health : $(curl -sf -m 5 http://127.0.0.1:3001/health)"
echo "public URL : $URL   (stable — never changes)"
echo
echo "Set these in Render (service → Environment):"
echo "  OMNISEARCH_DISCOVERY_SERVER_BASE_URL=$URL"
echo "  OMNISEARCH_DISCOVERY_SERVER_API_KEY=$(cat "$KEY_FILE")"
