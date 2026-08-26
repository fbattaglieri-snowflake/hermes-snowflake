# Troubleshooting

This file documents every known gotcha encountered during deployment and operation. Each entry is identified by the resolution number from the project log.

## Container and SPCS

### `hermes serve` starts but never listens (R-03)
`hermes serve` without `--skip-build` tries to build the web UI. `npm` is present in the image but the web UI source is absent. The process stays alive but never opens a port, and `serve.log` is empty. Always use `--skip-build`.

### UDP and TUN networking unavailable (R-01)
SPCS containers have no `/dev/net/tun`, `NET_ADMIN` is not granted, and the `tun` kernel module is not loadable. Tailscale must run with `--tun=userspace-networking`. QUIC/UDP egress is blocked; DERP relay over TCP/443 works correctly.

### DERP performance (R-02)
The DERP relay closest to your SPCS region provides acceptable latency for interactive use. The earlier concern about relay latency does not apply in practice.

### Block volume masks image content
`/root` is a 20 GiB block volume mounted at runtime. Everything the installer writes under `/root` at build time is hidden. The image keeps a copy in `/opt/hermes-seed` and restores it with `cp -a -n` (no-clobber) at every boot.

## Authentication and Client Connection

### `/auth/password-login` returns 422 (R-06)
The documented payload `{"username","password"}` is missing the `provider` field. Use `{"provider":"basic","username":"...","password":"..."}`.

### Dashboard asks for a session token instead of Sign in
The `basic` provider is not configured. Check that `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` and `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH` are present in `/root/.hermes/.env` and that the process loaded them at startup.

### Client disconnects after every container restart
`HERMES_DASHBOARD_BASIC_AUTH_SECRET` is absent or regenerated at boot. Set a stable value once and keep it in the Snowflake Secret.

### WebSocket close 4401
The WS ticket did not authenticate. The session has expired, or the secret has changed. Sign in again.

### WebSocket close 4403 (R-07)
The request guard rejected the connection. This is not expected with `--host 0.0.0.0`; if it appears, check that the bind address has not regressed to loopback.

### `hermes serve --status` reports no active process (R-08)
`hermes serve --status` is unreliable in v0.20.2 and later. Use `curl http://127.0.0.1:9119/api/status` as the health check. Watchdog scripts must poll this endpoint.

## Cortex Proxy

### `Context length exceeded (N tokens)` (R-00, root cause)
This is the symptom of sending `max_tokens` to Cortex. The proxy renames it to `max_completion_tokens`. If you see this with the proxy running, verify the proxy is actually receiving the request (check `/tmp/cortex_proxy.log`).

### `Response truncated — stream ended before completion`
`finish_reason` is absent in the final SSE chunk. The proxy injects a synthetic terminal chunk. If this persists, verify the proxy version matches this repository.

### `Function tools with reasoning_effort are not supported`
The model is in `tools_require_reasoning_effort_none`. The proxy forces `reasoning_effort="none"` for these models when `tools` is present. If this appears with the proxy active, the model may not be in that list yet — run `cortex_model_gate.py` to verify.

### Telegram bot responds only `The model provider failed after retries` (R-19)
A previous turn contained parallel tool calls. Cortex rejected the assistant turn as invalid. The session history is permanently corrupted. Do not retry. Open a new session. The proxy's `collapse_parallel_tool_calls` function prevents new occurrences.

### HTTP 401 on all 27 models simultaneously (R-21, methodology trap)
This is not a Snowflake regression. It means the PAT was not injected: the command placed `CORTEX_PAT="..."` after a `&&` instead of at the start. Place the assignment first.

### Empty response with `finish_reason: length` on reasoning models (R-21, methodology trap)
`openai-gpt-5`, `-mini`, and `-nano` consume their token budget in reasoning before emitting text. Use at least 1024 tokens in probes and treat `finish_reason: length` with empty content as insufficient budget, not a model defect.

## Model Management

### Model appears in `SHOW CORTEX BASE MODELS` but returns `unknown model` on REST
Pre-registered models have `lifecycle_status: NULL`. They are not served. Do not add them to configuration.

### `openai-1p-gpt-5.6-*` returns `unknown model`
The catalog name includes `1p-` (first-party marker); the invocable name does not. Use `openai-gpt-5.6-luna/sol/terra`.

## Methodology

### WS ticket is single-use with a 30-second TTL (R-00, test trap)
A test that reuses a ticket for a second attempt receives 403 and appears as a guard rejection. Use a fresh ticket for every WebSocket attempt.

### Jinja templating in `snow sql -f`
`snow sql -f script.sh` applies Jinja templating. Shell syntax like `${#VAR}` triggers a Jinja comment error. Avoid `{#`, `{{`, `{%` in scripts passed via `-f`.

### `SYSTEM$GET_SERVICE_LOGS` tail limit
The `tail` parameter has an undocumented maximum of 1000. Values above that fail with `Invalid tail`.

### Log lines from `readinessProbe` swamp startup messages
The `ttyd` probe fires every 5 seconds. With a 400-line tail, startup messages older than ~30 minutes are out of range.
