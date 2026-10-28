#!/usr/bin/env python3
"""Configura Hermes Agent per Snowflake Cortex dentro SPCS.

Perche' patchare invece di riscrivere: l'installer di Hermes genera un
config.yaml da ~99KB con 23 sezioni di default documentate (compression,
prompt_caching, agent, platform_toolsets, ...). Sostituirlo con un file
minimale butterebbe via tutte quelle impostazioni, quindi qui si modificano
solo le chiavi necessarie, preservando commenti e ordine via ruamel.yaml.

Va eseguito con l'interprete del venv di Hermes, che ha ruamel:
    /usr/local/lib/hermes-agent/venv/bin/python /opt/hermes_configure.py

Idempotente: riscrive solo se il marker di versione non e' presente, a meno
di --force.
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
STANDARD_PROXY_ORIGIN = os.environ.get(
    "STANDARD_PROXY_ORIGIN", "http://llm-proxy-standard.namespace.svc.spcs.internal:8080"
).rstrip("/")
OLLAMA_URL = os.environ.get(
    "OLLAMA_HOST", "http://ollama-service:11434"  # override with OLLAMA_HOST
).rstrip("/") + "/v1"

DEFAULT_MODEL = "openai-gpt-6-astra"

OLLAMA_MODELS = {"muse-glimmer:30b": 128000}

PROVIDER_STANDARD_CHAT = "snowflake-standard-chat"
PROVIDER_STANDARD_RESPONSES = "snowflake-standard-responses"
PROVIDER_STANDARD_MESSAGES = "snowflake-standard-messages"
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

    # Shared multi-API Standard Proxy (snowflake-proxy-cortexrestapi) providers
    # with dynamic catalog discovery (discover_models: True) across all 3 APIs:
    for slug, display, suffix, mode, default_model, ctx_len in (
        (PROVIDER_STANDARD_RESPONSES, "Snowflake-Standard-Responses", "/responses/v1", "codex_responses", "openai-gpt-6-astra", 1050000),
        (PROVIDER_STANDARD_CHAT, "Snowflake-Standard-Chat", "/chat/v1", "chat_completions", "openai-gpt-6-luna", None),
        (PROVIDER_STANDARD_MESSAGES, "Snowflake-Standard-Messages", "/messages", "anthropic_messages", "claude-opus-5-5", None),
    ):
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
    # I cold start su GPU sono lenti: il default fallirebbe per timeout.
    ollama["request_timeout_seconds"] = 600
    ollama["models"] = build_models(OLLAMA_MODELS, ruamel_map)
    providers[PROVIDER_OLLAMA] = ollama

    return providers


ENV_PATH = "/root/.hermes/.env"


def migrate_terminal_cwd():
    """Sposta TERMINAL_CWD da .env a config.yaml (Hermes lo segnala come deprecato).

    Ritorna il valore trovato, o None se non c'e' nulla da migrare. Commenta la riga
    nel .env cosi' l'avviso non ricompare ad ogni avvio.
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
        # Accetta anche "export TERMINAL_CWD=..." e spazi attorno all'uguale.
        pfx = "export "
        candidate = stripped[len(pfx):].strip() if stripped.startswith(pfx) else stripped
        key = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
        if key == "TERMINAL_CWD" and not stripped.startswith("#"):
            value = candidate.split("=", 1)[1].strip().strip("\"'")
            out.append("# migrato in config.yaml (terminal.cwd): " + line)
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


# Valori che Hermes NON considera un cwd esplicito: con uno di questi (o con la
# chiave assente) l'avviso di deprecazione ricompare. Fonte:
# hermes_cli/config.py::warn_deprecated_cwd_env_vars.
CWD_NON_ESPLICITI = {".", "auto", "cwd", ""}

# Fallback quando non c'e' nulla da cui dedurre il path: /root e' la home del
# container ed e' su volume persistente.
CWD_DEFAULT = "/root"


def leggi_terminal_cwd(cfg):
    """Ritorna terminal.cwd dal config, o None se assente/non valido."""
    terminal = cfg.get("terminal")
    if not isinstance(terminal, dict):
        return None
    value = terminal.get("cwd")
    return value if isinstance(value, str) else None


def risolvi_terminal_cwd(cfg, cwd_migrato):
    """Decide il valore di terminal.cwd da scrivere.

    Perche' serve: warn_deprecated_cwd_env_vars avvisa quando TERMINAL_CWD e'
    nel PROCESS env (non nel file .env, malgrado il testo del messaggio dica
    "found in .env") E terminal.cwd non e' un path esplicito. Hermes stesso
    fa il bridge terminal.cwd -> TERMINAL_CWD, quindi la variabile resta
    nell'ambiente comunque: l'unica leva che spegne l'avviso in modo stabile e'
    avere un terminal.cwd esplicito in config.yaml.

    La versione precedente scriveva terminal.cwd solo quando trovava una riga
    TERMINAL_CWD ancora attiva nel .env. Al primo giro la riga veniva
    commentata, quindi dal secondo giro in poi cwd_migrato era None e
    terminal.cwd non veniva piu' scritto: l'avviso tornava ad ogni avvio.
    Qui il valore viene garantito ad ogni esecuzione, in ordine di preferenza:
    valore migrato dal .env, TERMINAL_CWD gia' nell'ambiente, valore esplicito
    gia' in config, infine CWD_DEFAULT.
    """
    for candidato in (
        cwd_migrato,
        os.environ.get("TERMINAL_CWD"),
        leggi_terminal_cwd(cfg),
    ):
        if isinstance(candidato, str):
            candidato = candidato.strip()
            if candidato and candidato not in CWD_NON_ESPLICITI:
                return candidato
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
        ],
        help="provider to activate as the default",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--force", action="store_true", help="riscrive anche se il marker c'e' gia'"
    )
    args = parser.parse_args()

    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.comments import CommentedMap
    except ImportError:
        sys.exit(
            "ruamel.yaml non disponibile: eseguire con "
            "/usr/local/lib/hermes-agent/venv/bin/python"
        )

    if not os.path.exists(CONFIG_PATH):
        sys.exit("config non trovato in %s" % CONFIG_PATH)

    yaml = YAML()
    yaml.preserve_quotes = True
    # Il config di default ha righe lunghe di commento: senza questo ruamel le
    # riavvolge e il diff diventa illeggibile.
    yaml.width = 4096

    with open(CONFIG_PATH) as fh:
        cfg = yaml.load(fh)

    # Va fatta comunque: se il .env contiene ancora TERMINAL_CWD, Hermes stampa un
    # avviso di deprecazione ad ogni avvio anche quando il config e' già alla versione.
    cwd_migrato = migrate_terminal_cwd()
    cwd_desiderato = risolvi_terminal_cwd(cfg, cwd_migrato)
    cwd_da_scrivere = (
        cwd_desiderato if cwd_desiderato != leggi_terminal_cwd(cfg) else None
    )

    if (
        cfg.get(MARKER_KEY) == MARKER_VALUE
        and not args.force
        and cwd_migrato is None
        and cwd_da_scrivere is None
    ):
        print("config già alla versione %s — nessuna modifica" % MARKER_VALUE)
        return

    shutil.copy2(CONFIG_PATH, "%s.bak.%d" % (CONFIG_PATH, int(time.time())))

    if cwd_da_scrivere is not None:
        terminal = cfg.get("terminal")
        if terminal is None:
            terminal = CommentedMap()
            cfg["terminal"] = terminal
        terminal["cwd"] = cwd_da_scrivere
        if cwd_migrato is not None:
            print("TERMINAL_CWD=%r migrato da .env a terminal.cwd" % cwd_migrato)
        else:
            print("terminal.cwd impostato a %r (silenzia l'avviso di deprecazione)"
                  % cwd_da_scrivere)

    model = cfg.get("model")
    if model is None:
        model = CommentedMap()
        cfg["model"] = model

    providers = build_providers(CommentedMap)
    active = providers[args.provider]

    model["provider"] = args.provider
    model["default"] = args.model
    # Per un provider named vince sempre providers.<slug>.base_url: model.base_url
    # viene ignorato. Lo allineiamo comunque per non lasciare nel config un
    # riferimento a openrouter.ai che trae in inganno chi lo legge.
    model["base_url"] = active["base_url"]

    # Un model.context_length globale avrebbe priorita' su quello per-modello
    # (step 0 contro step 0c) e resterebbe sbagliato cambiando modello.
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
        "config patchato: provider=%s model=%s base_url=%s"
        % (args.provider, args.model, active["base_url"])
    )


if __name__ == "__main__":
    main()
