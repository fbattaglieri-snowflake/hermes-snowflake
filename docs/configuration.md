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
| `STANDARD_PROXY_ORIGIN` | `http://llm-proxy-standard.core.svc.spcs.internal:8080` | Internal SPCS DNS origin for [**`snowflake-proxy-cortexrestapi`**](https://github.com/fbattaglieri-snowflake/snowflake-proxy-cortexrestapi). |
| `HERMES_PROVIDER` | `snowflake-standard-responses` | Default provider slug (`snowflake-standard-responses`, `snowflake-standard-chat`, `snowflake-standard-messages`, `ollama-spcs`, or `snowflake-cortex-proxy`). |
| `HERMES_MODEL` | `openai-gpt-6-astra` | Default model ID set in `config.yaml` (`model.default`). |
| `ENABLE_BACKUP` | `true` / `false` | Optional toggle for scheduled runs of `backup.yml`. |

---

## 2. Hermes Upstream Revision Pin (`HERMES_GIT_SHA`)

The upstream Hermes installer supports pinning an exact commit via `--commit`. `docker/hermes/Dockerfile` pins this revision explicitly:

```dockerfile
ENV HERMES_RUNTIME_DIR=/opt/hermes/tools
ARG HERMES_GIT_SHA=4d04303818b61bf3aa09278af48356315515965b
```

Why this matters:
- **Reproducible Builds:** Two builds of the same commit in this repository always produce the exact same Hermes Agent revision and Python environment (`/opt/hermes/installs`).
- **Outside `/root` Block Volume:** By installing into `/opt/hermes/hermes-agent` and `/opt/hermes/installs` and symlinking `/root/.hermes/installs` at boot, the runtime code is never masked by the `/root` block volume and automatically updates whenever you deploy a new image.

To upgrade Hermes Agent to a newer upstream commit:

```bash
docker build --platform linux/amd64 --build-arg HERMES_GIT_SHA=<new-commit-sha> -f docker/hermes/Dockerfile .
```

---

## 3. Container Environment Variables (`hermes.service.yaml`)

| Variable | Default / Example | Purpose |
|---|---|---|
| `HERMES_PROVIDER` | `snowflake-standard-responses` | Active provider slug written into `/root/.hermes/config.yaml`. |
| `HERMES_MODEL` | `openai-gpt-6-astra` | Default model written into `model.default`. Keeping this declarative prevents cronjob inference-config drift across restarts. |
| `STANDARD_PROXY_ORIGIN` | `http://llm-proxy-standard.core.svc.spcs.internal:8080` | Base URL of the shared [**`snowflake-proxy-cortexrestapi`**](https://github.com/fbattaglieri-snowflake/snowflake-proxy-cortexrestapi) service. |
| `HERMES_SELFTEST` | `1` | Runs non-destructive boot self-tests and logs results to `SYSTEM$GET_SERVICE_LOGS`. Set to `0` to skip. |
| `TELEGRAM_ALLOWED_USERS` | `123456789` | Optional comma-separated **numeric** Telegram user IDs (do not use `@username`). |

---

## 4. Snowflake Secrets (Created by `10_bootstrap.sql`)

GitHub Actions uses **passwordless OIDC Workload Identity Federation** — no long-lived Snowflake credentials are stored in GitHub. Runtime secrets are stored as **Snowflake Secrets** in your deployment schema and injected into the container by `infrastructure/specs/hermes.service.yaml`:

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

Run inside the container (via the web terminal on `:7681`) or any environment with the Hermes virtualenv:

```bash
/usr/local/lib/hermes-agent/venv/bin/python -c "
import sys
sys.path.insert(0, '/usr/local/lib/hermes-agent')
from plugins.dashboard_auth.basic import hash_password
print(hash_password('your-strong-password'))
"
```

### Creating the Tailscale Auth Key (`TS_AUTHKEY`)

Generate a key at `https://login.tailscale.com/admin/settings/keys` with:
- **Reusable:** `Yes`
- **Ephemeral:** `No` (ephemeral nodes are deleted on disconnect and lose their IP)
- **Tag:** `tag:hermes-spcs` (recommended so node keys do not expire)

`start.sh` uses `TS_AUTHKEY` **only on first boot** when `/root/tailscale/tailscaled.state` does not exist yet. On all subsequent boots, `tailscaled` re-authenticates from the persisted state file on the block volume and keeps the **exact same `100.x.y.z` tailnet IP**.
