# Architecture

## Container Components

Each component has a defined role, and the container startup order in `start.sh` reflects their dependencies. Nothing is removed when adding a new component: `start.sh` is append-only.

| Process | Port | Role |
|---|---|---|
| `ttyd` | 7681 | Web terminal. `exec` target and `readinessProbe` target. If it exits, the container exits. |
| `cortex_proxy.py` | 127.0.0.1:8080 | Cortex compatibility proxy. Has its own watchdog in `start.sh`. Critical path. |
| `hermes serve` | 0.0.0.0:9119 | Agent backend. The Desktop and CLI connect here. `--skip-build` is mandatory. |
| `tailscaled` | unix socket | Tailscale daemon, userspace networking (no TUN/TAP, no NET_ADMIN needed). |
| `sshd` | 22 | Fallback access via SSH key in Snowflake Secret. |
| `cloudflared` | — | Outbound tunnel via Cloudflare token in Snowflake Secret. Fallback for Tailscale. |

## Block Storage and the Seed Problem

SPCS mounts block storage on `/root` at runtime. This mount **hides all image content under `/root`**, including the Hermes configuration, skills, and memory that the installer writes there.

The image saves a copy of the installer output in `/opt/hermes-seed` at build time. `start.sh` restores any missing files with `cp -a -n` (no-clobber) at every boot. Existing files — sessions, memory, custom skills, configuration — are never overwritten.

## Client Connectivity

The Hermes Dashboard at port 9119 requires authentication when bound to a non-loopback address (`--host 0.0.0.0`). Tailscale relays traffic to the container using DERP over TCP/443, which passes through SPCS external access integrations. The Snowflake ingress at port 7681 is not involved in client sessions.

The Tailscale IP is stable across container restarts once the state is persisted on the block volume. The internal SPCS IP changes at every restart and is not used for client connectivity.

## Model Configuration & Shared Multi-API Proxy (`snowflake-proxy-cortexrestapi`)

Hermes supports two proxy topologies on SPCS:

1. **Shared Multi-API Standard Proxy ([`snowflake-proxy-cortexrestapi`](https://github.com/fbattaglieri-snowflake/snowflake-proxy-cortexrestapi), recommended):**
   - Exposes all three Snowflake Cortex REST API protocols over SPCS internal DNS (`http://<proxy-service>.<namespace>.svc.spcs.internal:8080`):
     - `snowflake-standard-chat` (`/chat/v1`, `api_mode: chat_completions`, `discover_models: true`)
     - `snowflake-standard-responses` (`/responses/v1`, `api_mode: codex_responses`, `discover_models: true`)
     - `snowflake-standard-messages` (`/messages`, `api_mode: anthropic_messages`, `discover_models: true`)
   - Performs universal `ProfileV2` client normalization (`1..128000` token budgets, `ordered-items` SSE streaming on Responses, `store=false`, multi-round tool call ID deduplication `call_0..N`, and `ReplayCache` rehydration of `encrypted_content` and `thinking` signatures).
   - Models are qualified with [`cortex-model-compatibility-matrix`](https://github.com/fbattaglieri-snowflake/cortex-model-compatibility-matrix) and promoted zero-touch via `scripts/promote_model.py`; Hermes discovers them dynamically without static `models.json` edits or container restarts.

2. **Embedded Single-Container Fallback (`proxy/cortex_proxy.py`):**
   - `models.json` is the local source of truth for the embedded loopback proxy (`127.0.0.1:8080/v1/models`) and for the fallback `snowflake-cortex-proxy` provider in `hermes_configure.py`.
   - The file can be updated without rebuilding the image by uploading a new version to the Snowflake stage and waiting for the proxy to reload it (it checks `mtime` on every request).

## Suspend/Resume Order

Hermes has no external database. All state lives on the block volume. The only dependency constraint is:

- **Suspend**: services first, then the compute pool.
- **Resume**: compute pool first, then services.

After a resume, the `start.sh` watchdog brings the proxy, `hermes serve`, and the Tailscale processes back up automatically (once the image includes them in the startup script).
