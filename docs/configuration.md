# Configuration

## 1. GitHub Repository Variables

Configure these variables in your GitHub repository under **Settings $\rightarrow$ Secrets and variables $\rightarrow$ Actions $\rightarrow$ Variables**:

| Variable | Example | Purpose |
|---|---|---|
| `SNOWFLAKE_ACCOUNT` | `org-account` | Snowflake account identifier used by Snowflake CLI. |
| `SNOWFLAKE_DATABASE` | `HERMES_PLATFORM` | Target deployment database. |
| `SNOWFLAKE_SCHEMA` | `CORE` | Target deployment schema. |
| `SNOWFLAKE_BOOTSTRAP_USER` | `HERMES_GITHUB_BOOTSTRAP` | OIDC service user for `bootstrap.yml`. |
| `SNOWFLAKE_DEPLOY_USER` | `HERMES_GITHUB_DEPLOY` | OIDC service user for `deploy.yml`, `operate.yml`, and `backup.yml`. |
| `SNOWFLAKE_BOOTSTRAP_ROLE` | `HERMES_GITHUB_BOOTSTRAP_ROLE` | Role granted infrastructure creation privileges. |
| `SNOWFLAKE_DEPLOY_ROLE` | `HERMES_GITHUB_DEPLOY_ROLE` | Least-privilege role for service deployment and operation. |
| `SNOWFLAKE_WAREHOUSE` | `HERMES_DEPLOY_WH` | Warehouse used to execute deployment SQL. |
| `HERMES_COMPUTE_POOL` | `HERMES_CPU_POOL` | SPCS compute pool name. |
| `HERMES_INSTANCE_FAMILY` | `CPU_X64_M` | Compute pool instance family (check `CREATE COMPUTE POOL` docs for your cloud). |
| `HERMES_IMAGE_REPOSITORY` | `HERMES_IMAGES` | SPCS image repository name. |
| `HERMES_EGRESS_EAI` | `HERMES_EGRESS_EAI` | External Access Integration name for outbound HTTPS/DERP traffic. |
| `STANDARD_PROXY_ORIGIN` | `http://llm-proxy-standard.core.svc.spcs.internal:8080` | **Option A:** Internal SPCS DNS origin for [**`snowflake-proxy-cortexrestapi`**](https://github.com/fbattaglieri-snowflake/snowflake-proxy-cortexrestapi) (leave empty if deploying Option B only). |
| `AIGATEWAY_PROXY_ORIGIN` | `http://llm-proxy-aigateway.core.svc.spcs.internal:8080` | **Option B:** Internal SPCS DNS origin for [**`snowflake-proxy-cortex-aigateway`**](https://github.com/fbattaglieri-snowflake/snowflake-proxy-cortex-aigateway) (leave empty if deploying Option A only; set both to co-host Option A + Option B). |
| `HERMES_PROVIDER` | `snowflake-aigateway-messages` | Default provider slug (`snowflake-aigateway-{messages,responses,chat}`, `snowflake-standard-{responses,chat,messages}`, `ollama-spcs`, or `snowflake-cortex-proxy`). |
| `HERMES_MODEL` | `claude-opus-5-5` | Default model ID set in `config.yaml` (`model.default`). |
| `ENABLE_BACKUP` | `true` / `false` | Optional toggle for scheduled runs of `backup.yml`. |

> [!TIP]
> **Smart Provider Registration (`hermes_configure.py`):**
> - Set **only `AIGATEWAY_PROXY_ORIGIN`** (`HERMES_PROVIDER=snowflake-aigateway-messages`) $\rightarrow$ registers the 3 `Snowflake-AIGateway-*` providers (`api_key: "client:hermes-spcs"`) + `ollama-spcs`.
> - Set **only `STANDARD_PROXY_ORIGIN`** (`HERMES_PROVIDER=snowflake-standard-responses`) $\rightarrow$ registers the 3 `Snowflake-Standard-*` providers + `ollama-spcs`.
> - Set **both `AIGATEWAY_PROXY_ORIGIN` and `STANDARD_PROXY_ORIGIN`** $\rightarrow$ registers all 6 providers in the `/model` picker so you can switch seamlessly between Cortex AI Gateway and direct Cortex REST API.

---

## 2. Hermes Upstream Revision Pin (`HERMES_GIT_SHA`)

`docker/hermes/Dockerfile` pins an exact upstream Hermes Agent commit via `--commit`:

```dockerfile
ENV HERMES_RUNTIME_DIR=/opt/hermes/tools
ARG HERMES_GIT_SHA=4d04303818b61bf3aa09278af48356315515965b
```

- **Reproducible Builds:** Two builds of the same commit always produce the exact same Hermes Agent revision and Python environment (`/opt/hermes/installs`).
- **Outside `/root` Block Volume:** Installing into `/opt/hermes` and symlinking `/root/.hermes/installs` at boot ensures runtime code is never masked by `/root` and updates cleanly on `ALTER SERVICE`.

To upgrade Hermes Agent to a newer upstream commit:

```bash
docker build --platform linux/amd64 --build-arg HERMES_GIT_SHA=<new-commit-sha> -f docker/hermes/Dockerfile .
```

---

## 3. Container Environment Variables (`hermes.service.yaml`)

| Variable | Default / Example | Purpose |
|---|---|---|
| `HERMES_PROVIDER` | `snowflake-aigateway-messages` / `snowflake-standard-responses` | Active provider slug written into `/root/.hermes/config.yaml`. |
| `HERMES_MODEL` | `claude-opus-5-5` / `openai-gpt-6-astra` | Default model written into `model.default`. Keeping this declarative prevents cronjob spend-guard drift across restarts. |
| `STANDARD_PROXY_ORIGIN` | `http://llm-proxy-standard.core.svc.spcs.internal:8080` | Base URL of [**`snowflake-proxy-cortexrestapi`**](https://github.com/fbattaglieri-snowflake/snowflake-proxy-cortexrestapi) (Option A). |
| `AIGATEWAY_PROXY_ORIGIN` | `http://llm-proxy-aigateway.core.svc.spcs.internal:8080` | Base URL of [**`snowflake-proxy-cortex-aigateway`**](https://github.com/fbattaglieri-snowflake/snowflake-proxy-cortex-aigateway) (Option B). |
| `HERMES_SELFTEST` | `1` | Runs non-destructive boot self-tests and logs results to `SYSTEM$GET_SERVICE_LOGS`. Set to `0` to skip. |
| `TELEGRAM_ALLOWED_USERS` | `123456789` | Optional comma-separated **numeric** Telegram user IDs (do not use `@username`). |

---

## 4. Snowflake Secrets (Created by `10_bootstrap.sql`)

GitHub Actions uses **passwordless OIDC Workload Identity Federation** — no long-lived Snowflake credentials are stored in GitHub. Runtime secrets live as **Snowflake Secrets** in your deployment schema and are injected by `infrastructure/specs/hermes.service.yaml`:

| Snowflake Secret Name | Injected Container Env Var | Required? | Purpose |
|---|---|---|---|
| `HERMES_SSH_PUBKEY` | `SSH_PUBLIC_KEY` | Recommended | SSH public key written to `/root/.ssh/authorized_keys`. |
| `HERMES_DASH_USERNAME` | `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` | Required for Desktop | Username for Hermes Desktop Remote Gateway (`:9119`). |
| `HERMES_DASH_PASSWORD_HASH` | `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH` | Required for Desktop | Scrypt hash of the dashboard password. |
| `HERMES_DASH_SECRET` | `HERMES_DASHBOARD_BASIC_AUTH_SECRET` | Required for Desktop | Stable cookie signing secret (must stay constant across restarts). |
| `TS_AUTHKEY` | `TS_AUTHKEY` | Required for Desktop | Tailscale **reusable, non-ephemeral** auth key (`tskey-auth-...`). |
| `TELEGRAM_BOT_TOKEN` | `TELEGRAM_BOT_TOKEN` | Optional | Enables `hermes gateway run` (Telegram bot + cron ticker). |
| `TELEGRAM_HOME_CHANNEL` | `TELEGRAM_HOME_CHANNEL` | Optional | Default Telegram `chat_id` for outbound notifications and cron delivery. |
| `CF_TUNNEL_TOKEN` | `CF_TUNNEL_TOKEN` | Optional | Cloudflare tunnel token (`--protocol http2`). |

### Generating `HERMES_DASH_PASSWORD_HASH`

```bash
/usr/local/lib/hermes-agent/venv/bin/python -c "
import sys
sys.path.insert(0, '/usr/local/lib/hermes-agent')
from plugins.dashboard_auth.basic import hash_password
print(hash_password('your-strong-password'))
"
```

### Creating the Tailscale Auth Key (`TS_AUTHKEY`)

Generate a key at `https://login.tailscale.com/admin/settings/keys` with **Reusable: Yes**, **Ephemeral: No**, and **Tag: `tag:hermes-spcs`** (recommended so node keys do not expire). `start.sh` uses `TS_AUTHKEY` only on first boot before `/root/tailscale/tailscaled.state` exists.
