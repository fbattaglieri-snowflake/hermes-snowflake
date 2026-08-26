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
OLLAMA_URL = os.environ.get(
    "OLLAMA_HOST", "http://ollama-service:11434"  # override with OLLAMA_HOST
).rstrip("/") + "/v1"

DEFAULT_MODEL = "claude-sonnet-5"

# Sorgente unica dei modelli, condivisa con cortex_proxy.py. Elenco verificato con
# chiamate reali: vedi cortex_models.json, che documenta anche i modelli provati e
# NON disponibili su questo account.
MODELS_PATH = os.environ.get("CORTEX_MODELS_PATH", "/opt/cortex_models.json")

FALLBACK_MODELS = {"claude-sonnet-5": 1000000, "claude-opus-5": 1000000}


def load_cortex_models():
    try:
        with open(MODELS_PATH) as fh:
            models = json.load(fh)["models"]
        if models:
            return {str(k): int(v) for k, v in models.items()}
    except Exception as err:
        print("attenzione: %s illeggibile (%s), uso il fallback" % (MODELS_PATH, err))
    return dict(FALLBACK_MODELS)


CORTEX_MODELS = load_cortex_models()

OLLAMA_MODELS = {"muse-glimmer:30b": 128000}

# Il session token SPCS viene ruotato sul filesystem. key_cmd lo rilegge per
# richiesta, ma con output "bare" Hermes lo cacherebbe 15 minuti e dopo una
# rotazione le richieste fallirebbero: l'helper emette JSON con expires_in
# breve, cosi' il token viene rinfrescato spesso.
SESSION_TOKEN_CMD = "/opt/spcs_token.sh"

# Da SPCS il Cortex REST API accetta solo OAuth, e pretende questo header che
# l'SDK OpenAI non invia di suo.
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

    # Percorso via proxy: e' il DEFAULT perche' e' l'unico che funziona con i
    # modelli Cortex. Hermes invia 'max_tokens', che Cortex rifiuta con HTTP 400
    # ("deprecated in favor of max_completion_tokens"), e sceglie la chiave nuova
    # solo per le famiglie OpenAI (gpt-4o/gpt-4.1/gpt-5/o1/o3/o4, vedi
    # model_forces_max_completion_tokens in utils.py). Per claude-*, mistral-*,
    # qwen3-* e simili la richiesta fallirebbe sempre, e Hermes riporterebbe
    # quell'errore come "Context length exceeded (N tokens)". Il proxy rinomina
    # il parametro e aggiunge l'header OAUTH.
    proxy = ruamel_map()
    proxy["name"] = "Snowflake Cortex (proxy locale :8080)"
    proxy["base_url"] = PROXY_URL
    proxy["api_mode"] = "chat_completions"
    proxy["api_key"] = "spcs-proxy"
    proxy["context_length"] = 128000
    proxy["models"] = build_models(CORTEX_MODELS, ruamel_map)
    providers[PROVIDER_PROXY] = proxy

    # Percorso diretto: nessun processo intermedio, ma utilizzabile SOLO con
    # modelli il cui nome fa scattare max_completion_tokens lato Hermes.
    # Tenuto per diagnostica e per un eventuale allineamento futuro del wire.
    direct = ruamel_map()
    direct["name"] = "Snowflake Cortex (diretto — richiede modelli max_completion_tokens)"
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
        default=PROVIDER_PROXY,
        choices=[PROVIDER_DIRECT, PROVIDER_PROXY, PROVIDER_OLLAMA],
        help="provider da attivare come default",
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
