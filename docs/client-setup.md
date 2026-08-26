# Client Setup: Hermes Desktop + Tailscale

This guide connects a Hermes Desktop instance to the backend running in SPCS.

## Prerequisites

- The SPCS Hermes service is running and `hermes serve` is active on port 9119.
- You have the tailnet IP of the container (`tailscale ip -4` from inside the container, or from Tailscale Admin Console).
- You know the dashboard username and password configured via Snowflake Secrets.

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

If you see `connection refused` or a timeout, the problem is upstream: the service is suspended, `hermes serve` is not running, or Tailscale is not set up. Do not open the Desktop yet.

## Step 2 — Install Tailscale

Download and install Tailscale from https://tailscale.com/download for your operating system. Log in to the **same tailnet** as the container.

Verify: `tailscale status` should list the `hermes-spcs` node.

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

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Desktop asks for a session token instead of showing Sign in | `basic` provider not active | Verify `HERMES_DASH_USERNAME` and `HERMES_DASH_PASSWORD_HASH` are set in `/root/.hermes/.env` |
| Disconnects on every container restart | `HERMES_DASHBOARD_BASIC_AUTH_SECRET` missing or changes | Set a stable, fixed value for this secret |
| WebSocket close code 4401 | Session expired or secret changed | Sign in again |
| WebSocket close code 4403 | Host guard reject | Verify `hermes serve` is bound to `0.0.0.0`, not `127.0.0.1` |
| Backend is ready but chat does not start | The probe and the chat are different endpoints | Test the WebSocket explicitly: `curl -s http://<ip>:9119/api/status` and then try the Desktop's built-in **Test** button |

## Resuming the service

With this topology, the client cannot wake a suspended service. Tailscale has no process to initiate the outbound connection when the container is stopped.

```sql
ALTER SERVICE <DATABASE>.<SCHEMA>.HERMES_SERVICE RESUME;
```

Wait 3–4 minutes, then verify with the `curl` from Step 1.
