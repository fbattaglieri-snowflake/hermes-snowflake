# Operations

## Upgrade

Run the **Deploy to Snowflake** workflow with an explicit image tag. The workflow builds an immutable commit-SHA image, stages the SPCS specification, and applies `ALTER SERVICE`.

**Never drop and recreate the Hermes service** during an upgrade. The block volume and the tailnet IP persistence are tied to the service object. Dropping the service detaches or deletes the volume.

## Suspend

Use the **Operate Stack** workflow with `suspend`, or run:

```sql
ALTER SERVICE <DATABASE>.<SCHEMA>.HERMES_SERVICE SUSPEND;
ALTER COMPUTE POOL <COMPUTE_POOL> SUSPEND;
```

## Resume

```sql
ALTER COMPUTE POOL <COMPUTE_POOL> RESUME;
ALTER SERVICE <DATABASE>.<SCHEMA>.HERMES_SERVICE RESUME;
```

Wait 3–4 minutes for the container startup sequence to complete, then verify with `curl -s http://<tailnet-ip>:9119/api/status`.

## After a Restart: Proxy Patch

`start.sh` copies `/opt/cortex_proxy.py` (the image's built-in copy) over `/root/.hermes/cortex_proxy.py` at **every** boot, unconditionally. This is correct only as long as the image's proxy is the one from this repository.

If you ever hot-patch `/root/.hermes/cortex_proxy.py` inside a running container, that change is **not lost but overwritten** at the next boot — and silently, because the proxy still starts and answers. Deleting `/opt/cortex_proxy.py` does not help: `/opt` lives in the image's ephemeral layer and returns on the next boot. The only durable fix is to build a new image.

Symptom of a stale image: requests that contain parallel tool calls start failing again with HTTP 400 (deviation 6). Verify with:

```bash
grep -c collapse_parallel_tool_calls /opt/cortex_proxy.py /root/.hermes/cortex_proxy.py
```

Both must report a non-zero count and the same `md5sum`. If `/opt` reports 0, the deployed image predates the fix and must be rebuilt.

## Desktop Remote Gateway

`start.sh` starts `tailscaled` and `hermes serve` (port 9119) at boot, with a 60-second watchdog, so the Desktop reconnects on its own after a restart. Confirm from the boot log:

```text
[hermes] tailnet IP: 100.x.y.z
[hermes] hermes serve pronto su 100.x.y.z:9119 (Remote gateway del Desktop)
```

The tailnet IP is logged deliberately: the `readinessProbe` floods the log every 5 seconds, so with a tail of 400 it becomes unreachable after roughly 30 minutes.

The watchdog polls `/api/status` rather than `hermes serve --status`, which reports nothing running while the port is serving. See [client-setup.md](client-setup.md) for recovery and the traps involved.

## Gateway and Cron

The gateway is started automatically by `start.sh` when `TELEGRAM_BOT_TOKEN` is set, after the proxy is confirmed up, and is kept alive by a 60-second watchdog. Confirm it with:

```bash
hermes cron status
```

Expect `✓ Gateway is running — cron jobs will fire automatically`. If it reports `⚠ Gateway is not running`, **no messaging platform is being listened to and no cronjob will fire** — the gateway process is both the platform listener and the cron ticker. This failure is silent: there is no error, only absence of replies. `hermes gateway install` requires systemd, which SPCS does not provide, so the gateway must run as a child process.

### Cronjobs and inference-config drift

A cronjob created while the global inference config pointed at one model is **skipped**, not run, if that config later changes and the job is unpinned:

```text
Skipped to prevent unintended spend: global inference config drifted since this job
was created (model 'A' -> 'B'), and this job is unpinned.
```

This is a spend guard working as designed, not a defect. The alert is sent once and the job stays skipped until pinned:

```bash
hermes cron edit <job_id> --provider <provider> --model <model>
```

Pin **every** active job, not only the one that alerted: all jobs created before the change carry the same stale snapshot and will skip in turn as their schedules come due. Pinning clears the job's `model_snapshot`.

To keep the default model declarative instead of living only in block-volume state, set `HERMES_MODEL` in the service spec. `hermes_configure.py` assigns `model.default` from it, so without it a config re-patch resets the default to the script's fallback.

## Viewing Logs

```sql
SELECT SYSTEM$GET_SERVICE_LOGS('<DATABASE>.<SCHEMA>.HERMES_SERVICE', 0, 'hermes', 400);
```

**Important**: the `readinessProbe` on port 7681 (`ttyd`) generates a log line every 5 seconds. With a tail of 400, startup messages from more than ~30 minutes ago are no longer reachable. Log the tailnet IP and proxy status periodically to a file on the block volume if you need to retrieve them later without opening the web terminal.


## Backup

An optional **Backup State** GitHub Action archives Hermes state from the block volume to a Snowflake internal stage. See [backup-recovery.md](backup-recovery.md) for setup, manual backup procedures, and recovery scenarios.

**Critical**: never `DROP SERVICE` without first backing up the block volume. The volume and all accumulated state (sessions, memory, skills, cronjobs) are destroyed when the service is dropped.
