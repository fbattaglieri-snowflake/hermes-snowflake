#!/usr/bin/env python3
"""Check which deviations from the OpenAI wire protocol are still present on the Cortex REST API.

What it is for: cortex_proxy.py exists only to work around five gateway defects.
When Snowflake fixes one of them, the corresponding fix in the proxy becomes useless — and in
one case (the model list) it becomes actively harmful, because it would hide new models
without reporting an error. This script tells you, with real calls, which fixes are still needed.

Usage:
    CORTEX_PAT="..." python3 cortex_wire_check.py
    CORTEX_PAT="..." python3 cortex_wire_check.py --host <other-account>.snowflakecomputing.com

It changes nothing: it only issues minimal read/completion requests.
Compare the output with the baseline recorded in tooling/README.md.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_HOST = os.environ.get("SNOWFLAKE_HOST", "localhost")
# Claude model: the family that suffers the most (it goes through the translation layer).
CLAUDE = "claude-sonnet-5"
# Model subject to the tools+reasoning_effort constraint.
REASONING = "openai-gpt-5.6-luna"

OK, BROKEN, UNKNOWN = "FIXED", "STILL PRESENT", "UNDETERMINED"
INFO = "INFORMATIONAL"


def call(host, pat, path, payload=None, method=None, extra_headers=None):
    """(status, parsed_or_text). Does not raise on 4xx/5xx."""
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
        return 0, "local exception: %s" % exc
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
    """Problem 1: max_tokens rejected. Fix in the proxy: adapt_payload()."""
    status, parsed = chat(host, pat, model=CLAUDE, max_tokens=16)
    if status == 200:
        return OK, "max_tokens accepted", "adapt_payload(): the rewrite is no longer needed"
    msg = parsed.get("message", parsed) if isinstance(parsed, dict) else parsed
    return BROKEN, "HTTP %s — %s" % (status, str(msg)[:110]), "keep the rewrite"


def t2_models_list(host, pat):
    """Problem 2: GET /v1/models -> 404. Fix in the proxy: do_GET() serves the list from file.

    WARNING: this is the only fix that becomes HARMFUL once the gateway is repaired.
    The proxy would keep serving its own file, silently hiding new models.
    """
    status, parsed = call(host, pat, "/models")
    if status == 200:
        n = len(parsed.get("data", [])) if isinstance(parsed, dict) else "?"
        return (OK, "HTTP 200, %s models" % n,
                "URGENT: do_GET() must be changed to 'try upstream, fall back to the file', "
                "otherwise it hides new models")
    return BROKEN, "HTTP %s" % status, "keep serving the list from the file"


def t3_finish_reason(host, pat):
    """Problem 3: empty finish_reason. Fix in the proxy: normalize_finish_reason()/stop_chunk().

    Three sub-cases, because the correct value differs in each:
    complete -> stop, tool call -> tool_calls, truncated -> length.
    The current fix always forces 'stop': it is LOSSY, marking a truncated response as complete.
    """
    results = {}

    _, parsed = chat(host, pat, model=CLAUDE, max_completion_tokens=16)
    results["complete (expected 'stop')"] = first_choice(parsed).get("finish_reason")

    tool = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
    _, parsed = chat(
        host, pat, model=CLAUDE, max_completion_tokens=200, tools=[tool],
        messages=[{"role": "user", "content": "What is the weather in Milan? Use the tool."}],
    )
    choice = first_choice(parsed)
    results["tool call (expected 'tool_calls')"] = choice.get("finish_reason")
    has_tool_calls = bool((choice.get("message") or {}).get("tool_calls"))

    _, parsed = chat(
        host, pat, model=CLAUDE, max_completion_tokens=5,
        messages=[{"role": "user", "content": "Write a long essay on the history of Rome."}],
    )
    results["truncated (expected 'length')"] = first_choice(parsed).get("finish_reason")

    detail = "; ".join("%s -> %r" % (k, v) for k, v in results.items())
    detail += "; tool_calls populated: %s" % has_tool_calls
    values = list(results.values())

    if all(v for v in values) and values[0] == "stop":
        return OK, detail, "normalize_finish_reason() and stop_chunk() disable themselves"
    if any(v for v in values):
        return UNKNOWN, detail, "partially fixed: re-read the code before touching it"
    return (BROKEN, detail,
            "keep the fix, BUT improve it: derive 'tool_calls' from message.tool_calls and "
            "'length' from usage.completion_tokens >= the requested max (see tooling/README.md)")


def t4_tools_reasoning(host, pat):
    """Problem 4: tools + reasoning_effort incompatible. Fix: list + adaptive retry."""
    tool = {
        "type": "function",
        "function": {
            "name": "noop",
            "description": "Does nothing",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    status, parsed = chat(host, pat, model=REASONING, max_completion_tokens=16, tools=[tool])
    if status == 200:
        return (OK, "%s accepts tools without forcing reasoning_effort" % REASONING,
                "empty out tools_require_reasoning_effort_none in cortex_models.json "
                "(just re-run refresh_cortex_models.py --write --upload)")
    msg = parsed.get("message", parsed) if isinstance(parsed, dict) else parsed
    if status == 400 and "unknown model" in str(msg).lower():
        return UNKNOWN, "%s no longer available" % REASONING, "retry with another reasoning model"
    return BROKEN, "HTTP %s — %s" % (status, str(msg)[:110]), "keep list + adaptive retry"


def t5_responses_api(host, pat):
    """Extra: /v1/responses endpoint check."""
    status, parsed = call(host, pat, "/responses", {"model": CLAUDE, "input": "ping"})
    if status == 200:
        return OK, "HTTP 200", "/v1/responses endpoint is available"
    msg = parsed.get("message", parsed) if isinstance(parsed, dict) else parsed
    return BROKEN, "HTTP %s — %s" % (status, str(msg)[:110]), "endpoint not available"


def t6_anthropic_endpoint(host, pat):
    """Extra: the Anthropic endpoint does not suffer from problems 1 and 3. Useful to compare."""
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
    return (INFO, "stop_reason -> %r (max_tokens accepted)" % reason,
            "an alternative for Claude models without the translation layer, see tooling/README.md")


TESTS = [
    ("1. max_tokens rejected",                t1_max_tokens),
    ("2. GET /v1/models -> 404",              t2_models_list),
    ("3. finish_reason not populated",        t3_finish_reason),
    ("4. tools + reasoning_effort",           t4_tools_reasoning),
    ("5. /v1/responses not enabled",          t5_responses_api),
    ("6. Anthropic endpoint (comparison)",    t6_anthropic_endpoint),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--pat-file", help="file containing the PAT")
    args = parser.parse_args()

    if args.pat_file:
        pat = open(args.pat_file).read().strip()
    else:
        pat = os.environ.get("CORTEX_PAT", "").strip()
    if not pat:
        sys.exit("the PAT is required: export CORTEX_PAT or use --pat-file")

    print("host: %s\n" % args.host)
    verdicts = {}
    for label, fn in TESTS:
        try:
            verdict, detail, action = fn(args.host, pat)
        except Exception as err:
            verdict, detail, action = UNKNOWN, "error in test: %s" % err, "-"
        verdicts[label] = verdict
        print("%-36s %s" % (label, verdict))
        print("    finding: %s" % detail)
        print("    action:  %s\n" % action)

    risolti = [k for k, v in verdicts.items() if v == OK]
    print("=" * 78)
    if not risolti:
        print("No change: the proxy is still needed in full.")
    else:
        print("SOMETHING CHANGED (%d entries): the proxy must be updated." % len(risolti))
        for k in risolti:
            print("  - %s" % k)
        print("\nFollow the migration section of tooling/README.md")


if __name__ == "__main__":
    main()
