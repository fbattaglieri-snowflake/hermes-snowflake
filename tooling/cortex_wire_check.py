#!/usr/bin/env python3
"""Verifica quali scostamenti dal wire OpenAI sono ancora presenti sul Cortex REST API.

A cosa serve: cortex_proxy.py esiste solo per aggirare cinque difetti del gateway.
Quando Snowflake ne sistema uno, il fix corrispondente nel proxy diventa inutile — e in
un caso (la lista modelli) diventa addirittura dannoso, perche' nasconderebbe i modelli
nuovi senza dare errore. Questo script dice, con chiamate reali, quali fix servono ancora.

Uso:
    CORTEX_PAT="..." python3 cortex_wire_check.py
    CORTEX_PAT="..." python3 cortex_wire_check.py --host <altro-account>.snowflakecomputing.com

Non modifica nulla: fa solo richieste di lettura/completion minime.
Confrontare l'output con il baseline registrato nell'handover
20260818_cortex_gateway_migration_playbook.md.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_HOST = os.environ.get("SNOWFLAKE_HOST", "localhost")
# Modello Claude: e' la famiglia che soffre di piu' (passa per lo strato di traduzione).
CLAUDE = "claude-sonnet-5"
# Modello con il vincolo tools+reasoning_effort.
REASONING = "openai-gpt-5.6-luna"

OK, BROKEN, UNKNOWN = "RISOLTO", "ANCORA PRESENTE", "NON DETERMINATO"
INFO = "INFORMATIVO"


def call(host, pat, path, payload=None, method=None, extra_headers=None):
    """(status, parsed_or_text). Non solleva su 4xx/5xx."""
    url = "https://%s/api/v2/cortex/v1%s" % (host, path)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Bearer %s" % pat,
    }
    headers.update(extra_headers or {})
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers=headers, method=method or ("POST" if data else "GET")
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read().decode("utf-8", "replace"), exc.code
    except Exception as exc:
        return 0, "eccezione locale: %s" % exc
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def chat(host, pat, **body):
    body.setdefault("messages", [{"role": "user", "content": "ping"}])
    return call(host, pat, "/chat/completions", body)


def first_choice(parsed):
    if isinstance(parsed, dict):
        choices = parsed.get("choices") or []
        if choices and isinstance(choices[0], dict):
            return choices[0]
    return {}


# --------------------------------------------------------------------------- test


def t1_max_tokens(host, pat):
    """Problema 1: max_tokens rifiutato. Fix nel proxy: adapt_payload()."""
    status, parsed = chat(host, pat, model=CLAUDE, max_tokens=16)
    if status == 200:
        return OK, "max_tokens accettato", "adapt_payload(): la riscrittura non serve piu'"
    msg = parsed.get("message", parsed) if isinstance(parsed, dict) else parsed
    return BROKEN, "HTTP %s — %s" % (status, str(msg)[:110]), "tenere la riscrittura"


def t2_models_list(host, pat):
    """Problema 2: GET /v1/models -> 404. Fix nel proxy: do_GET() serve la lista dal file.

    ATTENZIONE: e' l'unico fix che, se il gateway viene sistemato, diventa DANNOSO.
    Il proxy continuerebbe a servire il suo file, nascondendo i modelli nuovi in silenzio.
    """
    status, parsed = call(host, pat, "/models")
    if status == 200:
        n = len(parsed.get("data", [])) if isinstance(parsed, dict) else "?"
        return (OK, "HTTP 200, %s modelli" % n,
                "URGENTE: do_GET() va cambiato in 'prova upstream, ripiega sul file', "
                "altrimenti nasconde i modelli nuovi")
    return BROKEN, "HTTP %s" % status, "tenere la lista servita dal file"


def t3_finish_reason(host, pat):
    """Problema 3: finish_reason vuoto. Fix nel proxy: normalize_finish_reason()/stop_chunk().

    Tre sotto-casi, perche' il valore corretto e' diverso in ognuno:
    completa -> stop, tool call -> tool_calls, troncata -> length.
    Il fix attuale forza sempre 'stop': e' LOSSY, marca come completa una risposta tagliata.
    """
    results = {}

    _, parsed = chat(host, pat, model=CLAUDE, max_completion_tokens=16)
    results["completa (atteso 'stop')"] = first_choice(parsed).get("finish_reason")

    tool = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Meteo di una citta",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
    _, parsed = chat(
        host, pat, model=CLAUDE, max_completion_tokens=200, tools=[tool],
        messages=[{"role": "user", "content": "Che tempo fa a Milano? Usa il tool."}],
    )
    choice = first_choice(parsed)
    results["tool call (atteso 'tool_calls')"] = choice.get("finish_reason")
    has_tool_calls = bool((choice.get("message") or {}).get("tool_calls"))

    _, parsed = chat(
        host, pat, model=CLAUDE, max_completion_tokens=5,
        messages=[{"role": "user", "content": "Scrivi un saggio lungo sulla storia di Roma."}],
    )
    results["troncata (atteso 'length')"] = first_choice(parsed).get("finish_reason")

    detail = "; ".join("%s -> %r" % (k, v) for k, v in results.items())
    detail += "; tool_calls popolato: %s" % has_tool_calls
    values = list(results.values())

    if all(v for v in values) and values[0] == "stop":
        return OK, detail, "normalize_finish_reason() e stop_chunk() si disattivano da soli"
    if any(v for v in values):
        return UNKNOWN, detail, "parzialmente sistemato: rileggere il codice prima di toccarlo"
    return (BROKEN, detail,
            "tenere il fix, MA migliorarlo: dedurre 'tool_calls' da message.tool_calls e "
            "'length' da usage.completion_tokens >= max richiesto (vedi handover)")


def t4_tools_reasoning(host, pat):
    """Problema 4: tools + reasoning_effort incompatibili. Fix: lista + retry adattivo."""
    tool = {
        "type": "function",
        "function": {
            "name": "noop",
            "description": "Non fa nulla",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    status, parsed = chat(host, pat, model=REASONING, max_completion_tokens=16, tools=[tool])
    if status == 200:
        return (OK, "%s accetta tools senza forzare reasoning_effort" % REASONING,
                "svuotare tools_require_reasoning_effort_none in cortex_models.json "
                "(basta rilanciare refresh_cortex_models.py --write --upload)")
    msg = parsed.get("message", parsed) if isinstance(parsed, dict) else parsed
    if status == 400 and "unknown model" in str(msg).lower():
        return UNKNOWN, "%s non piu' disponibile" % REASONING, "riprovare con altro reasoning model"
    return BROKEN, "HTTP %s — %s" % (status, str(msg)[:110]), "tenere lista + retry adattivo"


def t5_responses_api(host, pat):
    """Extra: /v1/responses endpoint check."""
    status, parsed = call(host, pat, "/responses", {"model": CLAUDE, "input": "ping"})
    if status == 200:
        return OK, "HTTP 200", "/v1/responses endpoint is available"
    msg = parsed.get("message", parsed) if isinstance(parsed, dict) else parsed
    return BROKEN, "HTTP %s — %s" % (status, str(msg)[:110]), "endpoint not available"


def t6_anthropic_endpoint(host, pat):
    """Extra: l'endpoint Anthropic non ha i problemi 1 e 3. Utile come confronto."""
    url = "/messages"
    status, parsed = call(
        host, pat, url,
        {"model": CLAUDE, "max_tokens": 16,
         "messages": [{"role": "user", "content": "ping"}]},
    )
    if status != 200:
        msg = parsed.get("message", parsed) if isinstance(parsed, dict) else parsed
        return UNKNOWN, "HTTP %s — %s" % (status, str(msg)[:110]), "-"
    reason = parsed.get("stop_reason") if isinstance(parsed, dict) else None
    return (INFO, "stop_reason -> %r (max_tokens accettato)" % reason,
            "alternativa ai Claude senza strato di traduzione, vedi handover")


TESTS = [
    ("1. max_tokens rifiutato",               t1_max_tokens),
    ("2. GET /v1/models -> 404",              t2_models_list),
    ("3. finish_reason non valorizzato",      t3_finish_reason),
    ("4. tools + reasoning_effort",           t4_tools_reasoning),
    ("5. /v1/responses non abilitato",        t5_responses_api),
    ("6. endpoint Anthropic (confronto)",     t6_anthropic_endpoint),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--pat-file", help="file contenente il PAT")
    args = parser.parse_args()

    if args.pat_file:
        pat = open(args.pat_file).read().strip()
    else:
        pat = os.environ.get("CORTEX_PAT", "").strip()
    if not pat:
        sys.exit("serve il PAT: esporta CORTEX_PAT oppure usa --pat-file")

    print("host: %s\n" % args.host)
    verdicts = {}
    for label, fn in TESTS:
        try:
            verdict, detail, action = fn(args.host, pat)
        except Exception as err:
            verdict, detail, action = UNKNOWN, "errore nel test: %s" % err, "-"
        verdicts[label] = verdict
        print("%-36s %s" % (label, verdict))
        print("    riscontro: %s" % detail)
        print("    azione:    %s\n" % action)

    risolti = [k for k, v in verdicts.items() if v == OK]
    print("=" * 78)
    if not risolti:
        print("Nessun cambiamento: il proxy serve ancora tutto intero.")
    else:
        print("CAMBIATO qualcosa (%d voci): il proxy va aggiornato." % len(risolti))
        for k in risolti:
            print("  - %s" % k)
        print("\nSeguire la sezione 'PIANO DI MIGRAZIONE' dell'handover")
        print("20260818_cortex_gateway_migration_playbook.md")


if __name__ == "__main__":
    main()
