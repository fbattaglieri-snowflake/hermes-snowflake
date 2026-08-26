#!/usr/bin/env python3
"""Reverifies the Cortex model catalog and rewrites proxy/models.json.

Why this exists: models.json is the single source of truth for cortex_proxy.py
(which builds the /v1/models endpoint that the Cortex gateway does not offer)
and for hermes_configure.py. The Snowflake catalog changes — new models, EOL models,
models listed but not actually reachable from a given account — and the catalog alone
cannot be trusted: SHOW CORTEX BASE MODELS lists models that return HTTP 400
'unknown model'. Each name must be tested with a real call.

What it does:
  1. reads the catalog with SHOW CORTEX BASE MODELS IN SCHEMA SNOWFLAKE.MODELS
  2. for each candidate name, makes a real call to /chat/completions
  3. for responding models, checks whether tool calling requires
     reasoning_effort="none" (constraint of the gpt-5.6 family)
  4. rewrites models.json: known context windows are preserved,
     new models get a conservative default to be reviewed manually
  5. with --upload, re-uploads the file to the stage (reloaded hot by the service)

Usage:
    export CORTEX_PAT="$(cortex secret get hermes-cortex-pat)"   # or --pat-file
    python3 refresh_cortex_models.py                 # dry-run, report only
    python3 refresh_cortex_models.py --write         # update models.json
    python3 refresh_cortex_models.py --write --upload  # and reload to stage

The context window is NOT probed: doing so would require sending hundreds of thousands
of tokens per model. Values come from the documentation; for a new model the script
sets DEFAULT_CONTEXT and flags it in the report. Under-estimating is safe (the client
compresses before needed); over-estimating breaks calls.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_FILE = os.environ.get("CORTEX_MODELS_PATH", os.path.join(HERE, "..", "proxy", "models.json"))

DEFAULT_CONNECTION = os.environ.get("SNOWFLAKE_CONNECTION", "default")
DEFAULT_HOST = os.environ.get("SNOWFLAKE_HOST", "localhost")
DEFAULT_STAGE = "@n8n_platform.core.cortex_config"
DEFAULT_CONTEXT = 128000

REASONING_TOOLS_ERROR = "function tools with reasoning_effort"

# Ordine delle famiglie nel file riscritto: tenerlo stabile rende i diff leggibili.
FAMILY_ORDER = ["claude-opus", "claude-sonnet", "claude-haiku", "claude-4",
                "openai-gpt", "mistral", "llama", "snowflake"]

# Un tool banale ma valido: serve solo a far scattare (o no) il vincolo
# tools + reasoning_effort. Non ci interessa la risposta del modello.
PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Return the current time.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def die(msg):
    sys.stderr.write("errore: %s\n" % msg)
    sys.exit(1)


def read_pat(args):
    if args.pat_file:
        with open(args.pat_file) as fh:
            return fh.read().strip()
    pat = os.environ.get("CORTEX_PAT", "").strip()
    if not pat:
        die("serve il PAT: esporta CORTEX_PAT oppure passa --pat-file")
    return pat


def catalog_lifecycle(connection):
    """{nome: {status, eol}} dal catalogo, piu' le varianti plausibili del nome.

    Il catalogo usa il maiuscolo e per i modelli first-party inserisce un segmento
    '1p-' che NON fa parte del nome invocabile: OPENAI-1P-GPT-5.6-LUNA si chiama
    openai-gpt-5.6-luna sul gateway. Registriamo entrambe le forme e lasciamo
    decidere alla chiamata reale quale delle due risponde.

    lifecycle_status/eol_date servono a segnalare i modelli LEGACY: rispondono oggi
    ma spariscono a una data nota, e metterli in config senza avvisare significa
    ritrovarsi con un provider rotto il giorno dell'EOL.
    """
    out = subprocess.run(
        ["snow", "sql", "-c", connection, "--format", "json",
         "-q", "SHOW CORTEX BASE MODELS IN SCHEMA SNOWFLAKE.MODELS"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        die("SHOW CORTEX BASE MODELS ha fallito:\n%s" % (out.stderr or out.stdout))

    try:
        rows = json.loads(out.stdout)
    except json.JSONDecodeError:
        die("output di snow sql non interpretabile come JSON:\n%s" % out.stdout[:500])
    if rows and isinstance(rows[0], list):   # snow sql annida per statement
        rows = rows[0]

    lifecycle = {}
    for row in rows:
        raw = row.get("name") or row.get("NAME")
        if not raw:
            continue
        name = str(raw).strip().lower()
        meta = {
            "status": (row.get("lifecycle_status") or "").upper(),
            "eol": row.get("eol_date") or "",
        }
        lifecycle[name] = meta
        stripped = re.sub(r"-1p-", "-", name)
        if stripped != name:
            lifecycle.setdefault(stripped, meta)
    return lifecycle


def call_gateway(host, pat, payload, timeout=90):
    """(status, body_text). Non solleva su 4xx/5xx: lo stato ci serve."""
    req = urllib.request.Request(
        "https://%s/api/v2/cortex/v1/chat/completions" % host,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer %s" % pat,
            "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:                              # rete, DNS, timeout
        return 0, "eccezione locale: %s" % exc


def short_reason(status, body):
    """Riduce il corpo dell'errore a una riga da mettere in _non_disponibili."""
    text = body.strip()
    try:
        parsed = json.loads(text)
        text = str(parsed.get("message") or parsed.get("error") or text)
    except json.JSONDecodeError:
        pass
    text = " ".join(text.split())
    if status == 403:
        return "HTTP 403 - account non autorizzato"
    if "unknown model" in text.lower():
        return "unknown model"
    if "unavailable" in text.lower():
        return "unavailable"
    return "HTTP %s: %s" % (status, text[:160])


def probe(host, pat, model):
    """Ritorna dict con esito del modello.

    Due chiamate: la prima stabilisce se il modello risponde, la seconda (solo se la
    prima e' andata) se il tool calling richiede reasoning_effort="none".
    Nota: si usa max_completion_tokens, non max_tokens — il gateway rifiuta
    max_tokens per tutte le famiglie, ed e' esattamente cio' che il proxy riscrive.
    """
    base = {
        "model": model,
        "max_completion_tokens": 16,
        "messages": [{"role": "user", "content": "ping"}],
    }
    status, body = call_gateway(host, pat, base)
    if status != 200:
        return {"model": model, "ok": False, "reason": short_reason(status, body)}

    result = {"model": model, "ok": True, "needs_reasoning_none": False}

    with_tools = dict(base, tools=[PROBE_TOOL])
    status, body = call_gateway(host, pat, with_tools)
    if status == 200:
        return result
    if REASONING_TOOLS_ERROR in body.lower():
        # Riprova forzando "none": se passa, il modello va nella lista dei vincolati.
        status, _ = call_gateway(host, pat, dict(with_tools, reasoning_effort="none"))
        result["needs_reasoning_none"] = status == 200
        if status != 200:
            result["tools_note"] = "tools rifiutati anche con reasoning_effort=none"
        return result

    # Tools rifiutati per altri motivi: il modello resta usabile per il testo.
    result["tools_note"] = short_reason(status, body)
    return result


def family_key(name):
    for index, prefix in enumerate(FAMILY_ORDER):
        if name.startswith(prefix):
            return (index, name)
    return (len(FAMILY_ORDER), name)


def render(doc):
    """Serializza a mano per conservare il raggruppamento per famiglia.

    json.dump appiattirebbe tutto in un blocco unico: con 25+ modelli il file
    diventa illeggibile e i diff inutili. Le chiavi '_'-prefissate sono commenti.
    """
    def dumps(value, indent):
        text = json.dumps(value, indent=2, ensure_ascii=False)
        pad = " " * indent
        return text.replace("\n", "\n" + pad)

    lines = ["{"]
    lines.append('  "_commento": %s,' % dumps(doc["_commento"], 2))
    lines.append("")
    lines.append('  "models": {')

    entries, previous = [], None
    for name in sorted(doc["models"], key=family_key):
        family = family_key(name)[0]
        if previous is not None and family != previous:
            entries.append("")                         # riga vuota fra famiglie
        entries.append('    "%s": %d,' % (name, doc["models"][name]))
        previous = family
    if entries:
        entries[-1] = entries[-1].rstrip(",")          # niente virgola sull'ultimo
    lines.extend(entries)

    lines.append("  },")
    lines.append("")
    lines.append('  "tools_require_reasoning_effort_none": %s,'
                 % dumps(sorted(doc["tools_require_reasoning_effort_none"]), 2))

    if doc.get("tools_unsupported"):
        lines.append("")
        lines.append('  "tools_unsupported": %s,' % dumps(sorted(doc["tools_unsupported"]), 2))

    for key in ("_note_tools_reasoning", "_note_context"):
        if key in doc:
            lines.append("")
            lines.append('  "%s": %s,' % (key, dumps(doc[key], 2)))

    if doc.get("_legacy"):
        lines.append("")
        lines.append('  "_legacy": %s,' % dumps(doc["_legacy"], 2))

    lines.append("")
    lines.append('  "_non_disponibili": %s' % dumps(doc["_non_disponibili"], 2))
    lines.append("}")
    return "\n".join(lines) + "\n"


def upload(connection, stage):
    out = subprocess.run(
        ["snow", "stage", "copy", MODELS_FILE, stage, "-c", connection, "--overwrite"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        die("upload sullo stage ha fallito:\n%s" % (out.stderr or out.stdout))
    print("uploaded to %s" % stage)
    print("the service reloads the file within ~5 min (metadataCache); to apply immediately:")
    print("  ALTER SERVICE <DATABASE>.<SCHEMA>.HERMES_SERVICE SUSPEND;")
    print("  ALTER SERVICE <DATABASE>.<SCHEMA>.HERMES_SERVICE RESUME;")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--connection", default=DEFAULT_CONNECTION)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--stage", default=DEFAULT_STAGE)
    parser.add_argument("--pat-file", help="file contenente il PAT")
    parser.add_argument("--write", action="store_true", help="riscrive cortex_models.json")
    parser.add_argument("--upload", action="store_true",
                        help="ricarica sullo stage (implica --write)")
    parser.add_argument("--jobs", type=int, default=4, help="chiamate in parallelo")
    args = parser.parse_args()
    if args.upload:
        args.write = True

    pat = read_pat(args)
    with open(MODELS_FILE) as fh:
        doc = json.load(fh)
    known = dict(doc["models"])

    lifecycle = catalog_lifecycle(args.connection)
    candidates = set(lifecycle)
    # I nomi gia' in config vanno riprovati comunque: uno puo' essere andato EOL, e
    # un alias invocabile puo' non comparire affatto nel catalogo.
    candidates.update(known)
    candidates = sorted(candidates)
    print("candidati da provare: %d\n" % len(candidates))

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda m: probe(args.host, pat, m), candidates))

    models, unavailable, constrained = {}, {}, []
    no_tools, added, removed, notes, legacy = [], [], [], [], []
    for res in sorted(results, key=lambda r: family_key(r["model"])):
        name = res["model"]
        if not res["ok"]:
            if name in known:
                removed.append((name, res["reason"]))
            unavailable[name] = res["reason"]
            continue
        models[name] = known.get(name, DEFAULT_CONTEXT)
        if name not in known:
            added.append(name)
        if res["needs_reasoning_none"]:
            constrained.append(name)
        if res.get("tools_note"):
            notes.append((name, res["tools_note"]))
            if "not supported" in res["tools_note"]:
                no_tools.append(name)
        meta = lifecycle.get(name, {})
        if meta.get("status") == "LEGACY":
            legacy.append((name, meta.get("eol") or "data non dichiarata"))

    legacy_names = dict(legacy)
    print("FUNZIONANTI: %d" % len(models))
    for name in sorted(models, key=family_key):
        flags = ""
        if name in constrained:
            flags += "  [tools solo con reasoning_effort=none]"
        if name in no_tools:
            flags += "  [nessun tool calling]"
        if name in legacy_names:
            flags += "  [LEGACY, EOL %s]" % legacy_names[name]
        if name in added:
            flags += "  <-- NUOVO, context da verificare a mano"
        print("  %-28s %8d%s" % (name, models[name], flags))

    if removed:
        print("\nNON PIU' DISPONIBILI (erano in config):")
        for name, reason in removed:
            print("  %-28s %s" % (name, reason))
    if notes:
        print("\nNOTE SUL TOOL CALLING:")
        for name, note in notes:
            print("  %-28s %s" % (name, note))
    if legacy:
        print("\nLEGACY: rispondono adesso ma hanno una data di morte.")
        print("Se n8n o Hermes li usa in un workflow, quel workflow si rompe all'EOL.")
    if added:
        print("\nATTENZIONE: %d modelli nuovi hanno context=%d (prudenziale)."
              % (len(added), DEFAULT_CONTEXT))
        print("Correggerlo a mano da aisql-regional-availability prima di considerarlo definitivo.")

    if not models:
        die("nessun modello ha risposto: PAT scaduto o host sbagliato? file non toccato")

    if not args.write:
        print("\ndry-run: cortex_models.json non modificato (usa --write)")
        return

    # _non_disponibili: si fondono le voci storiche con quelle rilevate ora, cosi'
    # non si perdono le annotazioni scritte a mano (es. i nomi con '1p-').
    merged = dict(doc.get("_non_disponibili", {}))
    merged.update(unavailable)
    merged["_commento"] = doc.get("_non_disponibili", {}).get(
        "_commento", "Models tested and NOT working (unknown or unavailable). "
        "Do not add them back without re-testing.")

    doc["models"] = models
    doc["tools_require_reasoning_effort_none"] = constrained
    # Non lo usa il proxy: serve a chi configura n8n. Il nodo AI Agent richiede il
    # tool calling, quindi con questi modelli va scelto un nodo LLM semplice.
    doc["tools_unsupported"] = sorted(no_tools)
    doc["_legacy"] = dict(sorted(legacy)) or {}
    doc["_non_disponibili"] = merged

    backup = MODELS_FILE + ".bak"
    os.replace(MODELS_FILE, backup)
    with open(MODELS_FILE, "w") as fh:
        fh.write(render(doc))
    json.load(open(MODELS_FILE))                          # non spedire JSON rotto
    print("\nscritto %s (backup in %s)" % (MODELS_FILE, backup))

    if args.upload:
        upload(args.connection, args.stage)


if __name__ == "__main__":
    main()
