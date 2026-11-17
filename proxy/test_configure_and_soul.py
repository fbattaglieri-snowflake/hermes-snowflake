"""Unit tests for hermes_configure.py (Standard + AI Gateway + Fallback) and migrate_soul.py."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _load_module(name: str, rel_path: str):
    path = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hermes_configure_standard_providers(monkeypatch):
    monkeypatch.delenv("STANDARD_PROXY_ORIGIN", raising=False)
    monkeypatch.delenv("AIGATEWAY_PROXY_ORIGIN", raising=False)
    cfg_mod = _load_module("hermes_configure", "docker/hermes/hermes_configure.py")
    providers = cfg_mod.build_providers(dict)
    assert set(providers.keys()) == {
        "snowflake-standard-responses",
        "snowflake-standard-chat",
        "snowflake-standard-messages",
        "ollama-spcs",
    }
    resp = providers["snowflake-standard-responses"]
    assert resp["api_mode"] == "codex_responses"
    assert resp["discover_models"] is True
    assert resp["context_length"] == 1050000
    assert resp["api_key"] == "internal-spcs"
    assert resp["base_url"].endswith("/responses/v1")

    chat = providers["snowflake-standard-chat"]
    assert chat["api_mode"] == "chat_completions"
    assert chat["discover_models"] is True
    assert chat["base_url"].endswith("/chat/v1")

    msgs = providers["snowflake-standard-messages"]
    assert msgs["api_mode"] == "anthropic_messages"
    assert msgs["discover_models"] is True
    assert msgs["base_url"].endswith("/messages")


def test_hermes_configure_aigateway_only_when_selected():
    cfg_mod = _load_module("hermes_configure_aigw", "docker/hermes/hermes_configure.py")
    providers = cfg_mod.build_providers(
        dict,
        selected_provider="snowflake-aigateway-messages",
        standard_origin="",
        aigateway_origin="http://llm-proxy-aigateway.core.svc.spcs.internal:8080",
    )
    assert set(providers.keys()) == {
        "snowflake-aigateway-messages",
        "snowflake-aigateway-responses",
        "snowflake-aigateway-chat",
        "ollama-spcs",
    }
    gw_msgs = providers["snowflake-aigateway-messages"]
    assert gw_msgs["api_mode"] == "anthropic_messages"
    assert gw_msgs["api_key"] == "client:hermes-spcs"
    assert gw_msgs["discover_models"] is True
    assert gw_msgs["base_url"] == "http://llm-proxy-aigateway.core.svc.spcs.internal:8080/messages"

    gw_resp = providers["snowflake-aigateway-responses"]
    assert gw_resp["context_length"] == 1050000
    assert gw_resp["api_key"] == "client:hermes-spcs"


def test_hermes_configure_dual_proxy_cohosted():
    cfg_mod = _load_module("hermes_configure_dual", "docker/hermes/hermes_configure.py")
    providers = cfg_mod.build_providers(
        dict,
        selected_provider="snowflake-aigateway-messages",
        standard_origin="http://llm-proxy-standard.core.svc.spcs.internal:8080",
        aigateway_origin="http://llm-proxy-aigateway.core.svc.spcs.internal:8080",
    )
    assert set(providers.keys()) == {
        "snowflake-aigateway-messages",
        "snowflake-aigateway-responses",
        "snowflake-aigateway-chat",
        "snowflake-standard-responses",
        "snowflake-standard-chat",
        "snowflake-standard-messages",
        "ollama-spcs",
    }


def test_hermes_configure_legacy_fallback_when_selected(monkeypatch):
    monkeypatch.delenv("STANDARD_PROXY_ORIGIN", raising=False)
    monkeypatch.delenv("AIGATEWAY_PROXY_ORIGIN", raising=False)
    cfg_mod = _load_module("hermes_configure_legacy", "docker/hermes/hermes_configure.py")
    providers = cfg_mod.build_providers(dict, selected_provider="snowflake-cortex-proxy")
    assert "snowflake-cortex-proxy" in providers
    assert providers["snowflake-cortex-proxy"]["base_url"] == "http://127.0.0.1:8080/v1"


def test_migrate_soul_idempotent(tmp_path):
    soul_mod = _load_module("migrate_soul", "docker/hermes/migrate_soul.py")
    soul_file = tmp_path / "SOUL.md"
    soul_file.write_text("# My Custom Agent Persona\n", encoding="utf-8")

    changed = soul_mod.migrate(soul_file)
    assert changed is True
    text = soul_file.read_text(encoding="utf-8")
    assert "# My Custom Agent Persona" in text
    assert "<!-- spcs-telegram-v2 -->" in text

    # Second run is a no-op
    assert soul_mod.migrate(soul_file) is False


def test_migrate_soul_upgrades_legacy_block(tmp_path):
    soul_mod = _load_module("migrate_soul_upgrade", "docker/hermes/migrate_soul.py")
    soul_file = tmp_path / "SOUL.md"
    soul_file.write_text("# Header\n" + soul_mod.LEGACY_BLOCK, encoding="utf-8")

    assert soul_mod.migrate(soul_file) is True
    text = soul_file.read_text(encoding="utf-8")
    assert "<!-- spcs-telegram-v1 -->" not in text
    assert "<!-- spcs-telegram-v2 -->" in text


def test_migrate_soul_preserves_customized_legacy_block(tmp_path):
    soul_mod = _load_module("migrate_soul_custom", "docker/hermes/migrate_soul.py")
    soul_file = tmp_path / "SOUL.md"
    soul_file.write_text(
        "# Header\n<!-- spcs-telegram-v1 -->\nCustom user edits\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Customized legacy Telegram block"):
        soul_mod.migrate(soul_file)
