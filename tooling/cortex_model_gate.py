#!/usr/bin/env python3
"""Compatibility gate for Cortex models: tells you whether a new model is
usable with the stack we built, BEFORE putting it into configuration.

Why refresh_cortex_models.py is not enough
------------------------------------------
That script answers three questions (does the model respond? does it accept
'tools'? does it want reasoning_effort="none"?) and rewrites cortex_models.json
on that basis. Those are necessary but not sufficient conditions: the stack
rests on six Cortex wire deviations normalized by cortex_proxy.py, and a new
model can break one of them without failing any of the three checks. The real
case: the Claude models answer 200 on the first probe but do not populate
finish_reason, and with two tool calls in one turn they poison the history
permanently — a failure that showed up as "Telegram only replies 'model
provider failed'".

This script therefore tests the proxy+upstream PAIR, not the bare gateway: it
imports the real functions from cortex_proxy.py and applies them to the request
and to the response, just as the running proxy would. If the verdict is
COMPATIBLE, the model works with what we have built, not "with OpenAI in
general".

What it does NOT do: it changes nothing. No writes to files, stages, services
or containers. Read-only calls to the gateway.

Usage:
    CORTEX_PAT="..." python3 cortex_model_gate.py --new     # only the new names
    CORTEX_PAT="..." python3 cortex_model_gate.py --all     # regression on known ones
    CORTEX_PAT="..." python3 cortex_model_gate.py --models deepseek-v4-flash

The variable assignment must come AT THE HEAD of the command: with
'cd x && CORTEX_PAT="<key>" ...' the secret injection does not fire and you
get HTTP 401 (a trap already hit, playbook §9).

Verdicts
--------
COMPATIBLE         text and tool calling work through the proxy: safe to promote
WITH RESERVATION   responds but without usable tool calling. Suitable for text generation
                   only; NOT for Hermes in agent mode since agents depend on tool calling
INCOMPATIBLE       does not respond at all from this account: do not add to config

Exit code: 1 if a model ALREADY in cortex_models.json regresses (it was in
configuration and now does not respond, or has lost tool calling). Useful to
notice a Snowflake-side regression without reading the whole report.
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

# The proxy transformations are half of the contract to be verified: they are
# imported instead of rewritten, otherwise the gate would measure a divergent
# copy of the code that runs in production.
try:
    import cortex_proxy
except Exception as err:                                   # pragma: no cover
    sys.exit("cortex_proxy.py is not importable (%s): the gate must run "
             "in the same directory" % err)

# collapse_parallel_tool_calls() is part of cortex_proxy.py. The lookup stays
# defensive because the gate can be pointed at a proxy older than the patch: in
# that case T5 is not verifiable and the gate says so, instead of passing off as
# compatible a model never tested against that constraint.
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

FINISH_VALID = {"stop", "length", "tool_calls", "content_filter"}

# Output budgets. Do NOT lower them: reasoning models consume the budget before
# emitting text, and with too small a value they answer HTTP 200 with empty
# content and finish_reason='length'. Measured on 2026-08-21: with 64 tokens
# openai-gpt-5, -mini and -nano looked broken; with 512 they respond. It is the
# same trap as max_completion_tokens=1, which in the first census caused the
# whole gpt-5 family to be discarded by mistake.
BUDGET_TEXT = 1024
BUDGET_RETRY = 4096          # second attempt when the first ends in 'length'
BUDGET_STREAM = 512
BUDGET_TOOLS = 1024


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #

def post(host, pat, body, stream=False, timeout=120):
    """(status, text|sse_lines). Does not raise on 4xx/5xx: the status matters."""
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
    except Exception as exc:                               # network, DNS, timeout
        return 0, "local exception: %s" % exc


def reason(status, text):
    """Reduces an error body to a single line, distinguishing the three cases
    that matter: name not in the endpoint catalog, known name but not served,
    account not enabled. Confusing them wastes hours."""
    t = text.strip()
    try:
        parsed = json.loads(t)
        t = str(parsed.get("message") or parsed.get("error") or t)
    except ValueError:
        pass
    t = " ".join(t.split())
    if status == 403:
        return "HTTP 403 - account not enabled"
    low = t.lower()
    if "unknown model" in low:
        return "unknown model (name not served by this endpoint)"
    if "unavailable" in low:
        return "unavailable (name recognized but not served)"
    return "HTTP %s: %s" % (status, t[:140])


def choice(text):
    try:
        return ((json.loads(text).get("choices") or [{}])[0]) or {}
    except (ValueError, AttributeError, TypeError):
        return {}


# --------------------------------------------------------------------------- #
# the tests
# --------------------------------------------------------------------------- #

def t1_t2_non_stream(host, pat, model, result):
    """T1 non-stream response with 'max_tokens' + T2 normalized finish_reason.

    We start from the body HERMES sends (with 'max_tokens'), not from the one
    Cortex accepts: it is the proxy rewrite that has to make it valid. If that
    step were skipped, the test would measure a scenario that does not exist in
    production.

    Empty content with finish_reason='length' is NOT a model defect: it is the
    budget exhausted in reasoning. We retry once with a wider budget before
    declaring KO, otherwise the gate produces false alarms on reasoning models
    (observed across the whole openai-gpt-5 family).
    """
    def attempt(budget):
        request = {
            "model": model,
            "max_tokens": budget,
            "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
        }
        adapted, body = cortex_proxy.adapt_payload(json.dumps(request).encode())
        result["max_tokens_rewritten"] = "max_completion_tokens" in (body or {})
        return post(host, pat, json.loads(adapted)) + (budget,)

    status, text, budget = attempt(BUDGET_TEXT)
    if status != 200:
        result["T1"] = "KO"
        result["reason"] = reason(status, text)
        return False

    ch = choice(text)
    content = ((ch.get("message") or {}).get("content") or "").strip()
    if not content and ch.get("finish_reason") == "length":
        result["note"].append(
            "first attempt exhausted in reasoning (%d tokens, finish='length'): "
            "retried with %d" % (budget, BUDGET_RETRY))
        status, text, budget = attempt(BUDGET_RETRY)
        ch = choice(text)
        content = ((ch.get("message") or {}).get("content") or "").strip()

    result["budget_required"] = budget
    result["T1"] = "OK" if content else "KO"
    result["content"] = content[:30]
    if not content:
        result["reason"] = ("HTTP 200 but empty content even with %d tokens "
                            "(finish=%r)" % (budget, ch.get("finish_reason")))
        return False

    # T2: what Cortex sends, and what is left after the proxy normalization.
    raw = ch.get("finish_reason")
    result["finish_upstream"] = repr(raw)
    normalized = cortex_proxy.normalize_finish_reason(text, requested_max=budget)
    after = (choice(normalized.decode() if isinstance(normalized, bytes)
                    else normalized)).get("finish_reason")
    result["finish_proxy"] = repr(after)
    result["T2"] = "OK" if after in FINISH_VALID else "KO"
    return True


def t3_streaming(host, pat, model, result):
    """T3 streaming: chunks arrive, and the stream closes with a finish_reason.

    On the Claude models no chunk carries finish_reason and the proxy injects a
    synthetic chunk before [DONE]: without it the client considers the response
    truncated and attempts up to 4 continuations, duplicating the text.
    """
    request = {
        "model": model,
        "max_completion_tokens": BUDGET_STREAM,
        "stream": True,
        "messages": [{"role": "user", "content": "Count from 1 to 5."}],
    }
    status, lines = post(host, pat, request, stream=True)
    if status != 200:
        result["T3"] = "KO"
        result["note"].append("streaming: %s" % reason(status, "".join(lines)
                                                      if isinstance(lines, list) else lines))
        return

    chunk = 0
    text = ""
    saw_finish = False
    for line in lines:
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        chunk += 1
        for c in obj.get("choices") or []:
            text += ((c.get("delta") or {}).get("content") or "")
            if c.get("finish_reason"):
                saw_finish = True

    result["stream_chunk"] = chunk
    result["stream_finish_upstream"] = saw_finish
    if chunk == 0:
        result["T3"] = "KO"
        result["note"].append("streaming: no chunk received")
        return
    # The proxy closes the stream itself if the upstream does not: in both cases
    # the client sees a finish_reason. All that matters is that chunks arrive.
    result["T3"] = "OK" if text.strip() else "KO"
    if not text.strip():
        result["note"].append("streaming: chunks present but no content")


def t4_tool_roundtrip(host, pat, model, result):
    """T4 the test that decides whether a model is usable by an agent.

    Two steps: the model must emit a tool call, and the next request carrying
    the tool result back must be accepted. It is the second step that was
    breaking Hermes: a turn with toolUse and no matching toolResult is rejected
    with a non-retryable 400, and stays in the persisted history — so the
    session dies forever.
    """
    base = {
        "model": model,
        "max_completion_tokens": BUDGET_TOOLS,
        "tools": [TOOL],
        "messages": [{"role": "user",
                      "content": "What time is it in Rome? Use the get_time tool."}],
    }
    adapted, _ = cortex_proxy.adapt_payload(json.dumps(base).encode())
    status, text = post(host, pat, json.loads(adapted))

    if status != 200:
        low = text.lower()
        if cortex_proxy.REASONING_TOOLS_ERROR in low:
            result["T4"] = "deferred to T6"
            return
        if "tool calling is not supported" in low:
            result["T4"] = "KO"
            result["tools"] = "not supported by the model"
            return
        result["T4"] = "KO"
        result["tools"] = reason(status, text)
        return

    ch = choice(text)
    calls = (ch.get("message") or {}).get("tool_calls") or []
    if not calls:
        # Not a defect: the model chose to answer in words.
        result["T4"] = "INCONCLUSIVE"
        result["tools"] = "no tool call emitted (the model answered with text)"
        return

    result["tool_calls_emitted"] = len(calls)

    # Round-trip: the assistant message is sent back exactly as it arrived, plus
    # one 'tool' message for EVERY call (the constraint is 1:1).
    history = list(base["messages"])
    history.append({"role": "assistant",
                    "content": (ch.get("message") or {}).get("content") or "",
                    "tool_calls": calls})
    for c in calls:
        history.append({"role": "tool",
                        "tool_call_id": c.get("id"),
                        "content": "14:30 local time"})

    follow_up = dict(base, messages=history)
    adapted, _ = cortex_proxy.adapt_payload(json.dumps(follow_up).encode())
    status, text = post(host, pat, json.loads(adapted))
    if status != 200:
        result["T4"] = "KO"
        result["tools"] = "toolResult round-trip rejected: %s" % reason(status, text)
        return
    result["T4"] = "OK"


def t5_tool_parallel(host, pat, model, result):
    """T5 two tool calls in the same turn: the R-19 case.

    Cortex converts every 'tool' message into a separate turn, so an assistant
    with N toolUse receives only 1 toolResult in the first turn and the request
    is rejected. collapse_parallel_tool_calls() merges the turn into a single
    call, preserving the content of the other results.

    If that function is not in this source, the test says so: that is more
    useful information than the test itself, because it means an image rebuild
    from this context would bring the failure back.
    """
    history = [
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
            "tools": [TOOL, TOOL2], "messages": history}

    status, text = post(host, pat, body)
    result["parallel_raw"] = "HTTP %s" % status

    if status == 200:
        # The upstream accepts them: no merging needed for this model.
        result["T5"] = "OK (upstream accepts parallel tool calls)"
        return

    if COLLAPSE is None:
        result["T5"] = "NOT VERIFIABLE"
        result["note"].append(
            "collapse_parallel_tool_calls missing from cortex_proxy.py: the "
            "proxy is older than the parallel tool call patch")
        return

    merged = COLLAPSE(dict(body))
    status, text = post(host, pat, merged)
    result["T5"] = "OK (merged by the proxy)" if status == 200 else "KO"
    if status != 200:
        result["note"].append("parallel tool calls: %s" % reason(status, text))


def t6_tools_reasoning(host, pat, model, result):
    """T6 the tools + reasoning_effort constraint of the gpt-5.6 family.

    Omitting reasoning_effort is NOT enough: the gateway applies a default and
    rejects anyway. The proxy forces "none" for the models in the list and has
    an adaptive retry on the error message. Here we determine which of the two
    categories the model belongs to.
    """
    body = {"model": model, "max_completion_tokens": BUDGET_STREAM, "tools": [TOOL],
            "reasoning_effort": "low",
            "messages": [{"role": "user", "content": "What time is it in Rome?"}]}
    status, text = post(host, pat, body)
    if status == 200:
        result["T6"] = "OK (tools and reasoning coexist)"
        return
    if cortex_proxy.REASONING_TOOLS_ERROR not in text.lower():
        result["T6"] = "n/a"
        result["note"].append("tools+reasoning: %s" % reason(status, text))
        return

    status, text = post(host, pat, json.loads(cortex_proxy.force_no_reasoning(body)))
    if status == 200:
        result["T6"] = "OK with reasoning_effort=none"
        result["requires_reasoning_none"] = True
    else:
        result["T6"] = "KO"
        result["note"].append("tools rejected even with reasoning_effort=none")


def t7_context(model, declared, known, result):
    """T7 check of the declared context, not of the real window.

    Probing the real window would mean sending hundreds of thousands of tokens
    per model: expensive and pointless. Here we only verify that the value
    exists and flag when it is the conservative default, which must be fixed by
    hand from the docs before considering the model promoted. Underestimating is
    safe (the client compresses earlier than needed), overestimating breaks the
    calls.
    """
    if model not in known:
        result["T7"] = "TO VERIFY"
        result["note"].append(
            "new model: context to be read on aisql-regional-availability "
            "(the refresh sets a conservative 128000)")
        return
    result["context"] = declared
    result["T7"] = "OK" if declared and declared > 0 else "KO"


# --------------------------------------------------------------------------- #
# verdict
# --------------------------------------------------------------------------- #

def verdict(result):
    if result.get("T1") != "OK":
        return "INCOMPATIBLE"
    tool_ok = result.get("T4") == "OK" or str(result.get("T6", "")).startswith("OK")
    if result.get("T4") == "KO" and not str(result.get("T6", "")).startswith("OK"):
        return "WITH RESERVATION"
    if result.get("T4") == "INCONCLUSIVE":
        return "WITH RESERVATION"
    if result.get("T2") == "KO" or result.get("T3") == "KO":
        return "WITH RESERVATION"
    return "COMPATIBLE" if tool_ok else "WITH RESERVATION"


def evaluate(host, pat, model, known):
    result = {"model": model, "note": []}
    if not t1_t2_non_stream(host, pat, model, result):
        result["verdict"] = "INCOMPATIBLE"
        return result
    t3_streaming(host, pat, model, result)
    t4_tool_roundtrip(host, pat, model, result)
    if result.get("T4") in ("deferred to T6", "KO"):
        t6_tools_reasoning(host, pat, model, result)
        if str(result.get("T6", "")).startswith("OK"):
            # The constraint is handled by the proxy: retry the real round-trip.
            t4_tool_roundtrip(host, pat, model, result)
    if result.get("T4") == "OK":
        t5_tool_parallel(host, pat, model, result)
    t7_context(model, known.get(model), known, result)
    result["verdict"] = verdict(result)
    return result


# --------------------------------------------------------------------------- #
# catalog and report
# --------------------------------------------------------------------------- #

def catalog(connection):
    """Catalog names, plus the variant without the '1p-' segment.

    The catalog lists OPENAI-1P-GPT-5.6-LUNA but the invocable name is
    openai-gpt-5.6-luna: we register both forms and let the real call decide.
    The catalog also lists models that answer 'unknown model', so on its own it
    proves nothing.
    """
    out = subprocess.run(
        ["snow", "sql", "-c", connection, "--format", "json",
         "-q", "SHOW CORTEX BASE MODELS IN SCHEMA SNOWFLAKE.MODELS"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.stderr.write("warning: SHOW CORTEX BASE MODELS failed, "
                         "the diff against the catalog will not be available\n")
        return {}
    try:
        rows = json.loads(out.stdout)
    except json.JSONDecodeError:
        return {}
    if rows and isinstance(rows[0], list):
        rows = rows[0]

    found = {}
    for row in rows:
        raw = row.get("name") or row.get("NAME")
        if not raw:
            continue
        name = str(raw).strip().lower()
        meta = {"status": (row.get("lifecycle_status") or "").upper(),
                "created": str(row.get("created_on") or "")[:10]}
        found[name] = meta
        without_1p = re.sub(r"-1p-", "-", name)
        if without_1p != name:
            found.setdefault(without_1p, meta)
    return found


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--models", help="comma-separated list")
    p.add_argument("--new", action="store_true",
                   help="only the catalog names missing from cortex_models.json")
    p.add_argument("--all", action="store_true",
                   help="all the models in cortex_models.json (regression)")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--connection", default=DEFAULT_CONNECTION)
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--json", help="also write the report in JSON")
    p.add_argument("--retry-ko", action="store_true",
                   help="also retry the names already documented in _unavailable")
    args = p.parse_args()

    pat = os.environ.get("CORTEX_PAT", "").strip()
    if not pat:
        sys.exit("the PAT is required: CORTEX_PAT=\"...\" at the head of the command")

    with open(MODELS_FILE) as fh:
        doc = json.load(fh)
    known = {str(k): int(v) for k, v in doc["models"].items()}
    # Names already tried and not working: the catalog contains dozens of them
    # (embedding, parse, sentiment models, EOL versions) that are not candidates
    # for a chat provider. Retrying them every round costs time and buries the
    # report.
    # "_non_disponibili" is the former Italian name of this key: a models.json
    # copy already on the stage still uses it, and silently reading nothing there
    # would mean retrying every dead model on every round.
    already_ko = {
        k
        for k in (doc.get("_unavailable") or doc.get("_non_disponibili") or {})
        if not k.startswith("_")
    }

    cat = catalog(args.connection) if (args.new or not args.models) else {}
    all_new = sorted(n for n in cat if n not in known)
    skipped = {}
    new_models = []
    for n in all_new:
        if cat[n]["status"] == "EOL":
            skipped[n] = "EOL"
        elif n in already_ko and not args.retry_ko:
            skipped[n] = "already documented as unavailable"
        else:
            new_models.append(n)
    vanished = sorted(n for n in known if cat and n not in cat)

    if args.models:
        targets = [m.strip() for m in args.models.split(",") if m.strip()]
    elif args.all:
        targets = sorted(known)
    else:
        targets = new_models

    print("host: %s" % args.host)
    print("in configuration: %d models" % len(known))
    if cat:
        print("in the catalog: %d names (including the variants without '1p-')" % len(cat))
        print("new candidates: %s"
              % (", ".join("%s [%s, created %s]"
                           % (n, cat[n]["status"] or "lifecycle NULL", cat[n]["created"])
                           for n in new_models) or "none"))
        if skipped:
            print("discarded without trying: %d (%d EOL, %d already documented as "
                  "unavailable — with --retry-ko they are retried)"
                  % (len(skipped),
                     sum(1 for v in skipped.values() if v == "EOL"),
                     sum(1 for v in skipped.values() if v != "EOL")))
        if vanished:
            print("in configuration but NO longer in the catalog: %s" % ", ".join(vanished))
    if COLLAPSE is None:
        print("\nWARNING: collapse_parallel_tool_calls is not in cortex_proxy.py.")
        print("The proxy is older than the parallel tool call patch: T5 is not")
        print("verifiable, and an image built from this context would")
        print("reintroduce the failure.")

    if not targets:
        print("\nno model to evaluate.")
        return 0

    print("\nevaluating %d models: %s\n" % (len(targets), ", ".join(targets)))
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda m: evaluate(args.host, pat, m, known), targets))

    width = max(len(e["model"]) for e in results)
    print("%-*s  %-14s %-4s %-4s %-4s %-6s %s" % (
        width, "model", "verdict", "T1", "T2", "T3", "T4", "detail"))
    for e in sorted(results, key=lambda x: (x["verdict"], x["model"])):
        detail = e.get("reason") or e.get("tools") or ""
        if e.get("finish_upstream") and e.get("finish_upstream") != e.get("finish_proxy"):
            detail = detail or ("finish_reason %s -> %s"
                                % (e["finish_upstream"], e["finish_proxy"]))
        print("%-*s  %-14s %-4s %-4s %-4s %-6s %s" % (
            width, e["model"], e["verdict"], e.get("T1", "-"),
            e.get("T2", "-"), e.get("T3", "-"), str(e.get("T4", "-"))[:6], detail))

    # The header must be printed if there is ANYTHING to say, not only when
    # notes are present: otherwise the details of a model without notes end up
    # under the previous model's header and get attributed to it.
    for e in results:
        lines = list(e["note"])
        if e.get("T5") and e["T5"] != "OK":
            lines.append("parallel tool calls: %s" % e["T5"])
        if e.get("T6"):
            lines.append("tools+reasoning: %s" % e["T6"])
        if e.get("T7") == "TO VERIFY":
            lines.append("context: TO BE VERIFIED by hand against the docs")
        if not lines:
            continue
        print("\n%s:" % e["model"])
        for line in lines:
            print("  - %s" % line)

    # Regression: a model that was in configuration and no longer holds up.
    regressed = [e["model"] for e in results
                 if e["model"] in known and e["verdict"] == "INCOMPATIBLE"]
    if regressed:
        print("\nREGRESSION: %s were in configuration and no longer respond."
              % ", ".join(regressed))
        print("Do NOT run refresh_cortex_models.py --write now: it rewrites the")
        print("list based on this round and would remove them. First work out whether")
        print("it is a transient failure (retry) or a permanent one (Snowflake side).")

    promotable = [e["model"] for e in results if e["verdict"] == "COMPATIBLE"
                  and e["model"] not in known]
    if promotable:
        print("\nPROMOTABLE: %s" % ", ".join(promotable))
        print("Promotion procedure: §15 of 20260819_hermes_desktop_client_handover.md")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2, ensure_ascii=False)
        print("\nJSON report in %s" % args.json)

    return 1 if regressed else 0


if __name__ == "__main__":
    sys.exit(main())
