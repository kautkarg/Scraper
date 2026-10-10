"""Typed loader for config.yaml with safe defaults (file is optional).

Any config key can be overridden with an ``OMNISEARCH_*`` environment
variable — the deployment path for hosts where no config file is writable
(Render web services). The suffix is matched against dotted config paths,
ignoring ``_``/``.`` differences:

    OMNISEARCH_DISCOVERY_SERVER_BASE_URL=https://box:3000
      -> discovery.server_base_url = "https://box:3000"
    OMNISEARCH_EXECUTION_MAX_RECORDS_PER_JOB=7
      -> execution.max_records_per_job = 7

Values are parsed as YAML, so ``true``, ``3000`` and ``[401,403]`` keep
their types. Env overrides always win over the file.
"""

from __future__ import annotations

import copy
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

_ENV_PREFIX = "OMNISEARCH_"
# OMNISEARCH_CONFIG selects the config *file* — never a config key itself.
_ENV_IGNORED = frozenset({"OMNISEARCH_CONFIG"})


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


_DEFAULTS: dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 8000},
    "paths": {"outputs": "./outputs", "sqlite_db": "./outputs/omnisearch.db"},
    "execution": {
        "max_records_per_job": 500,
        "max_pages_per_job": 900,
        "max_results_per_query": 25,
        "max_concurrent_scrapes": 6,
        "request_delay_seconds": 0.2,
        "timeout_seconds": 20,
        "retry_attempts": 2,
        "retry_backoff_seconds": 1.5,
        "block_status_codes": [401, 403, 429, 503, 999],
        "blocked_domain_patterns": [],
        "user_agent": "OmnisearchBot/1.0 (+local research engine)",
        # Second-wave contact harvest: company sites collected during wave 1
        # (name + website, but no email on the listing page) get a focused
        # revisit — homepage, then /contact — looking only for an email.
        "wave2_enabled": True,
        "wave2_max_pages": 30,
    },
    "discovery": {
        "mode": "auto",
        "crw_bin": "crw",
        "server_base_url": "http://127.0.0.1:3000",
        # Optional `CRW_AUTH__API_KEYS` value for a remote `crw serve`
        # (sent as `Authorization: Bearer` — never as a generic header,
        # so it cannot leak to third-party sites on the http fallback).
        "server_api_key": "",
        "searxng_base_url": "http://127.0.0.1:8888",
        "allow_http_fallback": True,
    },
    "llm": {
        "enabled": False,
        # "openai" -> HTTP {base_url}/chat/completions
        # "opencode" -> `opencode run` CLI (OpenCode Zen free tier;
        #               direct HTTP to Zen is rejected with 403)
        "mode": "openai",
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key": "",
        "model": "mimo",
        "temperature": 0.1,
        "timeout_seconds": 30,
        "opencode_bin": "opencode",
    },
    "integrations": {
        "google_sheets": {"credentials_file": "", "spreadsheet_id": ""},
        "webhook": {
            "url": "",
            "headers": {},
            "timeout_seconds": 15,
            "max_retries": 3,
            "batch_size": 25,
        },
    },
}


class Config:
    """Dict wrapper with attribute-style access to the merged config tree."""

    def __init__(self, data: dict[str, Any], source: Path | None = None):
        self._data = data
        self.source = source

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, path: str, default: Any = None) -> Any:
        """Fetch a dotted path, e.g. ``config.get("execution.timeout_seconds")``."""
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def outputs_dir(self) -> Path:
        path = Path(self.get("paths.outputs", "./outputs"))
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def sqlite_path(self) -> Path:
        path = Path(self.get("paths.sqlite_db", "./outputs/omnisearch.db"))
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def raw(self) -> dict[str, Any]:
        return self._data


def _norm_key(path: str) -> str:
    """Normalize a dotted path so ``a.b_c`` and ``A_B.C`` compare equal."""
    return path.replace("_", "").replace(".", "").lower()


def _apply_env_overrides(data: dict[str, Any]) -> None:
    """Apply ``OMNISEARCH_<CONFIG_PATH>`` variables on top of ``data`` (in place).

    Unknown ``OMNISEARCH_*`` names are reported on stderr so a typo is
    visible in deploy logs instead of silently doing nothing.
    """
    index: dict[str, str] = {}

    def walk(node: dict[str, Any], prefix: str) -> None:
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                walk(value, dotted)
            else:
                # setdefault: first occurrence wins on (unlikely) collisions
                index.setdefault(_norm_key(dotted), dotted)

    walk(data, "")
    for name, raw in sorted(os.environ.items()):
        if not name.startswith(_ENV_PREFIX) or name in _ENV_IGNORED:
            continue
        dotted = index.get(_norm_key(name[len(_ENV_PREFIX):]))
        if dotted is None:
            print(f"[config] ignoring unknown env override {name}", file=sys.stderr)
            continue
        if raw == "":
            value: Any = ""
        else:
            try:
                value = yaml.safe_load(raw)
            except yaml.YAMLError:
                value = raw  # unparseable — keep the literal string
        node = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    config_path = Path(path) if path else Path(
        os.environ.get("OMNISEARCH_CONFIG", DEFAULT_CONFIG_PATH)
    )
    # deepcopy: env overrides must never mutate the shared _DEFAULTS tree
    data = copy.deepcopy(_DEFAULTS)
    source: Path | None = None
    if config_path.is_file():
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"config file must contain a YAML mapping: {config_path}")
        data = _deep_merge(data, loaded)
        source = config_path
    _apply_env_overrides(data)
    return Config(data, source=source)


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()
