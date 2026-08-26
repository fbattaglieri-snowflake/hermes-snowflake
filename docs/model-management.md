# Model Management

## The Four-Gate Model

Adding a new Cortex model to the Hermes configuration requires passing four gates in sequence:

1. The model must be actually served by the Snowflake account (not just listed in the catalog).
2. It must be in `proxy/models.json`.
3. The proxy image must contain the updated `models.json` (or the stage mount must deliver it at runtime).
4. `hermes_configure.py` must have been re-run with the updated file to write the provider entry into `config.yaml`.

Gates 2 and 3 can be decoupled: if the proxy image mounts the stage that holds `models.json`, updating the file and waiting for the proxy to reload it (on the next request, it checks `mtime`) is sufficient for gates 2 and 3 without a rebuild.

## Verifying a New Model: `cortex_model_gate.py`

The gate script tests a candidate model against the full proxy+upstream stack — not just the raw gateway. It imports `cortex_proxy.py` and applies the same transformations the running proxy would apply.

```bash
CORTEX_PAT="<your-pat>" python3 tooling/cortex_model_gate.py --new
```

The `--new` flag tests only models not yet in `models.json`. `--all` runs a regression test on every known model. `--models <name>` tests a specific model.

**IMPORTANT**: the `CORTEX_PAT=` assignment must be the first token of the command. Using `cd dir && CORTEX_PAT="..." command` causes the secret injection to fail (the PAT is not resolved) and produces HTTP 401 on every model, falsely appearing as a Snowflake regression.

### Verdicts

| Verdict | Meaning |
|---|---|
| `COMPATIBLE` | Tool calling works through the proxy. Safe to promote to configuration. |
| `WITH RESERVATION` | Responds but does not support tool calling. Suitable for text generation (n8n Basic LLM Chain) but not for Hermes agent mode. |
| `INCOMPATIBLE` | Does not respond from this account. Do not add to configuration. |

Exit code 1 indicates a regression: a model already in `models.json` no longer passes its tests.

## Refreshing the Catalog: `refresh_cortex_models.py`

```bash
CORTEX_PAT="..." python3 tooling/refresh_cortex_models.py           # dry run
CORTEX_PAT="..." python3 tooling/refresh_cortex_models.py --write   # update models.json
CORTEX_PAT="..." python3 tooling/refresh_cortex_models.py --write --upload  # write + upload to stage
```

The script reads the Snowflake model catalog, tests each entry with a real API call, detects tool-calling constraints, and rewrites `models.json`. It does not remove models that fail transiently: a model that gives a transient error on one run would be permanently dropped from the configuration, which is worse than leaving it in and catching the error at runtime.

## Known Catalog Traps

- **`openai-gpt-5.6-luna/sol/terra`**: the catalog lists them as `OPENAI-1P-GPT-5.6-*` (`1p` = first party). The invocable name has no `1p-` segment. With `1p-` you get `unknown model`.
- **Reasoning models with low token budget**: `openai-gpt-5`, `-mini`, and `-nano` return HTTP 200 with empty content and `finish_reason: length` when the token budget is below ~512. They are not broken; they consumed the budget in reasoning before emitting text. Use at least 1024 tokens in probes.
- **`deepseek-v4-flash` and similar**: appear in the catalog as pre-registrations with `lifecycle_status: NULL`. They return `unknown model` on the REST gateway and `is unavailable` via SQL. The catalog listing is not evidence of availability.
- **Legacy models**: `openai-gpt-4.1` reports `legacy state` from the gateway despite a future EOL date. Do not add new legacy-state models to configuration.
