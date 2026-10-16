# Cortex Proxy

> **Note:** For multi-protocol deployments across **Hermes, n8n, Langflow, and LangChain/LangGraph**, use the shared multi-API SPCS proxy [**`snowflake-proxy-cortexrestapi`**](https://github.com/fbattaglieri-snowflake/snowflake-proxy-cortexrestapi), which exposes all three Snowflake Cortex REST APIs (**OpenAI Chat Completions** `/chat/v1`, **OpenAI Responses** `/responses/v1`, and **Anthropic Messages** `/messages`) with dynamic model discovery (`discover_models: true`), universal `ProfileV2` client normalization, `ReplayCache` rehydration, and zero-touch model promotion via [**`cortex-model-compatibility-matrix`**](https://github.com/fbattaglieri-snowflake/cortex-model-compatibility-matrix).

`proxy/cortex_proxy.py` in this directory is the minimal, dependency-free embedded fallback HTTP server that runs inside the Hermes container on `127.0.0.1:8080`. It resolves the six deviations between Snowflake Cortex and the OpenAI Chat Completions protocol documented in [docs/motivation.md](../docs/motivation.md).

## Routes

- `POST /v1/chat/completions` — forwards to Cortex after header and body normalization.
- `GET /v1/models` — serves the `models.json` catalog.
- `POST /api/v2/statements` — forwards to the Snowflake SQL API (for agent tool calls that require SQL execution).
- `GET /healthz` — process health check.

## Authentication

The proxy reads `/snowflake/session/token` on every request and sends it as the SPCS OAuth token. The token is refreshed by SPCS and is usable only within the container.

## Model Catalog

`models.json` is the single source of truth for the proxy (`/v1/models`) and for `hermes_configure.py` (provider entries in `config.yaml`). It is loaded on demand and cached by `mtime`, so uploading a new version to the mounted Snowflake stage updates the proxy without a restart.

## Tests

```bash
pytest proxy/test_proxy.py -q
```
