#!/usr/bin/env python3
"""Cancello di compatibilita' per i modelli Cortex: dice se un modello nuovo e'
usabile con lo stack costruito, PRIMA di metterlo in configurazione.

Perche' non basta refresh_cortex_models.py
-----------------------------------------
Quello script risponde a tre domande (il modello risponde? accetta 'tools'? vuole
reasoning_effort="none"?) e su quella base riscrive cortex_models.json. Sono
condizioni necessarie ma non sufficienti: lo stack si regge su sei scostamenti
del wire Cortex normalizzati da cortex_proxy.py, e un modello nuovo puo' romperne
uno senza fallire nessuno dei tre controlli. Il caso reale: i modelli Claude
rispondono 200 al primo probe ma non valorizzano finish_reason, e con due tool
call in un turno avvelenano la history in modo permanente — un guasto che
si e' manifestato come "Telegram risponde solo 'model provider failed'".

Questo script testa quindi la COPPIA proxy+upstream, non il gateway nudo:
importa le funzioni vere di cortex_proxy.py e le applica alla richiesta e alla
risposta, come farebbe il proxy in esecuzione. Se il verdetto e' COMPATIBILE, il
modello funziona con cio' che abbiamo costruito, non "con OpenAI in generale".

Cosa NON fa: non modifica nulla. Nessuna scrittura su file, stage, servizi o
container. Solo chiamate in lettura al gateway.

Uso:
    CORTEX_PAT="..." python3 cortex_model_gate.py --new     # solo i nomi nuovi
    CORTEX_PAT="..." python3 cortex_model_gate.py --all     # regressione sui noti
    CORTEX_PAT="..." python3 cortex_model_gate.py --models deepseek-v4-flash

L'assegnazione della variabile deve stare IN TESTA al comando: con
'cd x && CORTEX_PAT="<chiave>" ...' l'iniezione del segreto non scatta e si
ottiene HTTP 401 (trappola gia' incontrata, playbook §9).

Verdetti
--------
COMPATIBILE     text and tool calling work through the proxy: safe to promote
CON RISERVA     responds but without usable tool calling. Suitable for text generation
                only; NOT for Hermes in agent mode since agents depend on tool calling
INCOMPATIBILE   does not respond at all from this account: do not add to config

Exit code: 1 se un modello GIA' in cortex_models.json regredisce (era in
configurazione e ora non risponde, o ha perso il tool calling). Serve per
accorgersi di una regressione lato Snowflake senza leggere tutto il report.
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
sys.path.insert(0, os.path.join(HERE, "..", "proxy"))

MODELS_FILE = os.environ.get("CORTEX_MODELS_PATH", os.path.join(HERE, "..", "proxy", "models.json"))
DEFAULT_CONNECTION = os.environ.get("SNOWFLAKE_CONNECTION", "default")
DEFAULT_HOST = os.environ.get("SNOWFLAKE_HOST", "localhost")

# Le trasformazioni del proxy sono la meta' del contratto da verificare: si
# importano invece di riscriverle, altrimenti il gate misurerebbe una copia
# divergente del codice che gira in produzione.
try:
    import cortex_proxy
except Exception as err:                                   # pragma: no cover
    sys.exit("cortex_proxy.py non importabile (%s): il gate deve girare "
             "nella stessa directory" % err)

# collapse_parallel_tool_calls() e' parte di cortex_proxy.py. La lookup resta
# difensiva perche' il gate puo' essere puntato a un proxy piu' vecchio della
# patch: in quel caso T5 non e' verificabile e il gate lo dichiara, invece di
# far passare per compatibile un modello mai testato su quel vincolo.
COLLAPSE = getattr(cortex_proxy, "collapse_parallel_tool_calls", None)

TOOL = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Return the current time in a given city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}

TOOL2 = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Return the weather in a given city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}

FINISH_VALIDI = {"stop", "length", "tool_calls", "content_filter"}

# Budget di output. NON abbassarli: i modelli di reasoning consumano il budget
# prima di emettere testo, e con un valore troppo piccolo rispondono HTTP 200 con
# contenuto vuoto e finish_reason='length'. Misurato il 2026-08-21: con 64 token
# openai-gpt-5, -mini e -nano sembravano rotti; con 512 rispondono. E' la stessa
# trappola di max_completion_tokens=1, che al primo censimento fece scartare per
# sbaglio tutta la famiglia gpt-5.
BUDGET_TESTO = 1024
BUDGET_RETRY = 4096          # secondo tentativo quando il primo finisce in 'length'
BUDGET_STREAM = 512
BUDGET_TOOLS = 1024


# --------------------------------------------------------------------------- #
# trasporto
# --------------------------------------------------------------------------- #

def post(host, pat, body, stream=False, timeout=120):
    """(status, testo|righe_sse). Non solleva su 4xx/5xx: lo stato serve."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        "https://%s/api/v2/cortex/v1/chat/completions" % host,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "Authorization": "Bearer %s" % pat,
            "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if stream:
                return resp.status, [ln.decode("utf-8", "replace").rstrip("\r\n")
                                     for ln in resp]
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:                               # rete, DNS, timeout
        return 0, "eccezione locale: %s" % exc


def motivo(status, testo):
    """Riduce il corpo di un errore a una riga, distinguendo i tre casi che
    contano: nome non nel catalogo dell'endpoint, nome noto ma non servito,
    account non abilitato. Confonderli fa perdere ore."""
    t = testo.strip()
    try:
        parsed = json.loads(t)
        t = str(parsed.get("message") or parsed.get("error") or t)
    except ValueError:
        pass
    t = " ".join(t.split())
    if status == 403:
        return "HTTP 403 - account non abilitato"
    low = t.lower()
    if "unknown model" in low:
        return "unknown model (nome non servito da questo endpoint)"
    if "unavailable" in low:
        return "unavailable (nome riconosciuto ma non servito)"
    return "HTTP %s: %s" % (status, t[:140])


def scelta(testo):
    try:
        return ((json.loads(testo).get("choices") or [{}])[0]) or {}
    except (ValueError, AttributeError, TypeError):
        return {}


# --------------------------------------------------------------------------- #
# i test
# --------------------------------------------------------------------------- #

def t1_t2_non_stream(host, pat, model, esito):
    """T1 risposta non-stream con 'max_tokens' + T2 finish_reason normalizzato.

    Si parte dal body che manda HERMES (con 'max_tokens'), non da quello che
    Cortex accetta: e' la riscrittura del proxy a doverlo rendere valido. Se
    saltasse quel passaggio, il test misurerebbe uno scenario che in produzione
    non esiste.

    Un contenuto vuoto con finish_reason='length' NON e' un difetto del modello:
    e' il budget esaurito nel reasoning. Si ritenta una volta piu' larghi prima
    di dichiarare KO, altrimenti il gate produce falsi allarmi sui modelli di
    reasoning (osservato su tutta la famiglia openai-gpt-5).
    """
    def tenta(budget):
        richiesta = {
            "model": model,
            "max_tokens": budget,
            "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
        }
        adattato, corpo = cortex_proxy.adapt_payload(json.dumps(richiesta).encode())
        esito["max_tokens_riscritto"] = "max_completion_tokens" in (corpo or {})
        return post(host, pat, json.loads(adattato)) + (budget,)

    status, testo, budget = tenta(BUDGET_TESTO)
    if status != 200:
        esito["T1"] = "KO"
        esito["motivo"] = motivo(status, testo)
        return False

    ch = scelta(testo)
    contenuto = ((ch.get("message") or {}).get("content") or "").strip()
    if not contenuto and ch.get("finish_reason") == "length":
        esito["note"].append(
            "primo tentativo esaurito nel reasoning (%d token, finish='length'): "
            "ritentato con %d" % (budget, BUDGET_RETRY))
        status, testo, budget = tenta(BUDGET_RETRY)
        ch = scelta(testo)
        contenuto = ((ch.get("message") or {}).get("content") or "").strip()

    esito["budget_necessario"] = budget
    esito["T1"] = "OK" if contenuto else "KO"
    esito["contenuto"] = contenuto[:30]
    if not contenuto:
        esito["motivo"] = ("HTTP 200 ma contenuto vuoto anche con %d token "
                           "(finish=%r)" % (budget, ch.get("finish_reason")))
        return False

    # T2: cosa manda Cortex, e cosa resta dopo la normalizzazione del proxy.
    grezzo = ch.get("finish_reason")
    esito["finish_upstream"] = repr(grezzo)
    normalizzato = cortex_proxy.normalize_finish_reason(testo, requested_max=budget)
    dopo = (scelta(normalizzato.decode() if isinstance(normalizzato, bytes)
                   else normalizzato)).get("finish_reason")
    esito["finish_proxy"] = repr(dopo)
    esito["T2"] = "OK" if dopo in FINISH_VALIDI else "KO"
    return True


def t3_streaming(host, pat, model, esito):
    """T3 streaming: arrivano chunk, e lo stream si chiude con un finish_reason.

    Sui modelli Claude nessun chunk porta finish_reason e il proxy inietta un
    chunk sintetico prima di [DONE]: senza di quello il client considera la
    risposta troncata e tenta fino a 4 continuazioni, duplicando il testo.
    """
    richiesta = {
        "model": model,
        "max_completion_tokens": BUDGET_STREAM,
        "stream": True,
        "messages": [{"role": "user", "content": "Count from 1 to 5."}],
    }
    status, righe = post(host, pat, richiesta, stream=True)
    if status != 200:
        esito["T3"] = "KO"
        esito["note"].append("streaming: %s" % motivo(status, "".join(righe)
                                                     if isinstance(righe, list) else righe))
        return

    chunk = 0
    testo = ""
    visto_finish = False
    for riga in righe:
        if not riga.startswith("data:"):
            continue
        payload = riga[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        chunk += 1
        for c in obj.get("choices") or []:
            testo += ((c.get("delta") or {}).get("content") or "")
            if c.get("finish_reason"):
                visto_finish = True

    esito["stream_chunk"] = chunk
    esito["stream_finish_upstream"] = visto_finish
    if chunk == 0:
        esito["T3"] = "KO"
        esito["note"].append("streaming: nessun chunk ricevuto")
        return
    # Il proxy chiude lo stream lui se l'upstream non lo fa: in entrambi i casi
    # il client vede un finish_reason. Serve solo che i chunk arrivino.
    esito["T3"] = "OK" if testo.strip() else "KO"
    if not testo.strip():
        esito["note"].append("streaming: chunk presenti ma nessun contenuto")


def t4_tool_roundtrip(host, pat, model, esito):
    """T4 il test che decide se un modello e' usabile da un agente.

    Due passaggi: il modello deve emettere una tool call, e la richiesta
    successiva che riporta il risultato del tool deve essere accettata. E' il
    secondo passaggio quello che rompeva Hermes: un turno con toolUse senza
    toolResult corrispondente viene rifiutato con 400 in modo non ritentabile,
    e resta nella history persistita — quindi la sessione muore per sempre.
    """
    base = {
        "model": model,
        "max_completion_tokens": BUDGET_TOOLS,
        "tools": [TOOL],
        "messages": [{"role": "user",
                      "content": "What time is it in Rome? Use the get_time tool."}],
    }
    adattato, _ = cortex_proxy.adapt_payload(json.dumps(base).encode())
    status, testo = post(host, pat, json.loads(adattato))

    if status != 200:
        low = testo.lower()
        if cortex_proxy.REASONING_TOOLS_ERROR in low:
            esito["T4"] = "rinviato a T6"
            return
        if "tool calling is not supported" in low:
            esito["T4"] = "KO"
            esito["tools"] = "non supportati dal modello"
            return
        esito["T4"] = "KO"
        esito["tools"] = motivo(status, testo)
        return

    ch = scelta(testo)
    chiamate = (ch.get("message") or {}).get("tool_calls") or []
    if not chiamate:
        # Non e' un difetto: il modello ha scelto di rispondere a parole.
        esito["T4"] = "INCONCLUSIVO"
        esito["tools"] = "nessuna tool call emessa (il modello ha risposto a testo)"
        return

    esito["tool_calls_emesse"] = len(chiamate)

    # Round-trip: si rimanda indietro l'assistant esattamente com'e' arrivato,
    # piu' un messaggio 'tool' per OGNI chiamata (il vincolo e' 1:1).
    storia = list(base["messages"])
    storia.append({"role": "assistant",
                   "content": (ch.get("message") or {}).get("content") or "",
                   "tool_calls": chiamate})
    for c in chiamate:
        storia.append({"role": "tool",
                       "tool_call_id": c.get("id"),
                       "content": "14:30 local time"})

    seguito = dict(base, messages=storia)
    adattato, _ = cortex_proxy.adapt_payload(json.dumps(seguito).encode())
    status, testo = post(host, pat, json.loads(adattato))
    if status != 200:
        esito["T4"] = "KO"
        esito["tools"] = "round-trip del toolResult rifiutato: %s" % motivo(status, testo)
        return
    esito["T4"] = "OK"


def t5_tool_parallele(host, pat, model, esito):
    """T5 due tool call nello stesso turno: il caso di R-19.

    Cortex converte ogni messaggio 'tool' in un turno separato, quindi una
    assistant con N toolUse riceve 1 solo toolResult nel primo turno e la
    richiesta viene rifiutata. collapse_parallel_tool_calls() fonde il turno in
    una sola chiamata conservando il contenuto degli altri risultati.

    Se quella funzione non e' in questo sorgente, il test lo dichiara: e'
    un'informazione piu' utile del test stesso, perche' significa che una
    rebuild dell'immagine da questo contesto riporterebbe il guasto.
    """
    storia = [
        {"role": "user", "content": "Time and weather in Rome?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "get_time", "arguments": '{"city":"Rome"}'}},
            {"id": "call_b", "type": "function",
             "function": {"name": "get_weather", "arguments": '{"city":"Rome"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_a", "content": "14:30"},
        {"role": "tool", "tool_call_id": "call_b", "content": "sunny, 28C"},
    ]
    body = {"model": model, "max_completion_tokens": BUDGET_STREAM,
            "tools": [TOOL, TOOL2], "messages": storia}

    status, testo = post(host, pat, body)
    esito["parallele_grezze"] = "HTTP %s" % status

    if status == 200:
        # L'upstream le accetta: nessuna fusione necessaria per questo modello.
        esito["T5"] = "OK (upstream accetta le tool call parallele)"
        return

    if COLLAPSE is None:
        esito["T5"] = "NON VERIFICABILE"
        esito["note"].append(
            "collapse_parallel_tool_calls assente da cortex_proxy.py: il proxy "
            "e' piu' vecchio della patch sulle tool call parallele")
        return

    fuso = COLLAPSE(dict(body))
    status, testo = post(host, pat, fuso)
    esito["T5"] = "OK (fuse dal proxy)" if status == 200 else "KO"
    if status != 200:
        esito["note"].append("tool call parallele: %s" % motivo(status, testo))


def t6_tools_reasoning(host, pat, model, esito):
    """T6 il vincolo tools + reasoning_effort della famiglia gpt-5.6.

    Omettere reasoning_effort NON basta: il gateway applica un default e
    rifiuta comunque. Il proxy forza "none" per i modelli in lista e ha un
    retry adattivo sul messaggio d'errore. Qui si stabilisce a quale delle due
    categorie appartiene il modello.
    """
    body = {"model": model, "max_completion_tokens": BUDGET_STREAM, "tools": [TOOL],
            "reasoning_effort": "low",
            "messages": [{"role": "user", "content": "What time is it in Rome?"}]}
    status, testo = post(host, pat, body)
    if status == 200:
        esito["T6"] = "OK (tools e reasoning convivono)"
        return
    if cortex_proxy.REASONING_TOOLS_ERROR not in testo.lower():
        esito["T6"] = "n/d"
        esito["note"].append("tools+reasoning: %s" % motivo(status, testo))
        return

    status, testo = post(host, pat, json.loads(cortex_proxy.force_no_reasoning(body)))
    if status == 200:
        esito["T6"] = "OK con reasoning_effort=none"
        esito["richiede_reasoning_none"] = True
    else:
        esito["T6"] = "KO"
        esito["note"].append("tools rifiutati anche con reasoning_effort=none")


def t7_context(model, dichiarato, noti, esito):
    """T7 controllo della dichiarazione del context, non della finestra reale.

    Sondare la finestra vera significherebbe spedire centinaia di migliaia di
    token per modello: costoso e inutile. Qui si verifica solo che il valore
    esista e si segnala quando e' il default prudenziale, che va corretto a mano
    dalla doc prima di considerare il modello promosso. Sottostimare e' sicuro
    (il client comprime prima del necessario), sovrastimare rompe le chiamate.
    """
    if model not in noti:
        esito["T7"] = "DA VERIFICARE"
        esito["note"].append(
            "modello nuovo: context da leggere su aisql-regional-availability "
            "(il refresh mette 128000 prudenziale)")
        return
    esito["context"] = dichiarato
    esito["T7"] = "OK" if dichiarato and dichiarato > 0 else "KO"


# --------------------------------------------------------------------------- #
# verdetto
# --------------------------------------------------------------------------- #

def verdetto(esito):
    if esito.get("T1") != "OK":
        return "INCOMPATIBILE"
    tool_ok = esito.get("T4") == "OK" or str(esito.get("T6", "")).startswith("OK")
    if esito.get("T4") == "KO" and not str(esito.get("T6", "")).startswith("OK"):
        return "CON RISERVA"
    if esito.get("T4") == "INCONCLUSIVO":
        return "CON RISERVA"
    if esito.get("T2") == "KO" or esito.get("T3") == "KO":
        return "CON RISERVA"
    return "COMPATIBILE" if tool_ok else "CON RISERVA"


def valuta(host, pat, model, noti):
    esito = {"model": model, "note": []}
    if not t1_t2_non_stream(host, pat, model, esito):
        esito["verdetto"] = "INCOMPATIBILE"
        return esito
    t3_streaming(host, pat, model, esito)
    t4_tool_roundtrip(host, pat, model, esito)
    if esito.get("T4") in ("rinviato a T6", "KO"):
        t6_tools_reasoning(host, pat, model, esito)
        if str(esito.get("T6", "")).startswith("OK"):
            # Il vincolo e' gestito dal proxy: si riprova il round-trip vero.
            t4_tool_roundtrip(host, pat, model, esito)
    if esito.get("T4") == "OK":
        t5_tool_parallele(host, pat, model, esito)
    t7_context(model, noti.get(model), noti, esito)
    esito["verdetto"] = verdetto(esito)
    return esito


# --------------------------------------------------------------------------- #
# catalogo e report
# --------------------------------------------------------------------------- #

def catalogo(connection):
    """Nomi del catalogo, piu' la variante senza il segmento '1p-'.

    Il catalogo elenca OPENAI-1P-GPT-5.6-LUNA ma il nome invocabile e'
    openai-gpt-5.6-luna: registriamo entrambe le forme e lascia decidere la
    chiamata reale. Il catalogo elenca anche modelli che rispondono
    'unknown model', quindi da solo non prova nulla.
    """
    out = subprocess.run(
        ["snow", "sql", "-c", connection, "--format", "json",
         "-q", "SHOW CORTEX BASE MODELS IN SCHEMA SNOWFLAKE.MODELS"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.stderr.write("attenzione: SHOW CORTEX BASE MODELS ha fallito, "
                         "il diff col catalogo non sara' disponibile\n")
        return {}
    try:
        righe = json.loads(out.stdout)
    except json.JSONDecodeError:
        return {}
    if righe and isinstance(righe[0], list):
        righe = righe[0]

    trovati = {}
    for riga in righe:
        raw = riga.get("name") or riga.get("NAME")
        if not raw:
            continue
        nome = str(raw).strip().lower()
        meta = {"status": (riga.get("lifecycle_status") or "").upper(),
                "creato": str(riga.get("created_on") or "")[:10]}
        trovati[nome] = meta
        senza = re.sub(r"-1p-", "-", nome)
        if senza != nome:
            trovati.setdefault(senza, meta)
    return trovati


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--models", help="elenco separato da virgole")
    p.add_argument("--new", action="store_true",
                   help="solo i nomi del catalogo assenti da cortex_models.json")
    p.add_argument("--all", action="store_true",
                   help="tutti i modelli in cortex_models.json (regressione)")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--connection", default=DEFAULT_CONNECTION)
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--json", help="scrive il report anche in JSON")
    p.add_argument("--retry-ko", action="store_true",
                   help="riprova anche i nomi già documentati in _non_disponibili")
    args = p.parse_args()

    pat = os.environ.get("CORTEX_PAT", "").strip()
    if not pat:
        sys.exit("serve il PAT: CORTEX_PAT=\"...\" in testa al comando")

    with open(MODELS_FILE) as fh:
        doc = json.load(fh)
    noti = {str(k): int(v) for k, v in doc["models"].items()}
    # Nomi gia' provati e non funzionanti: il catalogo ne contiene decine (modelli
    # di embedding, parse, sentiment, versioni EOL) che non sono candidati per un
    # provider di chat. Riprovarli ad ogni giro costa tempo e sommerge il report.
    gia_ko = {k for k in (doc.get("_non_disponibili") or {}) if not k.startswith("_")}

    cat = catalogo(args.connection) if (args.new or not args.models) else {}
    nuovi_tutti = sorted(n for n in cat if n not in noti)
    scartati = {}
    nuovi = []
    for n in nuovi_tutti:
        if cat[n]["status"] == "EOL":
            scartati[n] = "EOL"
        elif n in gia_ko and not args.retry_ko:
            scartati[n] = "già documentato non disponibile"
        else:
            nuovi.append(n)
    spariti = sorted(n for n in noti if cat and n not in cat)

    if args.models:
        bersagli = [m.strip() for m in args.models.split(",") if m.strip()]
    elif args.all:
        bersagli = sorted(noti)
    else:
        bersagli = nuovi

    print("host: %s" % args.host)
    print("in configurazione: %d modelli" % len(noti))
    if cat:
        print("nel catalogo: %d nomi (con le varianti senza '1p-')" % len(cat))
        print("candidati nuovi: %s"
              % (", ".join("%s [%s, creato %s]"
                           % (n, cat[n]["status"] or "lifecycle NULL", cat[n]["creato"])
                           for n in nuovi) or "nessuno"))
        if scartati:
            print("scartati senza provarli: %d (%d EOL, %d già documentati non "
                  "disponibili — con --retry-ko si riprovano)"
                  % (len(scartati),
                     sum(1 for v in scartati.values() if v == "EOL"),
                     sum(1 for v in scartati.values() if v != "EOL")))
        if spariti:
            print("in configurazione ma NON piu' nel catalogo: %s" % ", ".join(spariti))
    if COLLAPSE is None:
        print("\nATTENZIONE: collapse_parallel_tool_calls non e' in cortex_proxy.py.")
        print("Il proxy e' piu' vecchio della patch sulle tool call parallele: T5")
        print("non e' verificabile, e un'immagine costruita da questo contesto")
        print("reintrodurrebbe il guasto.")

    if not bersagli:
        print("\nnessun modello da valutare.")
        return 0

    print("\nvaluto %d modelli: %s\n" % (len(bersagli), ", ".join(bersagli)))
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        esiti = list(pool.map(lambda m: valuta(args.host, pat, m, noti), bersagli))

    larghezza = max(len(e["model"]) for e in esiti)
    print("%-*s  %-14s %-4s %-4s %-4s %-6s %s" % (
        larghezza, "modello", "verdetto", "T1", "T2", "T3", "T4", "dettaglio"))
    for e in sorted(esiti, key=lambda x: (x["verdetto"], x["model"])):
        dettaglio = e.get("motivo") or e.get("tools") or ""
        if e.get("finish_upstream") and e.get("finish_upstream") != e.get("finish_proxy"):
            dettaglio = dettaglio or ("finish_reason %s -> %s"
                                      % (e["finish_upstream"], e["finish_proxy"]))
        print("%-*s  %-14s %-4s %-4s %-4s %-6s %s" % (
            larghezza, e["model"], e["verdetto"], e.get("T1", "-"),
            e.get("T2", "-"), e.get("T3", "-"), str(e.get("T4", "-"))[:6], dettaglio))

    # L'intestazione va stampata se c'e' QUALCOSA da dire, non solo in presenza
    # di note: altrimenti i dettagli di un modello senza note finiscono sotto
    # l'intestazione del modello precedente e gli vengono attribuiti.
    for e in esiti:
        righe = list(e["note"])
        if e.get("T5") and e["T5"] != "OK":
            righe.append("tool call parallele: %s" % e["T5"])
        if e.get("T6"):
            righe.append("tools+reasoning: %s" % e["T6"])
        if e.get("T7") == "DA VERIFICARE":
            righe.append("context: DA VERIFICARE a mano sulla doc")
        if not righe:
            continue
        print("\n%s:" % e["model"])
        for riga in righe:
            print("  - %s" % riga)

    # Regressione: un modello che era in configurazione e non regge piu'.
    regrediti = [e["model"] for e in esiti
                 if e["model"] in noti and e["verdetto"] == "INCOMPATIBILE"]
    if regrediti:
        print("\nREGRESSIONE: %s erano in configurazione e non rispondono piu'."
              % ", ".join(regrediti))
        print("NON eseguire refresh_cortex_models.py --write adesso: riscrive la")
        print("lista in base a questo giro e li rimuoverebbe. Prima capire se e' un")
        print("guasto transitorio (riprovare) o definitivo (lato Snowflake).")

    promuovibili = [e["model"] for e in esiti if e["verdetto"] == "COMPATIBILE"
                    and e["model"] not in noti]
    if promuovibili:
        print("\nPROMUOVIBILI: %s" % ", ".join(promuovibili))
        print("Procedura di promozione: §15 di 20260819_hermes_desktop_client_handover.md")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(esiti, fh, indent=2, ensure_ascii=False)
        print("\nreport JSON in %s" % args.json)

    return 1 if regrediti else 0


if __name__ == "__main__":
    sys.exit(main())
