# Cortex Proxy

A minimal, dependency-free Python HTTP server that runs inside the Hermes container on `127.0.0.1:8080`. It resolves the six deviations between Snowflake Cortex and the OpenAI Chat Completions protocol documented in [docs/motivation.md](../docs/motivation.md).

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
