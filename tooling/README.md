# Tooling: Model Management

## `cortex_model_gate.py` — Verify a New Model

Tests a candidate model against the full proxy+upstream stack and assigns one of three verdicts: `COMPATIBLE`, `WITH RESERVATION`, or `INCOMPATIBLE`.

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
| `COMPATIBLE` | Tool calling works through the proxy. Safe to promote to `models.json`. |
| `WITH RESERVATION` | Responds but does not support tool calling. Use only for text generation, not for Hermes agent mode. |
| `INCOMPATIBLE` | Does not respond from this account. Do not add to `models.json`. |

## Model Promotion Checklist

1. Run `cortex_model_gate.py --models <name>` and confirm `COMPATIBLE`.
2. Add the model to `proxy/models.json` under `"models"`.
3. If the model requires `reasoning_effort: none` when tools are present, add it to `tools_require_reasoning_effort_none`.
4. If the model does not support tool calling, add it to `tools_unsupported`.
5. Upload the updated `models.json` to the stage **or** rebuild and redeploy the image.
6. Verify via `GET /v1/models` through the proxy that the new entry appears.
