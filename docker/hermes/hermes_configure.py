#!/usr/bin/env python3
"""Configure Hermes Agent for Snowflake Cortex inside Snowpark Container Services (SPCS).

Why patch instead of replacing: the upstream Hermes installer generates a ~99KB
`config.yaml` with 23+ documented sections (`compression`, `prompt_caching`, `agent`,
`platform_toolsets`, etc.). Replacing it with a minimal file would discard those
defaults. This script updates only the required keys while preserving comments and
key ordering via `ruamel.yaml`.

Run with the Hermes virtualenv Python interpreter:
    /usr/local/lib/hermes-agent/venv/bin/python /opt/hermes_configure.py

Idempotent: rewrites only when the version marker is absent or `--force` is passed.
"""
import argparse
import json
import os
import shutil
import sys
import time

CONFIG_PATH = "/root/.hermes/config.yaml"
MODELS_PATH = os.environ.get("CORTEX_MODELS_PATH", "/opt/cortex_models.json")
MARKER_KEY = "x_spcs_config_version"
MARKER_VALUE = "v10"

SNOWFLAKE_HOST = os.environ.get(
    "SNOWFLAKE_HOST", "localhost"  # overridden by SNOWFLAKE_HOST injected by SPCS
)
CORTEX_URL = "https://%s/api/v2/cortex/v1" % SNOWFLAKE_HOST
PROXY_URL = "http://127.0.0.1:8080/v1"
STANDARD_PROXY_ORIGIN = os.environ.get(
    "STANDARD_PROXY_ORIGIN",
    "http://llm-proxy-standard.core.svc.spcs.internal:8080",
).rstrip("/")
OLLAMA_URL = os.environ.get(
    "OLLAMA_HOST", "http://ollama-service:11434"
).rstrip("/") + "/v1"

DEFAULT_MODEL = "openai-gpt-6-astra"
OLLAMA_MODELS = {"muse-glimmer:30b": 128000}

PROVIDER_STANDARD_CHAT = "snowflake-standard-chat"
PROVIDER_STANDARD_RESPONSES = "snowflake-standard-responses"
PROVIDER_STANDARD_MESSAGES = "snowflake-standard-messages"
PROVIDER_OLLAMA = "ollama-spcs"
PROVIDER_LEGACY_PROXY = "snowflake-cortex-proxy"
PROVIDER_DIRECT_CORTEX = "snowflake-cortex"


def load_fallback_models():
    """Load the static fallback catalog from models.json when needed."""
    if not os.path.exists(MODELS_PATH):
        return {"claude-sonnet-5": 200000}
    try:
        with open(MODELS_PATH) as fh:
            doc = json.load(fh)
        models = doc.get("models") or {}
        return {str(k): int(v) for k, v in models.items()} or {"claude-sonnet-5": 200000}
    except (OSError, ValueError, TypeError):
        return {"claude-sonnet-5": 200000}


def build_models(mapping, ruamel_map):
    out = ruamel_map()
    for name in sorted(mapping):
        entry = ruamel_map()
        entry["context_length"] = mapping[name]
        out[name] = entry
    return out


def build_providers(ruamel_map, selected_provider=PROVIDER_STANDARD_RESPONSES):
    providers = ruamel_map()

    # Shared multi-API Standard Proxy (snowflake-proxy-cortexrestapi) providers
    # with dynamic catalog discovery (discover_models: True) across all 3 APIs:
    standard_specs = (
        (
            PROVIDER_STANDARD_RESPONSES,
            "Snowflake-Standard-Responses",
            "/responses/v1",
            "codex_responses",
            "openai-gpt-6-astra",
            1050000,
        ),
        (
            PROVIDER_STANDARD_CHAT,
            "Snowflake-Standard-Chat",
            "/chat/v1",
            "chat_completions",
            "openai-gpt-6-luna",
            None,
        ),
        (
            PROVIDER_STANDARD_MESSAGES,
            "Snowflake-Standard-Messages",
            "/messages",
            "anthropic_messages",
            "claude-opus-5-5",
            None,
        ),
    )
    for slug, display, suffix, mode, default_model, ctx_len in standard_specs:
        std = ruamel_map()
        std["name"] = display
        std["base_url"] = STANDARD_PROXY_ORIGIN + suffix
        std["api_mode"] = mode
        std["api_key"] = "internal-spcs"
        std["model"] = default_model
        if ctx_len is not None:
            std["context_length"] = ctx_len
        std["discover_models"] = True
        providers[slug] = std

    ollama = ruamel_map()
    ollama["name"] = "Ollama SPCS (GPU pool)"
    ollama["base_url"] = OLLAMA_URL
    ollama["api_mode"] = "chat_completions"
    ollama["api_key"] = "ollama"
    ollama["context_length"] = 128000
    # GPU cold starts can take up to several minutes on first load.
    ollama["request_timeout_seconds"] = 600
    ollama["models"] = build_models(OLLAMA_MODELS, ruamel_map)
    providers[PROVIDER_OLLAMA] = ollama

    # Optional single-container embedded fallback providers (registered only when
    # explicitly selected so the default Model Picker stays clean with the 3
    # Snowflake-Standard-* providers + Ollama).
    if selected_provider == PROVIDER_LEGACY_PROXY:
        fallback_models = load_fallback_models()
        legacy = ruamel_map()
        legacy["name"] = "Snowflake Cortex (Embedded Loopback Proxy)"
        legacy["base_url"] = PROXY_URL
        legacy["api_mode"] = "chat_completions"
        legacy["api_key"] = "spcs-proxy"
        legacy["context_length"] = 200000
        legacy["models"] = build_models(fallback_models, ruamel_map)
        providers[PROVIDER_LEGACY_PROXY] = legacy
    elif selected_provider == PROVIDER_DIRECT_CORTEX:
        fallback_models = load_fallback_models()
        direct = ruamel_map()
        direct["name"] = "Snowflake Cortex (Direct OAuth)"
        direct["base_url"] = CORTEX_URL
        direct["api_mode"] = "chat_completions"
        direct["key_cmd"] = "/opt/spcs_token.sh"
        direct["context_length"] = 200000
        direct["models"] = build_models(fallback_models, ruamel_map)
        providers[PROVIDER_DIRECT_CORTEX] = direct

    return providers


ENV_PATH = "/root/.hermes/.env"


def migrate_terminal_cwd():
    """Move deprecated TERMINAL_CWD from .env into config.yaml (terminal.cwd)."""
    if not os.path.exists(ENV_PATH):
        return None
    try:
        with open(ENV_PATH) as fh:
            lines = fh.readlines()
    except OSError:
        return None

    value = None
    out = []
    for line in lines:
        stripped = line.strip()
        pfx = "export "
        candidate = stripped[len(pfx):].strip() if stripped.startswith(pfx) else stripped
        key = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
        if key == "TERMINAL_CWD" and not stripped.startswith("#"):
            value = candidate.split("=", 1)[1].strip().strip("\"'")
            out.append("# migrated to config.yaml (terminal.cwd): " + line)
        else:
            out.append(line)

    if value is None:
        return None
    try:
        with open(ENV_PATH, "w") as fh:
            fh.writelines(out)
    except OSError:
        return None
    return value


NON_EXPLICIT_CWDS = {".", "auto", "cwd", ""}
CWD_DEFAULT = "/root"


def read_terminal_cwd(cfg):
    """Return terminal.cwd from config, or None if missing/invalid."""
    terminal = cfg.get("terminal")
    if not isinstance(terminal, dict):
        return None
    value = terminal.get("cwd")
    return value if isinstance(value, str) else None


def resolve_terminal_cwd(cfg, migrated_cwd):
    """Resolve the explicit terminal.cwd path to write into config.yaml."""
    for candidate in (
        migrated_cwd,
        os.environ.get("TERMINAL_CWD"),
        read_terminal_cwd(cfg),
    ):
        if isinstance(candidate, str):
            candidate = candidate.strip()
            if candidate and candidate not in NON_EXPLICIT_CWDS:
                return candidate
    return CWD_DEFAULT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider",
        default=PROVIDER_STANDARD_RESPONSES,
        choices=[
            PROVIDER_STANDARD_CHAT,
            PROVIDER_STANDARD_RESPONSES,
            PROVIDER_STANDARD_MESSAGES,
            PROVIDER_OLLAMA,
            PROVIDER_LEGACY_PROXY,
            PROVIDER_DIRECT_CORTEX,
        ],
        help="provider to activate as the default",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--force",
        action="store_true",
        help="rewrite config even when the version marker is already present",
    )
    args = parser.parse_args()

    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.comments import CommentedMap
    except ImportError:
        sys.exit(
            "ruamel.yaml is unavailable: run with "
            "/usr/local/lib/hermes-agent/venv/bin/python"
        )

    if not os.path.exists(CONFIG_PATH):
        sys.exit("config not found at %s" % CONFIG_PATH)

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096

    with open(CONFIG_PATH) as fh:
        cfg = yaml.load(fh)

    migrated_cwd = migrate_terminal_cwd()
    desired_cwd = resolve_terminal_cwd(cfg, migrated_cwd)
    cwd_to_write = (
        desired_cwd if desired_cwd != read_terminal_cwd(cfg) else None
    )

    if (
        cfg.get(MARKER_KEY) == MARKER_VALUE
        and not args.force
        and migrated_cwd is None
        and cwd_to_write is None
    ):
        print("config already at version %s — no changes needed" % MARKER_VALUE)
        return

    shutil.copy2(CONFIG_PATH, "%s.bak.%d" % (CONFIG_PATH, int(time.time())))

    if cwd_to_write is not None:
        terminal = cfg.get("terminal")
        if terminal is None:
            terminal = CommentedMap()
            cfg["terminal"] = terminal
        terminal["cwd"] = cwd_to_write
        if migrated_cwd is not None:
            print("TERMINAL_CWD=%r migrated from .env to terminal.cwd" % migrated_cwd)
        else:
            print("terminal.cwd set to %r" % cwd_to_write)

    model = cfg.get("model")
    if model is None:
        model = CommentedMap()
        cfg["model"] = model

    providers = build_providers(CommentedMap, selected_provider=args.provider)
    active = providers[args.provider]

    model["provider"] = args.provider
    model["default"] = args.model
    model["base_url"] = active["base_url"]
    model.pop("context_length", None)

    existing = cfg.get("providers")
    if existing is None:
        cfg["providers"] = providers
    else:
        for key, value in providers.items():
            existing[key] = value

    cfg[MARKER_KEY] = MARKER_VALUE

    with open(CONFIG_PATH, "w") as fh:
        yaml.dump(cfg, fh)

    print(
        "config patched: provider=%s model=%s base_url=%s"
        % (args.provider, args.model, active["base_url"])
    )


if __name__ == "__main__":
    main()
