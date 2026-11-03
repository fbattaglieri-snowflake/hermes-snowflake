# Backup & Recovery

> [!WARNING]
> **Block Volume Persistence Rule:** Everything that makes your Hermes deployment unique — conversation history (`state.db`), memories, custom skills, cronjobs, `SOUL.md`, and the Tailscale node identity (`/root/tailscale/tailscaled.state`) — resides on the SPCS block volume mounted at `/root`.
> **`DROP SERVICE` destroys the block volume** unless a snapshot exists. Always create a block volume snapshot or stage archive before dropping or recreating the service.

---

## 1. What Lives Where

| Component | Location | Persistence & Backup Mechanism |
|---|---|---|
| **Hermes State & Sessions** (`state.db`, `sessions/`) | `/root/.hermes` (Block Volume) | SPCS Block Volume Snapshot (`CREATE SNAPSHOT`) or Stage `.tgz` archive |
| **Configuration & Persona** (`config.yaml`, `SOUL.md`, `skills/`) | `/root/.hermes` (Block Volume) | Seeded from `/opt/hermes-seed` (`no-clobber`), backed up via Snapshot / Stage |
| **Tailscale Node Identity** (`tailscaled.state`) | `/root/tailscale` (Block Volume) | Included in SPCS Block Volume Snapshot (`CREATE SNAPSHOT`) |
| **Hermes Code & Python Env** (`/opt/hermes/*`) | Container Image Layer | Immutable in Git + SPCS Image Repository |
| **Snowflake Infrastructure & Secrets** | Snowflake Metadata | Re-creatable via `infrastructure/sql/10_bootstrap.sql` |

---

## 2. Method A: Native SPCS Block Volume Snapshot (Recommended)

The **Backup State** workflow (`.github/workflows/backup.yml`) or a direct SQL statement creates a native Snowflake snapshot of the `hermes-data` block volume:

```sql
CREATE SNAPSHOT IF NOT EXISTS <DATABASE>.<SCHEMA>.HERMES_STATE_20261103
  FROM SERVICE <DATABASE>.<SCHEMA>.HERMES_SERVICE
  VOLUME 'hermes-data'
  INSTANCE 0
  COMMENT = 'Hermes block volume backup';

-- Verify snapshot status (wait until STATE = CREATED):
DESCRIBE SNAPSHOT <DATABASE>.<SCHEMA>.HERMES_STATE_20261103;
```

In addition, `infrastructure/specs/hermes.service.yaml` configures `snapshotOnDelete: true` (`snapshotDeleteAfter: 14d`) as a safety net.

---

## 3. Method B: Manual `.tgz` Archive to a Snowflake Internal Stage

From inside the container shell (via the web terminal on `:7681` or SSH):

```bash
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
tar czf "/tmp/hermes-state-${TIMESTAMP}.tgz" \
  --exclude='node' --exclude='bin' --exclude='installs' --exclude='tools' \
  --exclude='cache' --exclude='audio_cache' --exclude='__pycache__' \
  --exclude='models_dev_cache.json' \
  -C /root .hermes
```

Then copy the archive to `@<DATABASE>.<SCHEMA>.HERMES_BACKUPS/state/` using `snow stage copy`.

---

## 4. Recovery Scenarios

| Scenario | Recovery Procedure |
|---|---|
| **Container restart / SPCS pod reschedule** | Automatic: `start.sh` preserves `/root`, restores missing seed files (`cp -a -n`), links `/opt/hermes/installs`, and restarts all daemons (`tailscaled`, `hermes serve`, `hermes gateway`, `cortex_proxy.py`). |
| **Image upgrade (`ALTER SERVICE`)** | Automatic: block volume `/root` and tailnet IP remain intact; `/root/.hermes/installs` updates automatically to point to the new image's `/opt/hermes/installs`. |
| **Accidental `DROP SERVICE`** | Recreate the service restoring the block volume from the latest SPCS `SNAPSHOT`, or redeploy and extract the `.tgz` stage archive into `/root`. |
| **Cronjobs skipped after model change** | Run `hermes cron edit <job_id> --provider <provider> --model <model>` to clear the spend-guard `model_snapshot` (see [operations.md](operations.md)). |
