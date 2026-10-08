"""Env-var config overrides and fastCRW server auth (the Render deploy path)."""

from __future__ import annotations

import pytest

import core.config as cfg_mod
from connectors.fastcrw_client import FastCRWClient
from core.config import load_config


def test_env_overrides_apply_with_types(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("discovery:\n  mode: auto\n")
    monkeypatch.setenv("OMNISEARCH_CONFIG", str(cfg_file))
    monkeypatch.setenv("OMNISEARCH_DISCOVERY_MODE", "server")
    monkeypatch.setenv("OMNISEARCH_DISCOVERY_SERVER_BASE_URL", "https://box.example:3000")
    monkeypatch.setenv("OMNISEARCH_EXECUTION_MAX_RECORDS_PER_JOB", "7")
    monkeypatch.setenv("OMNISEARCH_LLM_ENABLED", "true")
    monkeypatch.setenv("OMNISEARCH_DISCOVERY_SERVER_API_KEY", "k-123")

    cfg = load_config()
    assert cfg.get("discovery.mode") == "server"
    assert cfg.get("discovery.server_base_url") == "https://box.example:3000"
    assert cfg.get("execution.max_records_per_job") == 7
    assert cfg.get("llm.enabled") is True
    assert cfg.get("discovery.server_api_key") == "k-123"


def test_env_override_beats_file(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("llm:\n  enabled: false\n  model: from-file\n")
    monkeypatch.setenv("OMNISEARCH_CONFIG", str(cfg_file))
    monkeypatch.setenv("OMNISEARCH_LLM_ENABLED", "true")
    monkeypatch.setenv("OMNISEARCH_LLM_MODEL", "from-env")

    cfg = load_config()
    assert cfg.get("llm.enabled") is True
    assert cfg.get("llm.model") == "from-env"


def test_unknown_env_override_warns_and_is_ignored(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OMNISEARCH_NOT_A_REAL_KEY", "x")
    cfg = load_config(str(tmp_path / "missing.yaml"))
    assert cfg.get("not_a_real_key") is None
    assert "OMNISEARCH_NOT_A_REAL_KEY" in capsys.readouterr().err


def test_env_overrides_do_not_pollute_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNISEARCH_DISCOVERY_MODE", "server")
    cfg1 = load_config(str(tmp_path / "missing.yaml"))
    assert cfg1.get("discovery.mode") == "server"

    monkeypatch.delenv("OMNISEARCH_DISCOVERY_MODE")
    cfg2 = load_config(str(tmp_path / "missing.yaml"))
    assert cfg2.get("discovery.mode") == cfg_mod._DEFAULTS["discovery"]["mode"] == "auto"


def test_server_auth_header(monkeypatch):
    monkeypatch.setenv("OMNISEARCH_DISCOVERY_SERVER_API_KEY", "k-9")
    client = FastCRWClient(load_config())
    try:
        assert client._server_auth() == {"Authorization": "Bearer k-9"}
    finally:
        pass  # no connections opened; async close not required

    monkeypatch.delenv("OMNISEARCH_DISCOVERY_SERVER_API_KEY")
    client2 = FastCRWClient(load_config())
    assert client2._server_auth() == {}


def test_parse_firecrawl_search_shape():
    """`crw serve /v1/search` returns the nested Firecrawl envelope."""
    payload = {"success": True, "data": {"results": [
        {"url": "https://a.example/page", "title": "A", "description": "d"},
        {"url": "not-a-url", "title": "skip me"},
    ]}}
    client = FastCRWClient(load_config())
    results = client._parse_results(payload, "server", "q")
    assert len(results) == 1
    assert results[0].url == "https://a.example/page"
    assert results[0].title == "A"
    assert results[0].engine == "server"


def test_parse_cli_and_searxng_shapes():
    """Older shapes keep working: top-level results / list / searxng."""
    client = FastCRWClient(load_config())
    top = {"results": [{"url": "https://b.example/", "snippet": "s"}]}
    assert client._parse_results(top, "cli", "q")[0].snippet == "s"
    as_list = [{"link": "https://c.example/", "title": "C"}]
    got = client._parse_results(as_list, "searxng", "q")
    assert got[0].url == "https://c.example/" and got[0].engine == "searxng"


def test_auto_mode_unreachable_server_is_noted(monkeypatch):
    """A dead server_base_url must surface in report.notes even in auto mode.

    On Render a missing/misconfigured OMNISEARCH_DISCOVERY_SERVER_BASE_URL
    used to fail silently (notes only appeared when mode was forced to
    "server"), leaving /api/health with server=false and notes=[].
    """
    import asyncio

    monkeypatch.setenv("OMNISEARCH_DISCOVERY_SERVER_BASE_URL",
                       "http://127.0.0.1:9")
    client = FastCRWClient(load_config())
    report = asyncio.run(client.detect())
    assert report.server is False
    assert any("crw server unreachable at http://127.0.0.1:9" in n
               for n in report.notes), report.notes
