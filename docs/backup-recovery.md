# Backup and Recovery

Backup is **opt-in**. Enable it only when you need it — all backup operations are billable.

## What to back up

| Component | Where it lives | Backup method |
|---|---|---|
| **Hermes state** (`state.db`, sessions, memory) | Block volume under `/root` | Archive to Snowflake stage |
| **Configuration** (`config.yaml`, `SOUL.md`, skills) | Block volume under `/root/.hermes` | Archive to Snowflake stage |
| **Cortex proxy** | Image layer (`/opt/cortex_proxy.py`) | Already versioned in Git |
| **Snowflake objects** (database, secrets, EAI) | Snowflake metadata | Re-created by `10_bootstrap.sql` |

## Block Volume: the single point of failure

Everything that makes your Hermes instance unique — sessions, memory, custom skills, cronjobs, SOUL.md edits — lives on the block volume mounted at `/root`. The image seeds `/opt/hermes-seed` at build time and `start.sh` restores missing files with `cp -a -n` (no-clobber), but this only covers the default configuration, not your accumulated state.

**`DROP SERVICE` destroys the block volume.** There is no undo. Always back up before any operation that drops or recreates the service.

## State Archive to Stage

The **Backup** workflow (`.github/workflows/backup.yml`) archives Hermes state from the block volume to a Snowflake internal stage. Enable it by:

1. Setting the `ENABLE_BACKUP` variable to `true` in your GitHub repository settings.
2. Optionally uncommenting the `schedule` trigger in the workflow file.

The workflow runs a `snow sql` command inside the container via `SYSTEM$EXECUTE_IN_CONTAINER` (when available) or exports via the web terminal. Archives land in `@<DATABASE>.<SCHEMA>.HERMES_BACKUPS/state/`.

### Manual backup from inside the container

If you have shell access (SSH or ttyd):

```bash
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
tar czf "/tmp/hermes-state-${TIMESTAMP}.tgz" \
  --exclude='node' --exclude='bin' --exclude='cache' \
  --exclude='audio_cache' --exclude='__pycache__' \
  --exclude='models_dev_cache.json' \
  -C /root .hermes

# Then copy to a Snowflake stage from outside the container:
# snow stage copy /tmp/hermes-state-<timestamp>.tgz @<DATABASE>.<SCHEMA>.HERMES_BACKUPS/state/
```

### What is included and excluded

| Included | Excluded (regenerable) |
|---|---|
| `state.db`, `state.db-wal` | `node/` (~227 MB) |
| `sessions/`, `state-snapshots/` | `bin/` (~79 MB) |
| `SOUL.md`, `config.yaml` (+ `.bak` files) | `cache/`, `audio_cache/` |
| `skills/` (custom and bundled) | `__pycache__/` |
| `channel_directory.json`, `auth.json` | `models_dev_cache.json` |

## Recovery Scenarios

| Scenario | Recovery |
|---|---|
| Service restart | Automatic: `start.sh` restores seed files, proxy, and starts all services |
| Proxy stale after image update | Rebuild image with updated proxy — `start.sh` copies `/opt/cortex_proxy.py` at every boot |
| Block volume lost (DROP SERVICE) | Restore archive from stage: download `.tgz`, redeploy service, extract into `/root` via shell |
| Full environment rebuild | Re-run bootstrap + deploy, then restore state archive into the new container |
| Cronjobs / gateway not firing | Check `hermes cron status`; see [operations.md](operations.md) for inference-config drift |

## What is NOT backed up

- **Tailscale node state** — after volume loss, Tailscale re-registers with a new IP. Update your Desktop client configuration.
- **Cloudflare tunnel** — the tunnel token is in a Snowflake secret and survives independently.
- **SSH authorized keys** — stored in a Snowflake secret, re-injected at boot by `start.sh`.
