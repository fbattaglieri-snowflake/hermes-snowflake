#!/usr/bin/env python3
"""
OpenAI-compatible proxy -> Snowflake Cortex, for use inside SPCS.

Why it is needed: from SPCS the only auth accepted by the Cortex REST API is OAuth
with the session token, and it requires the X-Snowflake-Authorization-Token-Type:
OAUTH header. Standard OpenAI clients (Hermes included) only send
"Authorization: Bearer <key>", so this proxy rewrites the headers and forwards
the request.

Bonus: it re-reads /snowflake/session/token on every request, so the token never
expires (SPCS renews it automatically on the filesystem).

Usage:
    nohup python3 /root/.hermes/cortex_proxy.py > /tmp/cortex_proxy.log 2>&1 &

Then point the client at http://127.0.0.1:8080/v1
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

# Configurable bind: 127.0.0.1 inside the Hermes container (the client is local),
# 0.0.0.0 when the proxy runs as a standalone SPCS service and must be reachable
# from other services via internal DNS.
LISTEN_ADDR = (
    os.environ.get("CORTEX_PROXY_BIND", "127.0.0.1"),
    int(os.environ.get("CORTEX_PROXY_PORT", "8080")),
)

# Single source of truth for the models, shared with hermes_configure.py: if the two
# lists diverged, Hermes would declare a context length different from the one
# announced here on /v1/models. See cortex_models.json for the list and the checks.
#
# They are looked up in order: explicit path, volume from a Snowflake stage (updatable
# with a PUT, without rebuilding the image), copy inside the image as a fallback.
MODELS_PATHS = [
    p
    for p in (
        os.environ.get("CORTEX_MODELS_PATH"),
        "/models/cortex_models.json",
        "/opt/cortex_models.json",
    )
    if p
]

# Minimal fallback if no file is readable: better two known-good models than none.
FALLBACK_MODELS = {"claude-sonnet-5": 1000000, "claude-opus-5": 1000000}

# File cache: we re-read only when the mtime changes, so a stage update is picked up
# without restarting the service and without re-reading on every request.
_models_cache = {"path": None, "mtime": None, "models": None, "no_reasoning": None}


def _read_models_file():
    """Return (models, tools_require_reasoning_effort_none) from the first readable file."""
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
            sys.stderr.write("%s unreadable (%s), trying the next one\n" % (path, err))
            continue

        _models_cache.update(
            path=path, mtime=stat.st_mtime, models=models, no_reasoning=no_reasoning
        )
        sys.stderr.write(
            "model list loaded from %s (%d models)\n" % (path, len(models))
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


# Message with which Cortex rejects tools+reasoning: used for the adaptive retry.
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
    """Model descriptor with every context length alias known to Hermes.

    Hermes tries 12 different keys in order; we publish the main ones so the
    probe finds the correct value whichever alias it looks for.
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
    """True if an upstream /v1/models response declares the context length.

    Without at least one of the aliases Hermes looks for, the upstream list would
    be a regression compared to our file: Hermes would not know the models'
    window and would go back to estimating it badly.
    """
    try:
        data = (json.loads(raw) or {}).get("data") or []
    except (ValueError, TypeError):
        return False
    return any(
        isinstance(m, dict) and any(m.get(k) for k in CONTEXT_KEYS) for m in data
    )


def read_token():
    """Re-read the token on every call: SPCS rotates it on the filesystem."""
    with open(TOKEN_PATH) as fh:
        return fh.read().strip()


def adapt_payload(payload):
    """Adapt the OpenAI body to the differences of the Cortex wire format.

    1) Cortex rejects 'max_tokens' with HTTP 400 "max_tokens is deprecated in favor
    of max_completion_tokens". Hermes sends 'max_tokens' for every model that does
    not belong to the OpenAI families (see model_forces_max_completion_tokens
    in utils.py), so for claude-*, mistral-*, qwen3-* and the like the request
    would always fail. Hermes then interprets that 400 as a context overflow and
    reports the misleading "Context length exceeded (N tokens)".

    2) The gpt-5.6-* models reject 'tools' if reasoning_effort is not explicitly
    "none": "Function tools with reasoning_effort are not supported". Omitting it
    is NOT enough, the gateway applies a default. Without this rewrite those
    models cannot use tools, that is, they are useless for an agent.

    Neither of these two things is configurable on the Hermes side: that is why
    this proxy exists.
    """
    if not payload:
        return payload, None
    try:
        body = json.loads(payload)
    except (ValueError, TypeError):
        return payload, None  # non-JSON: forward unchanged
    if not isinstance(body, dict):
        return payload, None

    if "max_tokens" in body:
        value = body.pop("max_tokens")
        # If the client already sent the new key, its value wins.
        body.setdefault("max_completion_tokens", value)

    collapse_parallel_tool_calls(body)

    model = str(body.get("model") or "")
    if body.get("tools") and model in tools_need_no_reasoning():
        body["reasoning_effort"] = "none"

    return json.dumps(body).encode(), body


def collapse_parallel_tool_calls(body):
    """Collapse turns with more than one tool call: Cortex rejects them.

    Cortex converts every 'tool' message into a separate turn, so an assistant
    message with N toolUse blocks receives only 1 toolResult in the first turn and
    the request dies with HTTP 400 "Each 'toolUse' block must be accompanied
    with a matching 'toolResult' block", which is not retryable.

    The turn stays in the persisted history, so the failure is permanent: every
    subsequent message in the same session fails.

    We keep the first tool call and merge the outputs of the others into its
    toolResult as text: the 1:1 constraint is respected and nothing is lost.
    """
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return False
    out, changed, i = [], False, 0
    while i < len(msgs):
        m = msgs[i]
        tcs = m.get("tool_calls") if isinstance(m, dict) else None
        if not (isinstance(tcs, list) and len(tcs) > 1):
            out.append(m)
            i += 1
            continue
        ids = [str(tc.get("id") or "") for tc in tcs if isinstance(tc, dict)]
        results, j = {}, i + 1
        while j < len(msgs) and isinstance(msgs[j], dict) and msgs[j].get("role") == "tool":
            results[str(msgs[j].get("tool_call_id") or "")] = msgs[j]
            j += 1
        if len(results) < 2:
            out.append(m)
            i += 1
            continue
        keep = dict(m)
        keep["tool_calls"] = [tcs[0]]
        first = results.get(ids[0]) or list(results.values())[0]
        merged = dict(first)
        extra = [str(results[k].get("content")) for k in ids[1:] if k in results]
        if extra:
            nota = "[output of parallel tool calls, merged by the proxy]"
            merged["content"] = "\n\n".join(
                [str(first.get("content")), nota] + extra
            )
        out.append(keep)
        out.append(merged)
        changed = True
        i = j
    if changed:
        body["messages"] = out
    return changed


def force_no_reasoning(body):
    """Rewrite the body for the retry: reasoning_effort explicitly disabled."""
    body = dict(body)
    body["reasoning_effort"] = "none"
    return json.dumps(body).encode()


def infer_finish_reason(choice, requested_max, usage):
    """Infer the finish_reason that Cortex does not send, instead of forcing 'stop'.

    Cortex collapses three distinct cases into "": complete response, tool call and
    response truncated by the token limit. Forcing 'stop' on all three is lossy
    in two ways, both observed:

      - a client that branches on finish_reason == 'tool_calls' does not run the
        tool. The assistant message with the toolUse block ends up in the history
        anyway, which is left without the corresponding toolResult: the next
        request is rejected with HTTP 400 "Each 'toolUse' block must be
        accompanied with a matching 'toolResult' block". That is why tool calling
        did not work on the Claude models.
      - a response cut in half by the token limit is marked as complete, so a
        workflow may treat half a sentence as final.

    message.tool_calls and usage.completion_tokens make it possible to reconstruct
    two of the three cases. content_filter and refusal remain indistinguishable,
    which are rare cases: we go from "always wrong" to "rarely wrong".
    """
    if (choice.get("message") or {}).get("tool_calls"):
        return "tool_calls"
    done = (usage or {}).get("completion_tokens")
    if requested_max and done and done >= requested_max:
        return "length"
    return "stop"


def normalize_finish_reason(raw, requested_max=None):
    """Fill in finish_reason where Cortex leaves it empty (non-stream responses).

    Cortex returns "finish_reason": "" for the Claude models (for gpt-5.6 it sends
    "stop" instead). OpenAI clients read that field for two distinct decisions:
    whether the response is complete, and whether they must run a tool. See
    infer_finish_reason for the details of the cases.
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
    """Synthetic SSE chunk that closes the stream according to the OpenAI spec.

    reason must be set to 'tool_calls' if any delta carried a tool call,
    otherwise the client does not run it (see infer_finish_reason). In streaming
    'length' cannot be inferred: Cortex's SSE chunks do not carry usage.
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
    """Rewrite the index of the streaming tool_calls deltas.

    On the Claude models, Cortex marks ALL parallel tool calls with index 0.
    Measured on 2026-08-18 on claude-sonnet-5, two tool calls in one turn:
    seven fragments, all with index 0, the second 'id' appears at fragment 4
    still on index 0. The client reassembles by index, merges the two calls
    into one, runs a single tool and sends back one toolResult for two toolUse
    blocks: Cortex rejects the next request with HTTP 400
    "Each 'toolUse' block must be accompanied with a matching 'toolResult'".

    A fragment with a non-empty 'id' opens a new tool call, the following ones
    carry only the arguments chunk with empty id and name. We count the distinct
    ids and use the counter as the index. Comparing against last_id makes the
    operation idempotent: on the models that already index correctly
    (verified on openai-gpt-5.2, indexes [0, 1]) the recomputed indexes
    match the original ones and nothing is touched.
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
    """Process raw SSE bytes: inject stop_chunk if missing, reindex tool calls.

    Used by the tests; in production the same logic runs line by line in the
    handler so the whole stream is not buffered.
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
    """Normalize the client path towards the Cortex endpoint.

    The client may call /v1/chat/completions or /chat/completions:
    in both cases the upstream is <CORTEX_BASE>/chat/completions.
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

    def log_message(self, fmt, *args):  # silences the access log
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
        """GET towards Cortex. Needed because _upstream is pinned to POST."""
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

            # Adaptive retry: some reasoning models reject 'tools' if
            # reasoning_effort is not explicitly "none". The list in
            # cortex_models.json covers the known ones; this branch covers future
            # ones without having to update it.
            if (
                err.code == 400
                and body is not None
                and body.get("tools")
                and body.get("reasoning_effort") != "none"
                and REASONING_TOOLS_ERROR in text.lower()
            ):
                sys.stderr.write(
                    "retrying with reasoning_effort=none for model %r\n"
                    % body.get("model")
                )
                sys.stderr.flush()
                try:
                    response = self._upstream(force_no_reasoning(body))
                except urllib.error.HTTPError as err2:
                    body2 = err2.read()
                    sys.stderr.write(
                        "retry failed HTTP %s: %s\n"
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
                # Cortex's message is the only useful clue when the OpenAI wire
                # format and the Cortex one diverge: it must always be logged.
                sys.stderr.write(
                    "upstream HTTP %s on %s: %s\n" % (err.code, self.path, text[:500])
                )
                sys.stderr.flush()
                self._send_body(err.code, err_body)
                return
        except Exception as err:  # network, DNS, TLS
            self._send_body(
                502, json.dumps({"error": {"message": str(err)}}).encode()
            )
            return

        content_type = response.headers.get("Content-Type", "application/json")
        model = (body or {}).get("model") if isinstance(body, dict) else None

        # SSE streaming: forward line by line (SSE is line-delimited).
        if "text/event-stream" in content_type:
            self.send_response(response.status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            # Without this the server would keep the connection alive: the body
            # has no Content-Length, so the client would not know where it ends.
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
                            # Cortex never sends a chunk with finish_reason: the
                            # OpenAI spec requires it on the last one, and without
                            # it the client considers the response truncated and
                            # attempts continuations (duplicated text). We inject
                            # it here, with 'tool_calls' if the stream carried one:
                            # otherwise the client does not run the tool and leaves
                            # an orphan toolUse in the history, which Cortex
                            # rejects on the next request.
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
        # Some clients query /v1/models during the handshake, others ask for the
        # single model with /v1/models/<id>.
        path = self.path.rstrip("/")

        if path.endswith("/models") or path.endswith("/v1/models"):
            # Today Cortex answers 404 on /v1/models and we serve our own file.
            # But if one day it implemented it, continuing to serve the file
            # would hide the new models *without any error*: the worst kind of
            # failure, because everything looks healthy. So we try the upstream
            # first and fall back to the file only if it does not answer.
            try:
                upstream = self._upstream_get()
                if upstream.status == 200:
                    body = upstream.read()
                    # Our model_entry publishes the context length under six
                    # different keys because clients look for it under different
                    # names. If the upstream publishes none of the useful ones,
                    # Hermes would go back to getting the context wrong: in that
                    # case our file is better, since that value was verified.
                    if _has_context_length(body):
                        self._send_body(200, body)
                        return
                    sys.stderr.write(
                        "upstream /v1/models answers but without context length: "
                        "using the local file\n"
                    )
                    sys.stderr.flush()
            except Exception:  # noqa: S110
                pass  # 404, network, TLS: historical behaviour

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
    """Hot check after startup: the outcome ends up in the service logs.

    Needed because when the proxy runs as an SPCS service with an internal endpoint
    it is not reachable from outside: SYSTEM$GET_SERVICE_LOGS is the only way to
    know whether it works without going through another container.
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
            print("SELFTEST %s: FAILED (%s)%s" % (label, err, body), flush=True)

    modelli = cortex_models()
    print("SELFTEST declared models: %d" % len(modelli), flush=True)

    check("/v1/models", lambda: ur.urlopen(base + "/models", timeout=30).read())

    # The payload uses max_tokens: if it returns 200, the translation into
    # max_completion_tokens is working (Cortex would reject it).
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
            raise RuntimeError("finish_reason=%r, expected 'stop'" % reason)

    check("chat/completions with max_tokens on %s" % modello, chat)

    # The case that the 2026-08-18 fix addresses: with a tool call the
    # finish_reason must be 'tool_calls', not 'stop'. If it returns 'stop' the
    # client does not run the tool and leaves an orphan toolUse in the history,
    # which Cortex rejects on the next request with HTTP 400.
    tool_payload = {
        "model": modello,
        "messages": [{"role": "user", "content": "What is the weather in Milan?"}],
        "max_tokens": 256,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Current weather for a city",
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
                    "the model did not invoke the tool (finish_reason=%r)" % reason
                )
            if reason != "tool_calls":
                raise RuntimeError("finish_reason=%r, expected 'tool_calls'" % reason)
            return

        # Streaming: the last chunk with finish_reason must say 'tool_calls'.
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
            raise RuntimeError("closing finish_reason=%r, expected 'tool_calls'" % reasons[-1:])

    check("non-stream tool calling on %s" % modello, lambda: tool_call(False))
    check("streaming tool calling on %s" % modello, lambda: tool_call(True))

    def parallel_tool_calls():
        """Two tool calls in one turn: Cortex sends them all with index 0.

        Without reindex_tool_calls the client merges them into one, runs only one
        tool and leaves an orphan toolUse that makes the next request fail with 400.
        """
        body = dict(
            tool_payload,
            stream=True,
            messages=[{
                "role": "user",
                "content": "Give me the weather for Rome AND for Milan. "
                           "Call get_weather once for each city.",
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
                "the model invoked %d tool calls, 2 are needed for the test"
                % len(ids)
            )
        atteso = set(range(len(ids)))
        if indici != atteso:
            raise RuntimeError(
                "%d tool calls but indexes %s, expected %s"
                % (len(ids), sorted(indici), sorted(atteso))
            )

    check("parallel tool calls on %s" % modello, parallel_tool_calls)


def main():
    if not os.path.exists(TOKEN_PATH):
        sys.exit("session token not found at %s (outside SPCS?)" % TOKEN_PATH)

    host, port = LISTEN_ADDR
    print("Cortex proxy on http://%s:%d -> %s" % (host, port, CORTEX_BASE), flush=True)

    if os.environ.get("CORTEX_PROXY_SELFTEST", "0") == "1":
        import threading

        # It is queried via 127.0.0.1 even when the bind is 0.0.0.0.
        threading.Thread(
            target=selftest, args=("http://127.0.0.1:%d/v1" % port,), daemon=True
        ).start()

    ThreadingHTTPServer(LISTEN_ADDR, CortexProxy).serve_forever()


if __name__ == "__main__":
    main()
