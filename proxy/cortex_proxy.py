#!/usr/bin/env python3
"""
Proxy OpenAI-compatible -> Snowflake Cortex, per uso dentro SPCS.

Perché serve: da SPCS l'unica auth accettata dal Cortex REST API e' OAuth con
il session token, e richiede l'header X-Snowflake-Authorization-Token-Type: OAUTH.
I client OpenAI standard (Hermes incluso) inviano solo "Authorization: Bearer <key>",
quindi questo proxy riscrive gli header e inoltra la richiesta.

Bonus: rilegge /snowflake/session/token ad ogni richiesta, quindi il token
non scade mai (SPCS lo rinnova automaticamente sul filesystem).

Uso:
    nohup python3 /root/.hermes/cortex_proxy.py > /tmp/cortex_proxy.log 2>&1 &

Poi puntare il client a http://127.0.0.1:8080/v1
"""
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SNOWFLAKE_HOST = os.environ.get(
    "SNOWFLAKE_HOST", "localhost"  # overridden by SNOWFLAKE_HOST injected by SPCS
)
CORTEX_BASE = "https://%s/api/v2/cortex/v1" % SNOWFLAKE_HOST
SQL_API_PATH = "/api/v2/statements"
TOKEN_PATH = "/snowflake/session/token"  # noqa: S105

# Bind configurabile: 127.0.0.1 dentro il container di Hermes (il client e' locale),
# 0.0.0.0 quando il proxy gira come servizio SPCS a se' stante e deve essere
# raggiungibile da altri servizi via DNS interno.
LISTEN_ADDR = (
    os.environ.get("CORTEX_PROXY_BIND", "127.0.0.1"),
    int(os.environ.get("CORTEX_PROXY_PORT", "8080")),
)

# Sorgente unica dei modelli, condivisa con hermes_configure.py: se le due liste
# divergessero, Hermes dichiarerebbe un context length diverso da quello annunciato
# qui su /v1/models. Vedi cortex_models.json per l'elenco e le verifiche.
#
# Si cercano in ordine: percorso esplicito, volume da stage Snowflake (aggiornabile
# con un PUT, senza rebuild dell'immagine), copia dentro l'immagine come fallback.
MODELS_PATHS = [
    p
    for p in (
        os.environ.get("CORTEX_MODELS_PATH"),
        "/models/cortex_models.json",
        "/opt/cortex_models.json",
    )
    if p
]

# Fallback minimo se nessun file e' leggibile: meglio due modelli certi che nessuno.
FALLBACK_MODELS = {"claude-sonnet-5": 1000000, "claude-opus-5": 1000000}

# Cache del file: rileggiamo solo quando cambia mtime, cosi' un aggiornamento dello
# stage viene raccolto senza riavviare il servizio e senza rileggere ad ogni richiesta.
_models_cache = {"path": None, "mtime": None, "models": None, "no_reasoning": None}


def _read_models_file():
    """Ritorna (models, tools_require_reasoning_effort_none) dal primo file leggibile."""
    for path in MODELS_PATHS:
        try:
            stat = os.stat(path)
        except OSError:
            continue

        if _models_cache["path"] == path and _models_cache["mtime"] == stat.st_mtime:
            return _models_cache["models"], _models_cache["no_reasoning"]

        try:
            with open(path) as fh:
                data = json.load(fh)
            models = {str(k): int(v) for k, v in (data["models"] or {}).items()}
            if not models:
                continue
            no_reasoning = set(data.get("tools_require_reasoning_effort_none") or [])
        except Exception as err:
            sys.stderr.write("%s illeggibile (%s), provo il prossimo\n" % (path, err))
            continue

        _models_cache.update(
            path=path, mtime=stat.st_mtime, models=models, no_reasoning=no_reasoning
        )
        sys.stderr.write(
            "elenco modelli caricato da %s (%d modelli)\n" % (path, len(models))
        )
        sys.stderr.flush()
        return models, no_reasoning

    if _models_cache["models"]:
        return _models_cache["models"], _models_cache["no_reasoning"]
    return dict(FALLBACK_MODELS), set()


def cortex_models():
    return _read_models_file()[0]


def tools_need_no_reasoning():
    return _read_models_file()[1]


# Messaggio con cui Cortex rifiuta tools+reasoning: usato per il retry adattivo.
REASONING_TOOLS_ERROR = "function tools with reasoning_effort"


CONTEXT_KEYS = (
    "context_length",
    "context_window",
    "max_context_length",
    "max_input_tokens",
    "max_model_len",
    "n_ctx",
)


def model_entry(name, context_length):
    """Descrittore modello con tutti gli alias di context length noti a Hermes.

    Hermes prova 12 chiavi diverse in ordine; ne pubblichiamo le principali
    cosi' il probe trova il valore corretto qualunque alias cerchi.
    """
    entry = {
        "id": name,
        "object": "model",
        "created": 0,
        "owned_by": "snowflake",
    }
    for key in CONTEXT_KEYS:
        entry[key] = context_length
    return entry


def _has_context_length(raw):
    """True se una risposta /v1/models upstream dichiara il context length.

    Senza almeno uno degli alias che Hermes cerca, la lista upstream sarebbe una
    regressione rispetto al nostro file: Hermes non saprebbe la finestra dei
    modelli e tornerebbe a stimarla male.
    """
    try:
        data = (json.loads(raw) or {}).get("data") or []
    except (ValueError, TypeError):
        return False
    return any(
        isinstance(m, dict) and any(m.get(k) for k in CONTEXT_KEYS) for m in data
    )


def read_token():
    """Rilegge il token ad ogni chiamata: SPCS lo ruota sul filesystem."""
    with open(TOKEN_PATH) as fh:
        return fh.read().strip()


def adapt_payload(payload):
    """Adatta il body OpenAI alle differenze del wire Cortex.

    1) Cortex rifiuta 'max_tokens' con HTTP 400 "max_tokens is deprecated in favor
    of max_completion_tokens". Hermes invia 'max_tokens' per tutti i modelli che
    non appartengono alle famiglie OpenAI (vedi model_forces_max_completion_tokens
    in utils.py), quindi per claude-*, mistral-*, qwen3-* e simili la richiesta
    fallirebbe sempre. Hermes interpreta poi quel 400 come overflow di contesto e
    riporta il fuorviante "Context length exceeded (N tokens)".

    2) I modelli gpt-5.6-* rifiutano 'tools' se reasoning_effort non e'
    esplicitamente "none": "Function tools with reasoning_effort are not
    supported". Ometterlo NON basta, il gateway applica un default. Senza questa
    riscrittura quei modelli non possono usare strumenti, cioe' sono inutili per
    un agente.

    Nessuna di queste due cose e' configurabile lato Hermes: e' il motivo per cui
    questo proxy esiste.
    """
    if not payload:
        return payload, None
    try:
        body = json.loads(payload)
    except (ValueError, TypeError):
        return payload, None  # non-JSON: inoltra invariato
    if not isinstance(body, dict):
        return payload, None

    if "max_tokens" in body:
        value = body.pop("max_tokens")
        # Se il client ha già inviato la chiave nuova, la sua vince.
        body.setdefault("max_completion_tokens", value)

    model = str(body.get("model") or "")
    if body.get("tools") and model in tools_need_no_reasoning():
        body["reasoning_effort"] = "none"

    return json.dumps(body).encode(), body


def force_no_reasoning(body):
    """Riscrive il body per il retry: reasoning_effort esplicitamente disattivato."""
    body = dict(body)
    body["reasoning_effort"] = "none"
    return json.dumps(body).encode()


def infer_finish_reason(choice, requested_max, usage):
    """Deduce il finish_reason che Cortex non manda, invece di forzare 'stop'.

    Cortex collassa tre casi distinti in "": risposta completa, tool call e
    risposta troncata dal limite di token. Forzare 'stop' su tutti e tre e' lossy
    in due modi, entrambi osservati:

      - un client che ramifica su finish_reason == 'tool_calls' non esegue il
        tool. Il messaggio assistant col blocco toolUse finisce comunque in
        history, che resta senza il toolResult corrispondente: la richiesta
        successiva viene rifiutata con HTTP 400 "Each 'toolUse' block must be
        accompanied with a matching 'toolResult' block". E' il motivo per cui il
        tool calling non funzionava sui modelli Claude.
      - una risposta tagliata a metà dal limite di token viene marcata come
        completa, quindi un workflow puo' trattare mezza frase come definitiva.

    message.tool_calls e usage.completion_tokens permettono di ricostruire due
    dei tre casi. Restano indistinguibili content_filter e refusal, casi rari:
    si passa da "sbaglio sempre" a "sbaglio raramente".
    """
    if (choice.get("message") or {}).get("tool_calls"):
        return "tool_calls"
    done = (usage or {}).get("completion_tokens")
    if requested_max and done and done >= requested_max:
        return "length"
    return "stop"


def normalize_finish_reason(raw, requested_max=None):
    """Valorizza finish_reason dove Cortex lo lascia vuoto (risposte non-stream).

    Cortex restituisce "finish_reason": "" per i modelli Claude (per gpt-5.6 invece
    manda "stop"). I client OpenAI leggono quel campo per due decisioni distinte:
    se la risposta e' completa, e se devono eseguire un tool. Vedi
    infer_finish_reason per il dettaglio dei casi.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if not isinstance(obj, dict):
        return raw
    usage = obj.get("usage")
    changed = False
    for choice in obj.get("choices") or []:
        if isinstance(choice, dict) and not choice.get("finish_reason"):
            choice["finish_reason"] = infer_finish_reason(choice, requested_max, usage)
            changed = True
    return json.dumps(obj).encode() if changed else raw


def stop_chunk(model, reason="stop"):
    """Chunk SSE sintetico che chiude lo stream secondo lo spec OpenAI.

    reason va impostato a 'tool_calls' se qualche delta ha portato una tool call,
    altrimenti il client non la esegue (vedi infer_finish_reason). In streaming
    'length' non e' deducibile: i chunk SSE di Cortex non portano usage.
    """
    payload = {
        "id": "",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model or "",
        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
    }
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def reindex_tool_calls(obj, state):
    """Riscrive l'indice dei delta tool_calls in streaming.

    Sui modelli Claude, Cortex marca TUTTE le tool call parallele con index 0.
    Misurato il 2026-08-18 su claude-sonnet-5, due tool call in un turno:
    sette frammenti, tutti con index 0, il secondo 'id' compare al frammento 4
    sempre su index 0. Il client riassembla per indice, fonde le due chiamate
    in una sola, esegue un tool e rimanda un solo toolResult per due toolUse:
    Cortex rifiuta la richiesta successiva con HTTP 400
    "Each 'toolUse' block must be accompanied with a matching 'toolResult'".

    Un frammento con 'id' non vuoto apre una nuova tool call, i successivi
    portano solo il pezzo di arguments con id e name vuoti. Contiamo gli id
    distinti e usiamo il contatore come indice. Il confronto con last_id rende
    l'operazione idempotente: sui modelli che gia' indicizzano correttamente
    (verificato su openai-gpt-5.2, indici [0, 1]) gli indici ricalcolati
    coincidono con quelli originali e nulla viene toccato.
    """
    changed = False
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        for frag in (choice.get("delta") or {}).get("tool_calls") or []:
            if not isinstance(frag, dict):
                continue
            tid = frag.get("id") or ""
            if tid and tid != state.get("last_id"):
                state["count"] = state.get("count", 0) + 1
                state["last_id"] = tid
            index = max(state.get("count", 1) - 1, 0)
            if frag.get("index") != index:
                frag["index"] = index
                changed = True
    return changed


def normalize_stream(raw_bytes):
    """Processa SSE raw bytes: inietta stop_chunk se mancante, reindexta tool calls.

    Usato dai test; in produzione la stessa logica gira riga per riga nel handler
    per non bufferizzare l'intero stream.
    """
    lines = raw_bytes.split(b"\n")
    out = []
    saw_finish = False
    saw_tool_calls = False
    tool_state = {"count": 0, "last_id": None}
    model = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(b"data:"):
            chunk = stripped[5:].strip()
            if chunk == b"[DONE]":
                if not saw_finish:
                    out.append(stop_chunk(model, "tool_calls" if saw_tool_calls else "stop"))
                out.append(b"data: [DONE]\n\n")
                continue
            if chunk:
                try:
                    obj = json.loads(chunk)
                    model = model or str((obj.get("model") or ""))
                    for choice in obj.get("choices") or []:
                        if not isinstance(choice, dict):
                            continue
                        if choice.get("finish_reason"):
                            saw_finish = True
                        for delta_tc in (choice.get("delta") or {}).get("tool_calls") or []:
                            if delta_tc.get("id"):
                                saw_tool_calls = True
                    changed = reindex_tool_calls(obj, tool_state)
                    if changed:
                        line = b"data: " + json.dumps(obj).encode() + b"\n\n"
                except (ValueError, TypeError):
                    pass
        out.append(line if not line.endswith(b"\n") else line)
    result = b"".join(out)
    if not result.endswith(b"\n\n"):
        result = result.rstrip(b"\n") + b"\n\n"
    return result


def upstream_path(client_path):
    """Normalizza il path del client verso l'endpoint Cortex.

    Il client puo' chiamare /v1/chat/completions o /chat/completions:
    in entrambi i casi l'upstream e' <CORTEX_BASE>/chat/completions.
    """
    path = client_path
    if path.startswith("/v1/"):
        path = path[3:]
    elif path == "/v1":
        path = "/"
    if not path.startswith("/"):
        path = "/" + path
    return CORTEX_BASE + path


class CortexProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silenzia access log
        pass

    def _send_body(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _upstream(self, payload):
        request = urllib.request.Request(  # noqa: S310
            upstream_path(self.path),
            data=payload,
            headers={
                "Authorization": "Bearer " + read_token(),
                "X-Snowflake-Authorization-Token-Type": "OAUTH",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
                "Accept": self.headers.get("Accept", "application/json"),
            },
            method="POST",
        )
        return urllib.request.urlopen(request)  # noqa: S310

    def _sql_upstream(self, payload):
        """Forward SQL API requests using the SPCS service OAuth token unchanged."""
        request = urllib.request.Request(
            "https://%s%s" % (SNOWFLAKE_HOST, SQL_API_PATH),
            data=payload,
            headers={
                "Authorization": "Bearer " + read_token(),
                "X-Snowflake-Authorization-Token-Type": "OAUTH",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
                "Accept": "application/json",
            },
            method="POST",
        )
        return urllib.request.urlopen(request)  # noqa: S310

    def _upstream_get(self, timeout=15):
        """GET verso Cortex. Serve perche' _upstream e' fissato su POST."""
        request = urllib.request.Request(  # noqa: S310
            upstream_path(self.path),
            headers={
                "Authorization": "Bearer " + read_token(),
                "X-Snowflake-Authorization-Token-Type": "OAUTH",
                "Accept": "application/json",
            },
            method="GET",
        )
        return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        if self.path.rstrip("/") == SQL_API_PATH:
            try:
                response = self._sql_upstream(raw)
                body = response.read()
                self._send_body(
                    response.status,
                    body,
                    response.headers.get("Content-Type", "application/json"),
                )
            except urllib.error.HTTPError as err:
                self._send_body(
                    err.code,
                    err.read(),
                    err.headers.get("Content-Type", "application/json"),
                )
            except Exception as err:
                self._send_body(
                    502,
                    json.dumps({"error": "SQL API proxy failure: %s" % err}).encode(),
                )
            return

        payload, body = adapt_payload(raw)

        try:
            response = self._upstream(payload)
        except urllib.error.HTTPError as err:
            err_body = err.read()
            text = err_body.decode("utf-8", "replace")

            # Retry adattivo: alcuni modelli reasoning rifiutano 'tools' se
            # reasoning_effort non e' esplicitamente "none". La lista in
            # cortex_models.json copre quelli noti; questo ramo copre i futuri
            # senza doverla aggiornare.
            if (
                err.code == 400
                and body is not None
                and body.get("tools")
                and body.get("reasoning_effort") != "none"
                and REASONING_TOOLS_ERROR in text.lower()
            ):
                sys.stderr.write(
                    "retry con reasoning_effort=none per il modello %r\n"
                    % body.get("model")
                )
                sys.stderr.flush()
                try:
                    response = self._upstream(force_no_reasoning(body))
                except urllib.error.HTTPError as err2:
                    body2 = err2.read()
                    sys.stderr.write(
                        "retry fallito HTTP %s: %s\n"
                        % (err2.code, body2[:400].decode("utf-8", "replace"))
                    )
                    sys.stderr.flush()
                    self._send_body(err2.code, body2)
                    return
                except Exception as err2:
                    self._send_body(
                        502, json.dumps({"error": {"message": str(err2)}}).encode()
                    )
                    return
            else:
                # Il messaggio di Cortex e' l'unico indizio utile quando il wire
                # OpenAI e quello Cortex divergono: va sempre registrato.
                sys.stderr.write(
                    "upstream HTTP %s su %s: %s\n" % (err.code, self.path, text[:500])
                )
                sys.stderr.flush()
                self._send_body(err.code, err_body)
                return
        except Exception as err:  # rete, DNS, TLS
            self._send_body(
                502, json.dumps({"error": {"message": str(err)}}).encode()
            )
            return

        content_type = response.headers.get("Content-Type", "application/json")
        model = (body or {}).get("model") if isinstance(body, dict) else None

        # Streaming SSE: inoltra riga per riga (SSE e' line-delimited).
        if "text/event-stream" in content_type:
            self.send_response(response.status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            # Senza questo il server terrebbe la connessione keep-alive: il body
            # non ha Content-Length, quindi il client non saprebbe dove finisce.
            self.close_connection = True

            saw_finish = False
            saw_tool_calls = False
            tool_state = {"count": 0, "last_id": None}
            try:
                while True:
                    line = response.readline()
                    if not line:
                        break

                    stripped = line.strip()
                    if stripped.startswith(b"data:"):
                        chunk = stripped[5:].strip()
                        if chunk == b"[DONE]":
                            # Cortex non manda mai un chunk con finish_reason: lo
                            # spec OpenAI lo richiede sull'ultimo, e senza di esso
                            # il client considera la risposta troncata e tenta
                            # continuazioni (testo duplicato). Lo iniettiamo qui,
                            # con 'tool_calls' se lo stream ne ha portata una:
                            # altrimenti il client non esegue il tool e lascia in
                            # history un toolUse orfano, che Cortex rifiuta alla
                            # richiesta dopo.
                            if not saw_finish:
                                self.wfile.write(
                                    stop_chunk(
                                        model,
                                        "tool_calls" if saw_tool_calls else "stop",
                                    )
                                )
                                self.wfile.flush()
                        elif chunk:
                            try:
                                obj = json.loads(chunk)
                                for choice in obj.get("choices") or []:
                                    if not isinstance(choice, dict):
                                        continue
                                    if choice.get("finish_reason"):
                                        saw_finish = True
                                    delta = choice.get("delta") or {}
                                    if delta.get("tool_calls"):
                                        saw_tool_calls = True
                                if reindex_tool_calls(obj, tool_state):
                                    line = (
                                        b"data: "
                                        + json.dumps(obj).encode()
                                        + b"\n\n"
                                    )
                            except (ValueError, TypeError):
                                pass

                    self.wfile.write(line)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                response.close()
            return

        requested_max = (
            body.get("max_completion_tokens") if isinstance(body, dict) else None
        )
        self._send_body(
            response.status,
            normalize_finish_reason(response.read(), requested_max),
            content_type,
        )

    def do_GET(self):
        # Alcuni client interrogano /v1/models in fase di handshake, altri
        # chiedono il singolo modello con /v1/models/<id>.
        path = self.path.rstrip("/")

        if path.endswith("/models") or path.endswith("/v1/models"):
            # Oggi Cortex risponde 404 su /v1/models e serviamo il nostro file.
            # Ma se un giorno lo implementasse, continuare a servire il file
            # nasconderebbe i modelli nuovi *senza alcun errore*: il guasto
            # peggiore, perche' sembra tutto sano. Quindi si prova prima
            # l'upstream e si ripiega sul file solo se non risponde.
            try:
                upstream = self._upstream_get()
                if upstream.status == 200:
                    body = upstream.read()
                    # Il nostro model_entry pubblica il context length sotto sei
                    # chiavi diverse perche' i client lo cercano sotto nomi
                    # diversi. Se l'upstream non ne pubblica nessuna utile,
                    # Hermes tornerebbe a sbagliare il context: in quel caso
                    # meglio il file nostro, che quel dato lo ha verificato.
                    if _has_context_length(body):
                        self._send_body(200, body)
                        return
                    sys.stderr.write(
                        "upstream /v1/models risponde ma senza context length: "
                        "uso il file locale\n"
                    )
                    sys.stderr.flush()
            except Exception:  # noqa: S110
                pass  # 404, rete, TLS: comportamento storico

            models = [
                model_entry(name, ctx) for name, ctx in cortex_models().items()
            ]
            self._send_body(
                200, json.dumps({"object": "list", "data": models}).encode()
            )
            return

        name = path.rsplit("/", 1)[-1]
        modelli = cortex_models()
        if "/models/" in path and name in modelli:
            self._send_body(
                200, json.dumps(model_entry(name, modelli[name])).encode()
            )
            return

        self._send_body(404, json.dumps({"error": "not found"}).encode())


def selftest(base):
    """Verifica a caldo dopo l'avvio: l'esito finisce nei log del servizio.

    Serve perche' quando il proxy gira come servizio SPCS con endpoint interno non
    e' raggiungibile da fuori: SYSTEM$GET_SERVICE_LOGS e' l'unico modo di sapere
    se funziona senza passare da un altro container.
    """
    import time
    import urllib.request as ur

    time.sleep(2)

    def check(label, fn):
        try:
            fn()
            print("SELFTEST %s: OK" % label, flush=True)
        except Exception as err:
            body = ""
            if isinstance(err, urllib.error.HTTPError):
                body = " body=" + err.read()[:200].decode("utf-8", "replace")
            print("SELFTEST %s: FALLITO (%s)%s" % (label, err, body), flush=True)

    modelli = cortex_models()
    print("SELFTEST modelli dichiarati: %d" % len(modelli), flush=True)

    check("/v1/models", lambda: ur.urlopen(base + "/models", timeout=30).read())

    # Il payload usa max_tokens: se torna 200, la traduzione in
    # max_completion_tokens sta funzionando (Cortex lo rifiuterebbe).
    modello = "claude-sonnet-5" if "claude-sonnet-5" in modelli else sorted(modelli)[0]
    payload = json.dumps(
        {
            "model": modello,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 16,
        }
    ).encode()

    def chat():
        req = ur.Request(
            base + "/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        raw = ur.urlopen(req, timeout=90).read()
        reason = (json.loads(raw).get("choices") or [{}])[0].get("finish_reason")
        if reason != "stop":
            raise RuntimeError("finish_reason=%r, atteso 'stop'" % reason)

    check("chat/completions con max_tokens su %s" % modello, chat)

    # Il caso che il fix del 2026-08-18 indirizza: con una tool call il
    # finish_reason deve essere 'tool_calls', non 'stop'. Se torna 'stop' il
    # client non esegue il tool e lascia in history un toolUse orfano, che
    # Cortex rifiuta alla richiesta successiva con HTTP 400.
    tool_payload = {
        "model": modello,
        "messages": [{"role": "user", "content": "Che tempo fa a Milano?"}],
        "max_tokens": 256,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Meteo corrente per una citta'",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ],
    }

    def tool_call(stream):
        body = dict(tool_payload, stream=stream)
        req = ur.Request(
            base + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if stream else "application/json",
            },
            method="POST",
        )
        raw = ur.urlopen(req, timeout=90).read()

        if not stream:
            choice = (json.loads(raw).get("choices") or [{}])[0]
            reason = choice.get("finish_reason")
            if not (choice.get("message") or {}).get("tool_calls"):
                raise RuntimeError(
                    "il modello non ha invocato il tool (finish_reason=%r)" % reason
                )
            if reason != "tool_calls":
                raise RuntimeError("finish_reason=%r, atteso 'tool_calls'" % reason)
            return

        # Streaming: l'ultimo chunk con finish_reason deve dire 'tool_calls'.
        reasons = []
        for line in raw.split(b"\n"):
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == b"[DONE]":
                continue
            try:
                for ch in (json.loads(chunk).get("choices") or []):
                    if isinstance(ch, dict) and ch.get("finish_reason"):
                        reasons.append(ch["finish_reason"])
            except (ValueError, TypeError):
                pass
        if reasons[-1:] != ["tool_calls"]:
            raise RuntimeError("finish_reason di chiusura=%r, atteso 'tool_calls'" % reasons[-1:])

    check("tool calling non-stream su %s" % modello, lambda: tool_call(False))
    check("tool calling streaming su %s" % modello, lambda: tool_call(True))

    def parallel_tool_calls():
        """Due tool call in un turno: Cortex le manda tutte con index 0.

        Senza reindex_tool_calls il client le fonde in una, esegue un tool solo
        e lascia un toolUse orfano che fa fallire la richiesta dopo con 400.
        """
        body = dict(
            tool_payload,
            stream=True,
            messages=[{
                "role": "user",
                "content": "Dammi il meteo di Roma E di Milano. "
                           "Chiama get_weather una volta per ogni citta.",
            }],
        )
        req = ur.Request(
            base + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        raw = ur.urlopen(req, timeout=90).read()

        ids, indici = [], set()
        for line in raw.split(b"\n"):
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == b"[DONE]":
                continue
            try:
                for ch in (json.loads(chunk).get("choices") or []):
                    if not isinstance(ch, dict):
                        continue
                    for frag in (ch.get("delta") or {}).get("tool_calls") or []:
                        if frag.get("id"):
                            ids.append(frag["id"])
                        if frag.get("index") is not None:
                            indici.add(frag["index"])
            except (ValueError, TypeError):
                pass

        if len(ids) < 2:
            raise RuntimeError(
                "il modello ha invocato %d tool call, servono 2 per il test"
                % len(ids)
            )
        atteso = set(range(len(ids)))
        if indici != atteso:
            raise RuntimeError(
                "%d tool call ma indici %s, attesi %s"
                % (len(ids), sorted(indici), sorted(atteso))
            )

    check("tool call parallele su %s" % modello, parallel_tool_calls)


def main():
    if not os.path.exists(TOKEN_PATH):
        sys.exit("session token non trovato in %s (fuori da SPCS?)" % TOKEN_PATH)

    host, port = LISTEN_ADDR
    print("proxy Cortex su http://%s:%d -> %s" % (host, port, CORTEX_BASE), flush=True)

    if os.environ.get("CORTEX_PROXY_SELFTEST", "0") == "1":
        import threading

        # Si interroga via 127.0.0.1 anche quando il bind e' 0.0.0.0.
        threading.Thread(
            target=selftest, args=("http://127.0.0.1:%d/v1" % port,), daemon=True
        ).start()

    ThreadingHTTPServer(LISTEN_ADDR, CortexProxy).serve_forever()


if __name__ == "__main__":
    main()
