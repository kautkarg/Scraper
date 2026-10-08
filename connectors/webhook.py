"""Generic CRM / webhook sink — batched POSTs with retry and backoff.

Works with HubSpot, Notion, Airtable automations, or any custom endpoint:
payload shape is ``{"job_id": ..., "count": N, "records": [...]}``.
Static headers come from config; ``${ENV_VAR}`` references are expanded so
tokens never live in the repo.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

import httpx

from core.config import Config

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(value: str) -> str:
    return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)


class WebhookError(RuntimeError):
    pass


class WebhookSink:
    def __init__(self, config: Config):
        self.default_url = expand_env(config.get("integrations.webhook.url", "") or "")
        self.headers = {k: expand_env(str(v)) for k, v in
                        config.get("integrations.webhook.headers", {}).items()}
        self.timeout = float(config.get("integrations.webhook.timeout_seconds", 15))
        self.max_retries = int(config.get("integrations.webhook.max_retries", 3))
        self.batch_size = int(config.get("integrations.webhook.batch_size", 25))

    async def push(self, records: list[dict[str, Any]], job_id: str,
                   url: str | None = None) -> dict[str, Any]:
        target = expand_env(url or self.default_url)
        if not target:
            raise WebhookError("no webhook URL provided (UI field or config)")
        if not records:
            return {"sink": "webhook", "batches": 0, "rows": 0, "url": target}

        headers = {"Content-Type": "application/json", **self.headers}
        batches = [records[i:i + self.batch_size]
                   for i in range(0, len(records), self.batch_size)]
        sent = 0
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for index, batch in enumerate(batches, start=1):
                body = {"job_id": job_id, "batch": index, "batches": len(batches),
                        "count": len(batch), "records": batch}
                await self._post_with_retry(client, target, headers, body)
                sent += len(batch)
        return {"sink": "webhook", "batches": len(batches), "rows": sent, "url": target}

    async def _post_with_retry(self, client: httpx.AsyncClient, url: str,
                               headers: dict[str, str], body: dict[str, Any]) -> None:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await client.post(url, json=body, headers=headers)
                if resp.status_code < 400:
                    return
                # 4xx (except 429) are permanent — do not retry
                if resp.status_code < 500 and resp.status_code != 429:
                    raise WebhookError(
                        f"webhook rejected payload: HTTP {resp.status_code} {resp.text[:200]}")
                last_error = WebhookError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            except WebhookError:
                raise
            except Exception as exc:  # noqa: BLE001 — network errors are retryable
                last_error = exc
            if attempt < self.max_retries:
                await asyncio.sleep(1.5 * (attempt + 1))
        raise WebhookError(f"webhook failed after {self.max_retries + 1} attempts: {last_error}")

    async def probe(self) -> dict[str, Any]:
        return {"sink": "webhook", "ready": bool(self.default_url),
                "url": self.default_url or None,
                "error": None if self.default_url else "no default webhook URL configured"}
