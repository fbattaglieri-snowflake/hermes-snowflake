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
DEFAULT_STAGE = "@hermes_platform.core.hermes_config"
DEFAULT_CONTEXT = 128000

REASONING_TOOLS_ERROR = "function tools with reasoning_effort"

# Family order in the rewritten file: keeping it stable makes diffs readable.
FAMILY_ORDER = ["claude-opus", "claude-sonnet", "claude-haiku", "claude-4",
                "openai-gpt", "mistral", "llama", "snowflake"]

# A trivial but valid tool: it only serves to trigger (or not) the
# tools + reasoning_effort constraint. We do not care about the model's answer.
PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Return the current time.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def die(msg):
    sys.stderr.write("error: %s\n" % msg)
    sys.exit(1)


def read_pat(args):
    if args.pat_file:
        with open(args.pat_file) as fh:
            return fh.read().strip()
    pat = os.environ.get("CORTEX_PAT", "").strip()
    if not pat:
        die("the PAT is required: export CORTEX_PAT or pass --pat-file")
    return pat


def catalog_lifecycle(connection):
    """{name: {status, eol}} from the catalog, plus the plausible name variants.

    The catalog uses uppercase and for first-party models inserts a
    '1p-' segment that is NOT part of the invocable name: OPENAI-1P-GPT-5.6-LUNA is called
    openai-gpt-5.6-luna on the gateway. We register both forms and let
    the real call decide which of the two responds.

    lifecycle_status/eol_date serve to flag LEGACY models: they respond today
    but disappear on a known date, and putting them in config without warning means
    ending up with a broken provider on the day of the EOL.
    """
    out = subprocess.run(
        ["snow", "sql", "-c", connection, "--format", "json",
         "-q", "SHOW CORTEX BASE MODELS IN SCHEMA SNOWFLAKE.MODELS"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        die("SHOW CORTEX BASE MODELS failed:\n%s" % (out.stderr or out.stdout))

    try:
        rows = json.loads(out.stdout)
    except json.JSONDecodeError:
        die("snow sql output could not be parsed as JSON:\n%s" % out.stdout[:500])
    if rows and isinstance(rows[0], list):   # snow sql nests per statement
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
    """(status, body_text). Does not raise on 4xx/5xx: we need the status."""
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
    except Exception as exc:                              # network, DNS, timeout
        return 0, "local exception: %s" % exc


def short_reason(status, body):
    """Reduces the error body to a single line to put in _unavailable."""
    text = body.strip()
    try:
        parsed = json.loads(text)
        text = str(parsed.get("message") or parsed.get("error") or text)
    except json.JSONDecodeError:
        pass
    text = " ".join(text.split())
    if status == 403:
        return "HTTP 403 - account not authorized"
    if "unknown model" in text.lower():
        return "unknown model"
    if "unavailable" in text.lower():
        return "unavailable"
    return "HTTP %s: %s" % (status, text[:160])


def probe(host, pat, model):
    """Returns a dict with the model's outcome.

    Two calls: the first establishes whether the model responds, the second (only if the
    first succeeded) whether tool calling requires reasoning_effort="none".
    Note: max_completion_tokens is used, not max_tokens — the gateway rejects
    max_tokens for every family, and that is exactly what the proxy rewrites.
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
        # Retry forcing "none": if it passes, the model goes in the constrained list.
        status, _ = call_gateway(host, pat, dict(with_tools, reasoning_effort="none"))
        result["needs_reasoning_none"] = status == 200
        if status != 200:
            result["tools_note"] = "tools rejected even with reasoning_effort=none"
        return result

    # Tools rejected for other reasons: the model remains usable for text.
    result["tools_note"] = short_reason(status, body)
    return result


def family_key(name):
    for index, prefix in enumerate(FAMILY_ORDER):
        if name.startswith(prefix):
            return (index, name)
    return (len(FAMILY_ORDER), name)


# The comment keys used to be Italian ("_commento", "_non_disponibili"). A copy
# already uploaded to the stage still carries the old names, so a run pointed at
# it via CORTEX_MODELS_PATH would fail on a missing key. Accept both on read; the
# file is always rewritten with the current names.
LEGACY_KEYS = {"_commento": "_comment", "_non_disponibili": "_unavailable"}


def normalize_legacy_keys(doc):
    for old, new in LEGACY_KEYS.items():
        if old in doc and new not in doc:
            doc[new] = doc.pop(old)
    unavailable = doc.get("_unavailable")
    if isinstance(unavailable, dict) and "_commento" in unavailable:
        unavailable.setdefault("_comment", unavailable.pop("_commento"))


def render(doc):
    """Serializes by hand to preserve the grouping by family.

    json.dump would flatten everything into a single block: with 25+ models the file
    becomes unreadable and the diffs useless. The '_'-prefixed keys are comments.
    """
    def dumps(value, indent):
        text = json.dumps(value, indent=2, ensure_ascii=False)
        pad = " " * indent
        return text.replace("\n", "\n" + pad)

    lines = ["{"]
    lines.append('  "_comment": %s,' % dumps(doc["_comment"], 2))
    lines.append("")
    lines.append('  "models": {')

    entries, previous = [], None
    for name in sorted(doc["models"], key=family_key):
        family = family_key(name)[0]
        if previous is not None and family != previous:
            entries.append("")                         # blank line between families
        entries.append('    "%s": %d,' % (name, doc["models"][name]))
        previous = family
    if entries:
        entries[-1] = entries[-1].rstrip(",")          # no comma on the last one
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
    lines.append('  "_unavailable": %s' % dumps(doc["_unavailable"], 2))
    lines.append("}")
    return "\n".join(lines) + "\n"


def upload(connection, stage):
    out = subprocess.run(
        ["snow", "stage", "copy", MODELS_FILE, stage, "-c", connection, "--overwrite"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        die("upload to the stage failed:\n%s" % (out.stderr or out.stdout))
    print("uploaded to %s" % stage)
    print("the service reloads the file within ~5 min (metadataCache); to apply immediately:")
    print("  ALTER SERVICE <DATABASE>.<SCHEMA>.HERMES_SERVICE SUSPEND;")
    print("  ALTER SERVICE <DATABASE>.<SCHEMA>.HERMES_SERVICE RESUME;")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--connection", default=DEFAULT_CONNECTION)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--stage", default=DEFAULT_STAGE)
    parser.add_argument("--pat-file", help="file containing the PAT")
    parser.add_argument("--write", action="store_true", help="rewrites cortex_models.json")
    parser.add_argument("--upload", action="store_true",
                        help="reloads to the stage (implies --write)")
    parser.add_argument("--jobs", type=int, default=4, help="calls in parallel")
    args = parser.parse_args()
    if args.upload:
        args.write = True

    pat = read_pat(args)
    with open(MODELS_FILE) as fh:
        doc = json.load(fh)
    normalize_legacy_keys(doc)
    known = dict(doc["models"])

    lifecycle = catalog_lifecycle(args.connection)
    candidates = set(lifecycle)
    # The names already in config must be retried anyway: one may have gone EOL, and
    # an invocable alias may not appear in the catalog at all.
    candidates.update(known)
    candidates = sorted(candidates)
    print("candidates to test: %d\n" % len(candidates))

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
            legacy.append((name, meta.get("eol") or "date not declared"))

    legacy_names = dict(legacy)
    print("WORKING: %d" % len(models))
    for name in sorted(models, key=family_key):
        flags = ""
        if name in constrained:
            flags += "  [tools only with reasoning_effort=none]"
        if name in no_tools:
            flags += "  [no tool calling]"
        if name in legacy_names:
            flags += "  [LEGACY, EOL %s]" % legacy_names[name]
        if name in added:
            flags += "  <-- NEW, context to be verified by hand"
        print("  %-28s %8d%s" % (name, models[name], flags))

    if removed:
        print("\nNO LONGER AVAILABLE (they were in config):")
        for name, reason in removed:
            print("  %-28s %s" % (name, reason))
    if notes:
        print("\nNOTES ON TOOL CALLING:")
        for name, note in notes:
            print("  %-28s %s" % (name, note))
    if legacy:
        print("\nLEGACY: they respond now but have a death date.")
        print("If any workflow or agent uses them, it will break at EOL.")
    if added:
        print("\nWARNING: %d new models have context=%d (conservative)."
              % (len(added), DEFAULT_CONTEXT))
        print("Correct it by hand from aisql-regional-availability before considering it final.")

    if not models:
        die("no model responded: expired PAT or wrong host? file left untouched")

    if not args.write:
        print("\ndry-run: cortex_models.json not modified (use --write)")
        return

    # _unavailable: the historical entries are merged with the ones detected now, so
    # the hand-written annotations are not lost (e.g. the names with '1p-').
    merged = dict(doc.get("_unavailable", {}))
    merged.update(unavailable)
    merged["_comment"] = doc.get("_unavailable", {}).get(
        "_comment", "Models tested and NOT working (unknown or unavailable). "
        "Do not add them back without re-testing.")

    doc["models"] = models
    doc["tools_require_reasoning_effort_none"] = constrained
    # Not used by the proxy. Informs the caller that these models cannot be used
    # with tool calling; agents or orchestrators that depend on tools must exclude them.
    doc["tools_unsupported"] = sorted(no_tools)
    doc["_legacy"] = dict(sorted(legacy)) or {}
    doc["_unavailable"] = merged

    backup = MODELS_FILE + ".bak"
    os.replace(MODELS_FILE, backup)
    with open(MODELS_FILE, "w") as fh:
        fh.write(render(doc))
    json.load(open(MODELS_FILE))                          # do not ship broken JSON
    print("\nwrote %s (backup in %s)" % (MODELS_FILE, backup))

    if args.upload:
        upload(args.connection, args.stage)


if __name__ == "__main__":
    main()
