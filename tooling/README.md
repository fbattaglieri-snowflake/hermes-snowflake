# Tooling: Model Management

## `cortex_model_gate.py` — Verify a New Model

Tests candidate requests using the local proxy transformations and the upstream
endpoint. The current script emits `COMPATIBILE`, `CON RISERVA`, or `INCOMPATIBILE`.
This is not a test of the deployed service or the full streaming adapter.

```bash
# Test only models not yet in models.json
CORTEX_PAT="<your-pat>" python3 tooling/cortex_model_gate.py --new

# Regression test all known models
CORTEX_PAT="<your-pat>" python3 tooling/cortex_model_gate.py --all

# Test a specific model
CORTEX_PAT="<your-pat>" python3 tooling/cortex_model_gate.py --models deepseek-v4-flash
```

**Critical**: the `CORTEX_PAT=` assignment must be the first token of the command. Using `cd dir && CORTEX_PAT="..." command` leaves the PAT unresolved and produces HTTP 401 on every model, which looks like a global Snowflake outage.

Exit code 1 if a model already in `models.json` regresses (was working, now does not).

## `refresh_cortex_models.py` — Refresh the Model Catalog

```bash
# Dry run — prints a report without writing anything
CORTEX_PAT="<your-pat>" python3 tooling/refresh_cortex_models.py

# Update proxy/models.json
CORTEX_PAT="<your-pat>" python3 tooling/refresh_cortex_models.py --write

# Update and upload to the Snowflake stage (zero-touch proxy update)
CORTEX_PAT="<your-pat>" python3 tooling/refresh_cortex_models.py --write --upload
```

The proxy reads `/models/cortex_models.json` from the mounted stage and reloads it when the `mtime` changes. Uploading a new file updates the proxy without rebuilding the image or restarting the service.

## `cortex_wire_check.py` — Wire-Level Protocol Compatibility

Tests the raw wire protocol between the proxy and the upstream Cortex gateway. Useful when debugging a new Cortex release or a suspected gateway regression.

## Interpreting Verdicts

| Verdict | Meaning |
|---|---|
| `COMPATIBILE` | All gate checks passed, including tool round-trip, parallel history and a declared context limit. Still verify the deployed client before use. |
| `CON RISERVA` | At least one required check failed, is missing or remains inconclusive. Do not promote for agent use. |
| `INCOMPATIBILE` | The text probe did not produce usable output. Diagnose authentication, budget and transient failures before declaring a model unavailable. |

## Model Promotion Checklist

1. Run `cortex_model_gate.py --models <name>` and inspect every check, not just HTTP 200. A new model's context limit remains unverified until you review it; T7 blocks promotion in the meantime.
2. Add the model to `proxy/models.json` under `"models"`.
3. If the model requires `reasoning_effort: none` when tools are present, add it to `tools_require_reasoning_effort_none`.
4. If the model does not support tool calling, add it to `tools_unsupported`.
5. Upload the updated `models.json` to the stage **or** rebuild and redeploy the image.
6. Verify via `GET /v1/models` through the proxy that the new entry appears.

## Safe catalogue changes

SQL inference, native REST, OpenAI-compatible REST and the deployed proxy are
separate compatibility surfaces. Catalogue presence or SQL success does not
establish REST availability. Never add a SQL-only model to the agent catalogue.

The refresh command rebuilds the catalogue from one probe run. Do not use
`--write --upload` unattended: transient failures can remove working entries.
Review an additive diff against a private backup, retain existing defaults and
cron model pins, and verify rollback before changing a live configuration.

Text streaming success does not prove streaming tool-call reconstruction. Check
tool IDs, names, JSON arguments, final response and termination through the actual
client. A passing reasoning-parameter probe cannot override a failed round-trip.
In particular, the parallel-history transformation mutates a request and returns
a boolean; the retry must send the mutated request, never that boolean.

Updating the proxy's mounted catalogue does not necessarily update Hermes's
provider list. Inspect the deployed catalogue path and configuration marker.
Do not force the full configurator merely to add models: it also sets the default
provider/model and replaces managed provider sections. Preserve custom settings,
memory, sessions and scheduling state; test persistence and restoration separately.
