# Configuration

## GitHub Repository Variables

| Variable | Example | Purpose |
|---|---|---|
| `SNOWFLAKE_ACCOUNT` | `org-account` | Account identifier for the CLI. |
| `SNOWFLAKE_DATABASE` | `HERMES_PLATFORM` | Deployment database. |
| `SNOWFLAKE_SCHEMA` | `CORE` | Deployment schema. |
| `SNOWFLAKE_BOOTSTRAP_USER` | `HERMES_GITHUB_BOOTSTRAP` | OIDC bootstrap service user. |
| `SNOWFLAKE_DEPLOY_USER` | `HERMES_GITHUB_DEPLOY` | OIDC production deploy service user. |
| `SNOWFLAKE_BOOTSTRAP_ROLE` | `HERMES_GITHUB_BOOTSTRAP_ROLE` | Bootstrap role. |
| `SNOWFLAKE_DEPLOY_ROLE` | `HERMES_GITHUB_DEPLOY_ROLE` | Production deploy role. |
| `SNOWFLAKE_WAREHOUSE` | `HERMES_DEPLOY_WH` | Small warehouse for deployment SQL. |
| `HERMES_COMPUTE_POOL` | `HERMES_CPU_POOL` | SPCS compute pool. |
| `HERMES_INSTANCE_FAMILY` | `GEN_X64_G2_4` | Compute pool instance family. |
| `HERMES_IMAGE_REPOSITORY` | `HERMES_IMAGES` | Hermes image repository. |
| `CORTEX_PROXY_IMAGE_REPOSITORY` | `CORTEX_PROXY_IMAGES` | Proxy image repository. |
| `HERMES_EGRESS_EAI` | `HERMES_EGRESS_EAI` | External Access Integration name. |

## Hermes Version Pin

The upstream installer clones `HEAD` and offers no revision flag (`hermes update` accepts only `--branch`, never a commit). The image therefore pins the revision explicitly:

```dockerfile
ARG HERMES_GIT_SHA=13ce0c5c675e843af70d19c9e5144249cd51c8d1
```

Without the pin the build is **not reproducible**: two builds of the same commit of this repository can ship different Hermes versions, and a rebuild done to pick up a proxy fix silently bumps the agent as well, mixing two changes into one deployment.

To move to a newer revision, do it as its own change:

```bash
docker build --build-arg HERMES_GIT_SHA=<sha> -f docker/hermes/Dockerfile .
```

The pin step also runs `uv sync --frozen`, because the installer resolves dependencies from `uv.lock` rather than `requirements.txt`. Checking out a revision without re-syncing would leave the `HEAD` virtualenv on top of pinned code — a combination nobody has tested. For the same reason the pin must stay **before** the `python-telegram-bot` layer, so the sync cannot prune it.

## Container Environment Variables

Set in the service spec. `HERMES_PROVIDER` and `HERMES_MODEL` are rendered by the deploy workflow from the repository variables of the same name; the Telegram ones below are **not** in the default template and must be added to `infrastructure/specs/hermes.service.yaml` if you need them.

| Variable | Example | Purpose |
|---|---|---|
| `HERMES_PROVIDER` | `snowflake-cortex-proxy` | Provider slug written into `config.yaml`. |
| `HERMES_MODEL` | `claude-opus-5` | Default model. `hermes_configure.py` assigns `model.default` from this; if unset it falls back to the script's default, so a config re-patch would silently change the active model. Setting it keeps the choice declarative in the spec instead of living only in block-volume state. |
| `HERMES_SELFTEST` | `1` | Runs the boot self-tests and leaves the outcome in the service logs. Set to `0` to skip. |
| `TELEGRAM_ALLOWED_USERS` | `123456789` | Optional allow-list of comma-separated **numeric** user IDs. A username is silently ignored: the adapter compares numeric IDs, so inbound messages are dropped with no log line at default verbosity. Outbound still works, which makes the bot look like it "receives but never answers". |
| `TELEGRAM_HOME_CHANNEL` | `123456789` | Chat ID for outbound and cronjob delivery. Provided as a Snowflake secret in the default template. |

## GitHub Secrets

None required for Snowflake authentication. GitHub Actions obtains a short-lived OIDC token.

## Snowflake Secrets (Created at Runtime)

These are created in the deployment schema by bootstrap or set manually. They are injected into the SPCS container as environment variables.

| Secret Name | Env Var | Content |
|---|---|---|
| `HERMES_SSH_PUBKEY` | `SSH_PUBLIC_KEY` | SSH public key for fallback access. |
| `CF_TUNNEL_TOKEN` | `CF_TUNNEL_TOKEN` | Cloudflare tunnel token. |
| `TS_AUTHKEY` | `TS_AUTHKEY` | Tailscale reusable, non-ephemeral auth key. |
| `HERMES_DASH_USERNAME` | `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` | Dashboard login username. |
| `HERMES_DASH_PASSWORD_HASH` | `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH` | Scrypt hash of the dashboard password. |
| `HERMES_DASH_SECRET` | `HERMES_DASHBOARD_BASIC_AUTH_SECRET` | Stable signing secret for session cookies. Must not change between restarts. |

## Generating the Password Hash

Run this from inside the deployed container (web terminal at port 7681) or from any machine with the Hermes venv available:

```bash
python -c "
import sys
sys.path.insert(0, '/usr/local/lib/hermes-agent')
from plugins.dashboard_auth.basic import hash_password
print(hash_password('your-password-here'))
"
```

Store the output as the `HERMES_DASH_PASSWORD_HASH` secret value.

## Tailscale Auth Key

Create a key at https://login.tailscale.com/admin/settings/keys with:
- **Reusable: yes** (the container may restart and needs to re-authenticate)
- **Ephemeral: no** (an ephemeral node is removed on disconnect, changing the tailnet IP)
- **Pre-approved: yes** if device approval is enabled on the tailnet
- **Tag: `tag:hermes-spcs`** recommended (tagged nodes have no expiring node key)

Store the key as the `TS_AUTHKEY` Snowflake Secret.
