"""LLM transport for query planning and record extraction.

Two modes:

``openai`` (default)
    Plain HTTP POST to ``{base_url}/chat/completions`` — works with any
    OpenAI-compatible local server (Mimo, Ollama, llama.cpp …).

``opencode``
    Shells out to ``opencode run --pure -m <model> --format json``. This is
    the only way to use OpenCode Zen *free* models (e.g.
    ``opencode/mimo-v2.6-flash-free``): the Zen free tier rejects direct API
    calls with "can only be used from within OpenCode" (HTTP 403). The CLI
    attaches the client ticket itself and costs $0.

Both paths return raw assistant text; callers parse it with
:func:`extract_json`. Every failure returns ``None`` — the LLM layer is
strictly optional and falls back to deterministic heuristics upstream.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from typing import Any

DEFAULT_SYSTEM = "Respond with valid JSON only."


# --------------------------------------------------------------------------
# response helpers
# --------------------------------------------------------------------------

def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of an LLM response.

    Handles bare JSON, ```json fenced blocks, and leading/trailing prose —
    free-tier models do not always obey "JSON only".
    """
    t = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = t.find(opener), t.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise json.JSONDecodeError("no JSON value found", text or "", 0)


def parse_opencode_events(stdout: str) -> str | None:
    """Merge an ``opencode run --format json`` event stream into one reply.

    Events arrive as JSON lines; ``text`` events carry whole parts keyed by
    ``part.id`` (keep the last update per part, preserve first-seen order).
    ``error`` events are collected — they only fail the call when no text
    was produced.
    """
    parts: dict[str, str] = {}
    order: list[str] = []
    errors: list[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "error":
            err = ev.get("error") or {}
            errors.append(str(err.get("message") or err))
        elif etype == "text":
            part = ev.get("part") or {}
            txt = part.get("text")
            if isinstance(txt, str):
                pid = str(part.get("id") or f"p{len(order)}")
                if pid not in parts:
                    order.append(pid)
                parts[pid] = txt
    text = "".join(parts[p] for p in order).strip()
    if text:
        return text
    if errors:
        raise RuntimeError(f"opencode: {errors[0]}")
    return None


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------

async def http_complete(config, prompt: str,
                        system: str = DEFAULT_SYSTEM) -> str | None:
    import httpx
    timeout = float(config.get("llm.timeout_seconds", 30))
    async with httpx.AsyncClient(timeout=timeout) as client:
        headers = {"Content-Type": "application/json"}
        if config.get("llm.api_key"):
            headers["Authorization"] = f"Bearer {config.get('llm.api_key')}"
        resp = await client.post(
            f"{str(config.get('llm.base_url')).rstrip('/')}/chat/completions",
            headers=headers,
            json={"model": config.get("llm.model", "mimo"),
                  "temperature": config.get("llm.temperature", 0.1),
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": prompt}]},
        )
        resp.raise_for_status()
        return str(resp.json()["choices"][0]["message"]["content"])


async def opencode_complete(config, prompt: str) -> str | None:
    """Run one completion through the opencode CLI (Zen free tier)."""
    binary = str(config.get("llm.opencode_bin", "opencode"))
    model = str(config.get("llm.model", "opencode/mimo-v2.6-flash-free"))
    timeout = float(config.get("llm.timeout_seconds", 120))
    args = [binary, "run", "--pure", "-m", model, "--format", "json", prompt]
    with tempfile.TemporaryDirectory(prefix="omnisearch-llm-") as tmp:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=tmp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, _err = await asyncio.wait_for(proc.communicate(),
                                               timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"opencode timed out after {timeout:.0f}s")
    return parse_opencode_events(out.decode("utf-8", errors="replace"))


async def complete(config, prompt: str,
                   system: str = DEFAULT_SYSTEM) -> str | None:
    """Dispatch to the configured transport.

    Raises on transport/parse failure — callers treat any exception as
    "LLM unavailable" and fall back to heuristics.
    """
    if not config.get("llm.enabled", False):
        return None
    if str(config.get("llm.mode", "openai")) == "opencode":
        return await opencode_complete(config, prompt)
    return await http_complete(config, prompt, system)
