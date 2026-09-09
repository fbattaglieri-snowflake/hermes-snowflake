#!/usr/bin/env python3
"""Configure Hermes Agent for Snowflake Cortex inside SPCS.

Why patch instead of rewrite: the Hermes installer generates a ~99KB
config.yaml with 23 documented default sections (compression,
prompt_caching, agent, platform_toolsets, ...). Replacing it with a minimal
file would throw away all of those settings, so here we only modify the
keys we need, preserving comments and ordering via ruamel.yaml.

Must be run with the interpreter from Hermes' venv, which has ruamel:
    /usr/local/lib/hermes-agent/venv/bin/python /opt/hermes_configure.py

Idempotent: rewrites only if the version marker is absent, unless
--force is given.
"""
import argparse
import json
import os
import shutil
import sys
import time

CONFIG_PATH = "/root/.hermes/config.yaml"
MARKER_KEY = "x_spcs_config_version"
MARKER_VALUE = "v9"

SNOWFLAKE_HOST = os.environ.get(
    "SNOWFLAKE_HOST", "localhost"  # overridden by SNOWFLAKE_HOST env var injected by SPCS
)
CORTEX_URL = "https://%s/api/v2/cortex/v1" % SNOWFLAKE_HOST
PROXY_URL = "http://127.0.0.1:8080/v1"
OLLAMA_URL = os.environ.get(
    "OLLAMA_HOST", "http://ollama-service:11434"  # override with OLLAMA_HOST
).rstrip("/") + "/v1"

DEFAULT_MODEL = "claude-sonnet-5"

# Single source of truth for the models, shared with cortex_proxy.py. The list has
# been verified with real calls: see cortex_models.json, which also documents the
# models that were tried and are NOT available on this account.
MODELS_PATH = os.environ.get("CORTEX_MODELS_PATH", "/opt/cortex_models.json")

FALLBACK_MODELS = {"claude-sonnet-5": 1000000, "claude-opus-5": 1000000}


def load_cortex_models():
    try:
        with open(MODELS_PATH) as fh:
            models = json.load(fh)["models"]
        if models:
            return {str(k): int(v) for k, v in models.items()}
    except Exception as err:
        print("warning: %s unreadable (%s), falling back to defaults" % (MODELS_PATH, err))
    return dict(FALLBACK_MODELS)


CORTEX_MODELS = load_cortex_models()

OLLAMA_MODELS = {"muse-glimmer:30b": 128000}

# The SPCS session token is rotated on the filesystem. key_cmd re-reads it per
# request, but with "bare" output Hermes would cache it for 15 minutes and
# requests would fail after a rotation: the helper emits JSON with a short
# expires_in, so the token gets refreshed often.
SESSION_TOKEN_CMD = "/opt/spcs_token.sh"

# From SPCS the Cortex REST API only accepts OAuth, and it requires this header,
# which the OpenAI SDK does not send on its own.
OAUTH_HEADER = {"X-Snowflake-Authorization-Token-Type": "OAUTH"}

PROVIDER_DIRECT = "snowflake-cortex"
PROVIDER_PROXY = "snowflake-cortex-proxy"
PROVIDER_OLLAMA = "ollama-spcs"


def build_models(mapping, ruamel_map):
    out = ruamel_map()
    for name in sorted(mapping):
        entry = ruamel_map()
        entry["context_length"] = mapping[name]
        out[name] = entry
    return out


def build_providers(ruamel_map):
    providers = ruamel_map()

    # Path through the proxy: this is the DEFAULT because it is the only one that
    # works with the Cortex models. Hermes sends 'max_tokens', which Cortex rejects
    # with HTTP 400 ("deprecated in favor of max_completion_tokens"), and it picks
    # the new key only for the OpenAI families (gpt-4o/gpt-4.1/gpt-5/o1/o3/o4, see
    # model_forces_max_completion_tokens in utils.py). For claude-*, mistral-*,
    # qwen3-* and similar the request would always fail, and Hermes would report
    # that error as "Context length exceeded (N tokens)". The proxy renames the
    # parameter and adds the OAUTH header.
    proxy = ruamel_map()
    proxy["name"] = "Snowflake Cortex (local proxy :8080)"
    proxy["base_url"] = PROXY_URL
    proxy["api_mode"] = "chat_completions"
    proxy["api_key"] = "spcs-proxy"
    proxy["context_length"] = 128000
    proxy["models"] = build_models(CORTEX_MODELS, ruamel_map)
    providers[PROVIDER_PROXY] = proxy

    # Direct path: no intermediate process, but usable ONLY with models whose name
    # triggers max_completion_tokens on the Hermes side.
    # Kept for diagnostics and for a possible future alignment of the wire format.
    direct = ruamel_map()
    direct["name"] = "Snowflake Cortex (direct — requires max_completion_tokens models)"
    direct["base_url"] = CORTEX_URL
    direct["api_mode"] = "chat_completions"
    direct["key_cmd"] = SESSION_TOKEN_CMD
    direct["extra_headers"] = ruamel_map(OAUTH_HEADER)
    direct["context_length"] = 128000
    direct["models"] = build_models(CORTEX_MODELS, ruamel_map)
    providers[PROVIDER_DIRECT] = direct

    ollama = ruamel_map()
    ollama["name"] = "Ollama SPCS (GPU pool)"
    ollama["base_url"] = OLLAMA_URL
    ollama["api_mode"] = "chat_completions"
    ollama["api_key"] = "ollama"
    ollama["context_length"] = 128000
    # GPU cold starts are slow: the default would fail with a timeout.
    ollama["request_timeout_seconds"] = 600
    ollama["models"] = build_models(OLLAMA_MODELS, ruamel_map)
    providers[PROVIDER_OLLAMA] = ollama

    return providers


ENV_PATH = "/root/.hermes/.env"


def migrate_terminal_cwd():
    """Move TERMINAL_CWD from .env to config.yaml (Hermes flags it as deprecated).

    Returns the value found, or None if there is nothing to migrate. Comments out the
    line in .env so the warning does not reappear on every startup.
    """
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
        # Also accepts "export TERMINAL_CWD=..." and spaces around the equals sign.
        pfx = "export "
        candidate = stripped[len(pfx):].strip() if stripped.startswith(pfx) else stripped
        key = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
        if key == "TERMINAL_CWD" and not stripped.startswith("#"):
            value = candidate.split("=", 1)[1].strip().strip("\"'")
            out.append("# migrated into config.yaml (terminal.cwd): " + line)
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


# Values that Hermes does NOT consider an explicit cwd: with one of these (or with
# the key absent) the deprecation warning reappears. Source:
# hermes_cli/config.py::warn_deprecated_cwd_env_vars.
CWD_NON_EXPLICIT = {".", "auto", "cwd", ""}

# Fallback when there is nothing to infer the path from: /root is the container's
# home and it lives on a persistent volume.
CWD_DEFAULT = "/root"


def read_terminal_cwd(cfg):
    """Return terminal.cwd from the config, or None if absent/invalid."""
    terminal = cfg.get("terminal")
    if not isinstance(terminal, dict):
        return None
    value = terminal.get("cwd")
    return value if isinstance(value, str) else None


def resolve_terminal_cwd(cfg, migrated_cwd):
    """Decide the terminal.cwd value to write.

    Why this is needed: warn_deprecated_cwd_env_vars warns when TERMINAL_CWD is
    in the PROCESS env (not in the .env file, despite the message text saying
    "found in .env") AND terminal.cwd is not an explicit path. Hermes itself
    bridges terminal.cwd -> TERMINAL_CWD, so the variable stays in the
    environment regardless: the only lever that reliably silences the warning is
    having an explicit terminal.cwd in config.yaml.

    The previous version wrote terminal.cwd only when it found a TERMINAL_CWD
    line still active in .env. On the first run that line got commented out, so
    from the second run onwards migrated_cwd was None and terminal.cwd was no
    longer written: the warning came back on every startup.
    Here the value is guaranteed on every run, in order of preference:
    value migrated from .env, TERMINAL_CWD already in the environment, explicit
    value already in the config, and finally CWD_DEFAULT.
    """
    for candidate in (
        migrated_cwd,
        os.environ.get("TERMINAL_CWD"),
        read_terminal_cwd(cfg),
    ):
        if isinstance(candidate, str):
            candidate = candidate.strip()
            if candidate and candidate not in CWD_NON_EXPLICIT:
                return candidate
    return CWD_DEFAULT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider",
        default=PROVIDER_PROXY,
        choices=[PROVIDER_DIRECT, PROVIDER_PROXY, PROVIDER_OLLAMA],
        help="provider to activate as the default",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--force", action="store_true", help="rewrite even if the marker is already there"
    )
    args = parser.parse_args()

    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.comments import CommentedMap
    except ImportError:
        sys.exit(
            "ruamel.yaml not available: run with "
            "/usr/local/lib/hermes-agent/venv/bin/python"
        )

    if not os.path.exists(CONFIG_PATH):
        sys.exit("config not found at %s" % CONFIG_PATH)

    yaml = YAML()
    yaml.preserve_quotes = True
    # The default config has long comment lines: without this ruamel rewraps them
    # and the diff becomes unreadable.
    yaml.width = 4096

    with open(CONFIG_PATH) as fh:
        cfg = yaml.load(fh)

    # Must be done regardless: if .env still contains TERMINAL_CWD, Hermes prints a
    # deprecation warning on every startup even when the config is already at version.
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
        print("config already at version %s — no changes" % MARKER_VALUE)
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
            print("terminal.cwd set to %r (silences the deprecation warning)"
                  % cwd_to_write)

    model = cfg.get("model")
    if model is None:
        model = CommentedMap()
        cfg["model"] = model

    providers = build_providers(CommentedMap)
    active = providers[args.provider]

    model["provider"] = args.provider
    model["default"] = args.model
    # For a named provider, providers.<slug>.base_url always wins: model.base_url
    # is ignored. We align it anyway so as not to leave a reference to
    # openrouter.ai in the config, which misleads whoever reads it.
    model["base_url"] = active["base_url"]

    # A global model.context_length would take priority over the per-model one
    # (step 0 versus step 0c) and would stay wrong when switching models.
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
