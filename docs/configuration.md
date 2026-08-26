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
