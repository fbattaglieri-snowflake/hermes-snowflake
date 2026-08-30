# Client Setup: Hermes Desktop + Tailscale

This guide connects a Hermes Desktop instance to the backend running in SPCS, and brings that backend back up after a container restart.

## Prerequisites

- The SPCS Hermes service is `READY`.
- `tailscaled` and `hermes serve` are started automatically by `start.sh` at boot, and kept alive by a 60-second watchdog. The boot log reports the tailnet IP and the backend state:

  ```text
  [hermes] tailnet IP: 100.x.y.z
  [hermes] hermes serve pronto su 100.x.y.z:9119 (Remote gateway del Desktop)
  ```

  If those lines are missing, see [If the backend is not up](#if-the-backend-is-not-up).
- You have the tailnet IP of the container (from the boot log above, `tailscale ip -4` inside the container, or the Tailscale Admin Console). It is **stable across restarts** because `tailscaled` state lives on the block volume.
- You know the dashboard username and password configured via Snowflake Secrets.
- On first deployment only: the `TS_AUTHKEY` secret is set to a **reusable, non-ephemeral** Tailscale auth key. See [configuration.md](configuration.md).

### Version alignment

Keep the backend revision **no older than** the Desktop build. The Desktop is updated from upstream on its own schedule, while the backend is pinned in the image (`HERMES_GIT_SHA`, see [configuration.md](configuration.md)). A `hermes update` run inside the container is **ephemeral**: it installs outside the block volume and is lost at the next restart, so it is not a fix — bump the pin and rebuild instead.

## Step 1 — Verify the backend before opening the Desktop

This step isolates "the tailnet is not working" from "the Desktop is not working" and saves significant debugging time.

```bash
curl -s http://<tailnet-ip>:9119/api/status | python3 -m json.tool | grep -E "auth_required|auth_providers"
```

Expected output:
```
"auth_required": true,
"auth_providers": ["basic"]
```

If you see `connection refused` or a timeout, the problem is upstream: the service is suspended, `hermes serve` is not running, or Tailscale is not set up. Do not open the Desktop yet — go to [If the backend is not up](#if-the-backend-is-not-up).

## Step 2 — Install Tailscale

Download and install Tailscale from https://tailscale.com/download for your operating system. Log in to the **same tailnet** as the container.

Verify: `tailscale status` should list the `hermes-spcs` node.

On Windows, `winget install --id Tailscale.Tailscale --exact` works. After logging in, `tailscale status` must show **both** the client and `hermes-spcs`; if only the client appears, the container side is not up.

> A `nodekey:...` shown in the machine details of the Admin Console is **not** an auth key. Registering the container requires a reusable auth key in the form `tskey-auth-...`.

## Step 3 — Install Hermes Desktop

Download and install Hermes Desktop from https://hermes-agent.nousresearch.com. The Desktop does not need a local Hermes backend to function as a remote client.

## Step 4 — Add the remote connection

1. Open **Settings → Gateways**.
2. Set **Connection mode** to **Remote gateway**.
3. Enter the *Remote URL*: `http://<tailnet-ip>:9119`
4. Click **Sign in**, enter your username and password.
5. Click **Save and reconnect**.

## Step 5 — Register with a name

In the multi-connection registry (below the main gateway settings, or via the connection button in the profile rail), add the connection and give it a distinct name, for example `SPCS-Hermes`. The `Local` entry is managed by the app and cannot be removed.

## Step 6 — Validate with a real chat message

The Desktop probe only checks `GET /api/status`, which is a public endpoint. A WebSocket on `/api/ws` is separate. Send a message and verify that the response streams successfully.

## If the backend is not up

`start.sh` starts `tailscaled` and `hermes serve` at boot and a watchdog restarts them every 60 seconds if they die, so this should be rare. It still happens in two cases: the image predates that autostart, or `TS_AUTHKEY` was never set **and** there is no `tailscaled` state on the volume yet, so the node was never registered.

Check the boot log first — it says which step failed:

```sql
SELECT SYSTEM$GET_SERVICE_LOGS('<DATABASE>.<SCHEMA>.HERMES_SERVICE', 0, 'hermes', 400);
```

| Log line | Meaning |
|---|---|
| `nessuno stato Tailscale e TS_AUTHKEY assente — nodo non registrato` | Set the `TS_AUTHKEY` secret and restart the service |
| `tailscaled non partito` | Check `/tmp/tailscaled.log` in the container |
| `hermes serve non risponde` | The line includes the tail of `serve.log`; an **empty** `serve.log` means the build attempt (see traps below) |
| No `tailnet IP` line at all | The image predates the autostart — run the manual sequence below |

**No data is lost** in any of these cases: sessions and state are on the block volume. Only processes need to come back.

To bring them up by hand, open the web terminal (the `terminal` endpoint on port 7681) and run:

```bash
TS_SOCK=/root/tailscale/sock

# 1. tailscaled. SPCS provides no /dev/net/tun and no NET_ADMIN, so
#    userspace networking is mandatory, not a preference.
tailscaled --tun=userspace-networking \
  --state=/root/tailscale/tailscaled.state --socket="$TS_SOCK" &

# 2. Rejoin the tailnet. --accept-dns=false is deliberate: the container must
#    keep resolving internal SPCS hosts and Snowflake, so its resolver must
#    not be rewritten. Add --authkey "$TS_AUTHKEY" only if there is no state yet.
tailscale --socket="$TS_SOCK" up --hostname hermes-spcs --accept-dns=false
tailscale --socket="$TS_SOCK" ip -4        # the IP the Desktop points at

# 3. The backend. --skip-build is MANDATORY (see the traps below).
nohup hermes serve --skip-build --host 0.0.0.0 --port 9119 > /root/serve.log 2>&1 &
curl -s http://127.0.0.1:9119/api/status | python3 -m json.tool | grep -E 'auth_required|auth_providers'

# 4. Expose 9119 on the tailnet.
tailscale --socket="$TS_SOCK" serve --bg --tcp 9119 tcp://localhost:9119
```

On an image older than the autostart, the Tailscale binaries are not in `/usr/local/bin` but under `/root/tailscale/tailscale_*_amd64/` — use those paths instead.

### Three traps in this procedure

- **`--skip-build` is mandatory.** Without it `hermes serve` stays alive but **never starts listening**, and `serve.log` is **empty** — it is trying to build the web UI, and `web/dist` does not exist in the image. With the flag it listens within ~3 seconds. An empty `serve.log` is the signature of this mistake.
- **`hermes serve --status` lies.** It reports `No hermes dashboard processes running` while the process is listening on `0.0.0.0:9119`. Never use it to decide whether the backend is up; poll `/api/status` instead — which is what the watchdog does.
- **Binding to `0.0.0.0` enables the auth gate by itself.** No extra flag is needed, and `--insecure` is a no-op. There is no Host restriction: a forged `Host` header still gets `101` on the WebSocket, so DNS-rebinding protection will not be what is blocking you.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `connection refused` on port 9119, but the service is `READY` | `tailscaled` or `hermes serve` did not come up, or the node was never registered | [If the backend is not up](#if-the-backend-is-not-up) |
| `serve.log` is **empty** and nothing is listening | `--skip-build` is missing: it is building the web UI, which is absent from the image | Restart with `hermes serve --skip-build …` |
| `hermes serve --status` says nothing is running, but the port answers | `--status` is unreliable | Ignore it; trust `curl /api/status` |
| Desktop asks for a session token instead of showing Sign in | `basic` provider not active | Verify `HERMES_DASH_USERNAME` and `HERMES_DASH_PASSWORD_HASH` are set in `/root/.hermes/.env` |
| Disconnects on every container restart | `HERMES_DASHBOARD_BASIC_AUTH_SECRET` missing or changes | Set a stable, fixed value for this secret — with identical values, Desktop sessions survive a restart |
| WebSocket close code 4401 | Session expired or secret changed | Sign in again |
| WebSocket close code 4403 | Host guard reject | Verify `hermes serve` is bound to `0.0.0.0`, not `127.0.0.1` |
| Backend is ready but chat does not start | The probe and the chat are different endpoints | Test the WebSocket explicitly: `curl -s http://<ip>:9119/api/status` and then try the Desktop's built-in **Test** button |
| A manual WebSocket test fails on the second attempt | The `?ticket=` is **single-use** and expires in ~30 seconds | Request a fresh ticket for every attempt |
| The Desktop connects but the agent behaves unlike the CLI | Backend and Desktop are on different Hermes revisions | Align them — bump `HERMES_GIT_SHA` and rebuild; do not use `hermes update`, it does not survive a restart |

### Reading the logs

When something fails, collect `desktop.log` from the client **and** the container logs for the same time window, and look for the close code on `/api/ws`. The `readinessProbe` on port 7681 writes a log line every 5 seconds, so with a tail of 400 the startup messages are unreachable after roughly 30 minutes:

```sql
SELECT SYSTEM$GET_SERVICE_LOGS('<DATABASE>.<SCHEMA>.HERMES_SERVICE', 0, 'hermes', 400);
```

Retrieve the logs in the same statement that produces them where possible, and do not rely on scrolling back later.

## Resuming the service

With this topology, the client cannot wake a suspended service. Tailscale has no process to initiate the outbound connection when the container is stopped.

```sql
ALTER SERVICE <DATABASE>.<SCHEMA>.HERMES_SERVICE RESUME;
```

Wait 3–4 minutes, then verify with the `curl` from Step 1. A resume recreates the container, so `tailscaled` and `hermes serve` start again from scratch — automatically, but not instantly. If the tailnet IP is unchanged the Desktop reconnects on its own; check the boot log if it does not.
