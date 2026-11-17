# Embedded Fallback Proxy (`proxy/cortex_proxy.py`)

> **Recommended Production Topologies (Option A & Option B):** For full 3-protocol support (**Anthropic Messages** `/messages`, **OpenAI Responses** `/responses/v1` with `1.05M` context, and **OpenAI Chat Completions** `/chat/v1`), dynamic model discovery (`discover_models: true`), universal `ProfileV2` normalization, and `ReplayCache` multi-turn rehydration, deploy **either**:
> - **Option A — Cortex REST API Proxy:** [**`snowflake-proxy-cortexrestapi`**](https://github.com/fbattaglieri-snowflake/snowflake-proxy-cortexrestapi) (`STANDARD_PROXY_ORIGIN`)
> - **Option B — Cortex AI Gateway Proxy:** [**`snowflake-proxy-cortex-aigateway`**](https://github.com/fbattaglieri-snowflake/snowflake-proxy-cortex-aigateway) (`AIGATEWAY_PROXY_ORIGIN`, adding OpenTelemetry traces in `TABLE(AGENT_TRACE_TABLE('SNOWFLAKE'))`, `AI_GATEWAY_USAGE_HISTORY` attribution, and `SNOWFLAKE.CORE.QUOTA` enforcement)
> - **Qualification Harness:** [**`cortex-model-compatibility-matrix`**](https://github.com/fbattaglieri-snowflake/cortex-model-compatibility-matrix)

`proxy/cortex_proxy.py` in this directory is **Option C**: a minimal, dependency-free embedded fallback HTTP server that runs inside the Hermes container on `127.0.0.1:8080` for single-container Chat Completions fallback. It resolves the six deviations between raw Snowflake Cortex Chat Completions and standard OpenAI SDKs documented in [docs/motivation.md](../docs/motivation.md), plus bounded exponential backoff (`3s, 7s, 15s, 30s, 45s`) and `Retry-After` parsing on transient `HTTP 429`/`503`/`529` responses.

## Routes

- `POST /v1/chat/completions` — forwards to Cortex after header and body normalization + `429`/`503`/`529` retry.
- `GET /v1/models` — serves the `models.json` catalog.
- `POST /api/v2/statements` — forwards to the Snowflake SQL API (for agent tool calls that require SQL execution).
- `GET /healthz` — process health check.

## Authentication

Reads `/snowflake/session/token` on every request and sends it as the SPCS OAuth token (`X-Snowflake-Authorization-Token-Type: OAUTH`).

## Tests

```bash
pytest proxy -q
```
