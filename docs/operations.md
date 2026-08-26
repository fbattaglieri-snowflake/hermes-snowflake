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

The `start.sh` in the current image copies `/opt/cortex_proxy.py` (the image's built-in copy) over `/root/.hermes/cortex_proxy.py` at every boot. If the image's proxy is older than the version in this repository, the block-volume copy will be silently overwritten.

The correct long-term fix is to build a new image that includes the updated proxy. Until then, reapply the patch manually after each restart. The startup script can also be extended to detect the mtime divergence and skip the overwrite if the volume copy is newer.

## Viewing Logs

```sql
SELECT SYSTEM$GET_SERVICE_LOGS('<DATABASE>.<SCHEMA>.HERMES_SERVICE', 0, 'hermes', 400);
```

**Important**: the `readinessProbe` on port 7681 (`ttyd`) generates a log line every 5 seconds. With a tail of 400, startup messages from more than ~30 minutes ago are no longer reachable. Log the tailnet IP and proxy status periodically to a file on the block volume if you need to retrieve them later without opening the web terminal.
