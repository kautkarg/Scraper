#!/usr/bin/env python3
"""OpenAI-compatible LLM proxy for Project Omnisearch.

Lets a *remote* app (e.g. the Render deployment) borrow this machine's
free OpenCode/MiMo completions over the Tailscale Funnel:

    Render (llm.mode=openai)
      -> https://<machine>.<tailnet>.ts.net:8443/v1/chat/completions
      -> this proxy (127.0.0.1:8765, bearer-key protected)
      -> `opencode run --pure -m <model> --format json`

Zero dependencies beyond the standard library + the `opencode` CLI.

Environment:
    PROXY_KEY          required — bearer token callers must present
    PROXY_PORT         default 8765
    PROXY_MODEL        default opencode/mimo-v2.6-flash-free
    PROXY_OPENCODE_BIN default opencode
    PROXY_CONCURRENCY  default 3 (parallel opencode sessions)
    PROXY_TIMEOUT      per-completion seconds, default 300

Run:  PROXY_KEY=$(cat outputs/llm-proxy-key.txt) python3 deploy/discovery/llm_proxy.py
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from core.llm import parse_opencode_events  # noqa: E402  (repo on sys.path)

PORT = int(os.environ.get("PROXY_PORT", "8765"))
KEY = os.environ.get("PROXY_KEY", "")
MODEL = os.environ.get("PROXY_MODEL", "opencode/mimo-v2.6-flash-free")
BIN = os.environ.get("PROXY_OPENCODE_BIN", "opencode")
CONCURRENCY = int(os.environ.get("PROXY_CONCURRENCY", "3"))
TIMEOUT = float(os.environ.get("PROXY_TIMEOUT", "300"))

_slots = threading.BoundedSemaphore(CONCURRENCY)


def _flatten_messages(messages: list) -> str:
    """Collapse an OpenAI-style message list into one CLI prompt."""
    system_parts, user_parts = [], []
    for msg in messages:
        role = str(msg.get("role", ""))
        content = str(msg.get("content", ""))
        if role == "system":
            system_parts.append(content)
        else:
            user_parts.append(content)
    prompt = "\n\n".join(user_parts)
    if system_parts:
        prompt = "SYSTEM:\n" + "\n".join(system_parts) + "\n\nUSER:\n" + prompt
    return prompt.strip()


def _run_opencode(prompt: str) -> str:
    import asyncio

    async def call() -> str:
        args = [BIN, "run", "--pure", "-m", MODEL, "--format", "json", prompt]
        with tempfile.TemporaryDirectory(prefix="omnisearch-proxy-") as tmp:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=tmp,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(),
                                                   timeout=TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError(f"opencode timed out after {TIMEOUT:.0f}s")
        text = parse_opencode_events(out.decode("utf-8", errors="replace"))
        if not text:
            tail = err.decode("utf-8", errors="replace")[-300:]
            raise RuntimeError(f"opencode returned no text: {tail}")
        return text

    return asyncio.run(call())


class Handler(BaseHTTPRequestHandler):
    server_version = "OmnisearchLLMProxy/1.0"

    def log_message(self, fmt: str, *args) -> None:  # concise stdout log
        sys.stdout.write(f"[llm-proxy] {self.address_string()} {fmt % args}\n")
        sys.stdout.flush()

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not KEY:
            return True  # operator chose an open proxy (not recommended)
        return header.startswith("Bearer ") and hmac.compare_digest(
            header.removeprefix("Bearer ").strip(), KEY)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/healthz", "/"):
            self._send(200, {"status": "ok", "model": MODEL,
                             "concurrency": CONCURRENCY})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in ("/v1/chat/completions",
                                         "/chat/completions"):
            self._send(404, {"error": {"message": "not found"}})
            return
        if not self._authorized():
            self._send(401, {"error": {"message": "invalid bearer key"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": {"message": "invalid JSON body"}})
            return
        messages = req.get("messages")
        if not isinstance(messages, list) or not messages:
            self._send(400, {"error": {"message": "messages[] required"}})
            return

        prompt = _flatten_messages(messages)
        acquired = _slots.acquire(timeout=TIMEOUT)
        if not acquired:
            self._send(429, {"error": {"message": "proxy at concurrency limit"}})
            return
        try:
            text = _run_opencode(prompt)
        except Exception as exc:  # noqa: BLE001 — surface to the caller
            self._send(502, {"error": {"message": str(exc)[:500]}})
            return
        finally:
            _slots.release()

        self._send(200, {
            "id": f"chatcmpl-{secrets.token_hex(8)}",
            "object": "chat.completion",
            "created": 0,
            "model": req.get("model") or MODEL,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0},
        })


def main() -> None:
    if not KEY:
        print("[llm-proxy] PROXY_KEY is required (generate one with: "
              "openssl rand -hex 24)", file=sys.stderr)
        raise SystemExit(1)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print(f"[llm-proxy] listening on 127.0.0.1:{PORT} "
          f"model={MODEL} concurrency={CONCURRENCY}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
