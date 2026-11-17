#!/usr/bin/env python3
"""Check which OpenAI wire-protocol deviations remain active on Snowflake Cortex REST API.

Why this script exists: `proxy/cortex_proxy.py` (embedded single-container fallback)
normalizes wire-level differences between standard OpenAI SDKs and raw
`/api/v2/cortex/v1/chat/completions`. When Snowflake updates an upstream behavior,
this read-only probe reports which adaptations are still required.

Usage:
    CORTEX_PAT="..." python3 tooling/cortex_wire_check.py
    CORTEX_PAT="..." python3 tooling/cortex_wire_check.py --host <account>.snowflakecomputing.com

Read-only: sends minimal completion/catalog requests without mutating account state.
Compare output against the baseline documented in docs/motivation.md and tooling/README.md.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_HOST = os.environ.get("SNOWFLAKE_HOST", "localhost")
CLAUDE = "claude-sonnet-5-5"
REASONING = "openai-gpt-6-luna"

OK, BROKEN, UNKNOWN = "RESOLVED", "STILL PRESENT", "INCONCLUSIVE"
INFO = "INFO"


def call(host, pat, path, payload=None, method=None, extra_headers=None):
    """Return (status, parsed_or_text). Never raises on HTTP 4xx/5xx."""
    url = "https://%s/api/v2/cortex/v1%s" % (host, path)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Bearer %s" % pat,
    }
    headers.update(extra_headers or {})
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(  # noqa: S310
        url, data=data, headers=headers, method=method or ("POST" if data else "GET")
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:  # noqa: S310
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


def t1_max_tokens(host, pat):
    """Deviation 1: max_tokens rejected in favor of max_completion_tokens."""
    status, parsed = chat(host, pat, model=CLAUDE, max_tokens=16)
    if status == 200:
        return OK, "max_tokens accepted", "adapt_payload() rewrite is no longer required"
    msg = parsed.get("message", parsed) if isinstance(parsed, dict) else parsed
    return BROKEN, "HTTP %s — %s" % (status, str(msg)[:110]), "keep max_completion_tokens rewrite"


def t2_models_list(host, pat):
    """Deviation 2: GET /v1/models returns 404."""
    status, parsed = call(host, pat, "/models")
    if status == 200:
        n = len(parsed.get("data", [])) if isinstance(parsed, dict) else "?"
        return (
            OK,
            "HTTP 200, %s models" % n,
            "update do_GET() to prefer upstream /models before static fallback",
        )
    return BROKEN, "HTTP %s" % status, "keep static /v1/models catalog"


def t3_finish_reason(host, pat):
    """Deviation 3: finish_reason omitted or empty."""
    results = {}

    _, parsed = chat(host, pat, model=CLAUDE, max_completion_tokens=16)
    results["complete (expected 'stop')"] = first_choice(parsed).get("finish_reason")

    tool = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "City weather lookup",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
    _, parsed = chat(
        host,
        pat,
        model=CLAUDE,
        max_completion_tokens=200,
        tools=[tool],
        messages=[{"role": "user", "content": "What is the weather in Milan? Use the tool."}],
    )
    choice = first_choice(parsed)
    results["tool call (expected 'tool_calls')"] = choice.get("finish_reason")
    has_tool_calls = bool((choice.get("message") or {}).get("tool_calls"))

    _, parsed = chat(
        host,
        pat,
        model=CLAUDE,
        max_completion_tokens=5,
        messages=[{"role": "user", "content": "Write a long essay on distributed databases."}],
    )
    results["truncated (expected 'length')"] = first_choice(parsed).get("finish_reason")

    detail = "; ".join("%s -> %r" % (k, v) for k, v in results.items())
    detail += "; tool_calls present: %s" % has_tool_calls
    values = list(results.values())

    if all(v for v in values) and values[0] == "stop":
        return OK, detail, "normalize_finish_reason() and stop_chunk() are no-ops automatically"
    if any(v for v in values):
        return UNKNOWN, detail, "partially resolved: inspect finish_reason normalization"
    return BROKEN, detail, "keep finish_reason inference in proxy/cortex_proxy.py"


def t4_tools_reasoning(host, pat):
    """Deviation 4: tools + reasoning_effort rejected on reasoning models."""
    tool = {
        "type": "function",
        "function": {
            "name": "noop",
            "description": "No-op helper",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    status, parsed = chat(host, pat, model=REASONING, max_completion_tokens=16, tools=[tool])
    if status == 200:
        return (
            OK,
            "%s accepts tools without forcing reasoning_effort=none" % REASONING,
            "run refresh_cortex_models.py --write to refresh tools_require_reasoning_effort_none",
        )
    msg = parsed.get("message", parsed) if isinstance(parsed, dict) else parsed
    if status == 400 and "unknown model" in str(msg).lower():
        return UNKNOWN, "%s unavailable on this account" % REASONING, "re-test with another model"
    return BROKEN, "HTTP %s — %s" % (status, str(msg)[:110]), "keep reasoning_effort=none fallback"


def t5_responses_api(host, pat):
    """Extra: /responses endpoint availability check."""
    status, parsed = call(host, pat, "/responses", {"model": "openai-gpt-6-astra", "input": "ping"})
    if status == 200:
        return OK, "HTTP 200", "/responses endpoint is available (use shared multi-API proxy)"
    msg = parsed.get("message", parsed) if isinstance(parsed, dict) else parsed
    return BROKEN, "HTTP %s — %s" % (status, str(msg)[:110]), "endpoint not available"


def t6_anthropic_endpoint(host, pat):
    """Extra: /messages Anthropic native endpoint check."""
    status, parsed = call(
        host,
        pat,
        "/messages",
        {
            "model": CLAUDE,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    if status != 200:
        msg = parsed.get("message", parsed) if isinstance(parsed, dict) else parsed
        return UNKNOWN, "HTTP %s — %s" % (status, str(msg)[:110]), "-"
    reason = parsed.get("stop_reason") if isinstance(parsed, dict) else None
    return (
        INFO,
        "stop_reason -> %r (max_tokens accepted)" % reason,
        "native /messages surface available via shared multi-API proxy (see docs/architecture.md)",
    )


TESTS = [
    ("1. max_tokens rejected", t1_max_tokens),
    ("2. GET /v1/models -> 404", t2_models_list),
    ("3. finish_reason omitted", t3_finish_reason),
    ("4. tools + reasoning_effort", t4_tools_reasoning),
    ("5. /responses endpoint", t5_responses_api),
    ("6. Anthropic /messages endpoint", t6_anthropic_endpoint),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--pat-file", help="path to file containing a Snowflake PAT")
    args = parser.parse_args()

    if args.pat_file:
        with open(args.pat_file) as fh:
            pat = fh.read().strip()
    else:
        pat = os.environ.get("CORTEX_PAT", "").strip()
    if not pat:
        sys.exit("Snowflake PAT required: export CORTEX_PAT or pass --pat-file")

    print("host: %s\n" % args.host)
    verdicts = {}
    for label, fn in TESTS:
        try:
            verdict, detail, action = fn(args.host, pat)
        except Exception as err:
            verdict, detail, action = UNKNOWN, "probe error: %s" % err, "-"
        verdicts[label] = verdict
        print("%-36s %s" % (label, verdict))
        print("    detail: %s" % detail)
        print("    action: %s\n" % action)

    resolved = [k for k, v in verdicts.items() if v == OK]
    print("=" * 78)
    if not resolved:
        print("No upstream wire changes detected: all embedded proxy adaptations remain active.")
    else:
        print(
            "Upstream behavior changed (%d checks): review proxy/cortex_proxy.py."
            % len(resolved)
        )
        for k in resolved:
            print("  - %s" % k)
        print("\nSee docs/motivation.md and tooling/README.md for verification steps.")


if __name__ == "__main__":
    main()
