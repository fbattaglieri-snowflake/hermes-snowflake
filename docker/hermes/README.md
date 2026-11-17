# Hermes Container Image (`docker/hermes`)

Built on Ubuntu 24.04 (`linux/amd64`), this image installs a pinned revision of [Hermes Agent](https://hermes-agent.nousresearch.com) via the official upstream installer during `docker build`. Upstream Hermes Agent source code is not redistributed in this repository.

## What the Image Adds

| Path in Image | Purpose |
|---|---|
| `/opt/hermes/hermes-agent` *(symlinked at `/usr/local/lib/hermes-agent`)* | Pinned Hermes Agent checkout (`ARG HERMES_GIT_SHA`), installed outside `/root` so it is never masked by the SPCS block volume. |
| `/opt/hermes/installs` & `/opt/hermes/tools` | Committed Python virtualenv (including `python-telegram-bot`) and managed runtime tools, linked into `/root/.hermes/installs` at boot. |
| `/opt/hermes-seed` | Clean build-time seed of `config.yaml`, `.env`, `SOUL.md`, and 58+ bundled skills. Restored to `/root/.hermes` with `cp -a -n` (`no-clobber`) on every boot. |
| `/opt/hermes_configure.py` | Idempotent `ruamel.yaml` patcher registering **Option A** ([`snowflake-proxy-cortexrestapi`](https://github.com/fbattaglieri-snowflake/snowflake-proxy-cortexrestapi)), **Option B** ([`snowflake-proxy-cortex-aigateway`](https://github.com/fbattaglieri-snowflake/snowflake-proxy-cortex-aigateway) with `client:hermes-spcs` OTel attribution), `ollama-spcs`, and **Option C** local fallback. |
| `/opt/migrate_soul.py` | Atomic, backup-safe migrator maintaining the headless Telegram CLI instructions block in `/root/.hermes/SOUL.md`. |
| `/opt/cortex_proxy.py` & `/opt/cortex_models.json` | Zero-dependency embedded loopback fallback proxy (`127.0.0.1:8080`) and verified fallback model catalog. |
| `/opt/spcs_token.sh` | Emits the rotating `/snowflake/session/token` as JSON with `"expires_in": 120`. |
| `/start.sh` | Boot entrypoint that seeds `/root/.hermes`, configures providers, runs boot self-tests, and supervises `cortex_proxy.py`, `hermes gateway run` (via `gateway.pid` identity check), `tailscaled`, `hermes serve`, and `ttyd`. |

## Build Locally

```bash
docker build --platform linux/amd64 \
  -f docker/hermes/Dockerfile \
  -t hermes-snowflake:latest \
  .
```

## Key Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `STANDARD_PROXY_ORIGIN` | — | **Option A:** Internal SPCS base URL of [**`snowflake-proxy-cortexrestapi`**](https://github.com/fbattaglieri-snowflake/snowflake-proxy-cortexrestapi). |
| `AIGATEWAY_PROXY_ORIGIN` | — | **Option B:** Internal SPCS base URL of [**`snowflake-proxy-cortex-aigateway`**](https://github.com/fbattaglieri-snowflake/snowflake-proxy-cortex-aigateway). |
| `HERMES_PROVIDER` | `snowflake-standard-responses` | Active default provider (`snowflake-aigateway-{messages,responses,chat}`, `snowflake-standard-{responses,chat,messages}`, `ollama-spcs`, or `snowflake-cortex-proxy`). |
| `HERMES_MODEL` | `openai-gpt-6-astra` | Default model ID written into `config.yaml`. |
| `SNOWFLAKE_HOST` | Injected by SPCS | Account Cortex endpoint used by embedded fallback proxy, SQL API skills, and self-tests. |
| `OLLAMA_INTERNAL_URL` | `http://ollama-service:11434` | Internal URL for the optional GPU Ollama SPCS provider. |
| `HERMES_SELFTEST` | `1` | Set to `0` to skip boot self-tests. |
| `TS_AUTHKEY` | — | Tailscale reusable, non-ephemeral auth key (`tskey-auth-...`). |
| `HERMES_DASHBOARD_BASIC_AUTH_*` | — | Dashboard username, scrypt password hash, and stable cookie signing secret. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_HOME_CHANNEL` | — | Optional Telegram bot token and default chat ID. |
| `SSH_PUBLIC_KEY` / `CF_TUNNEL_TOKEN` | — | Optional SSH authorized key and Cloudflare tunnel token. |
