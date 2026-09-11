# Backup and Recovery

## Automatic backup is not implemented

The GitHub backup workflow deliberately fails without authenticating to Snowflake.
It does not create an archive or upload one. `ENABLE_BACKUP` does not enable an
implementation. Do not use a successful CI build as evidence of recoverability.

The previous workflow treated a notification API as a container shell and never
transferred the resulting file to a stage. That workflow was not a working backup.

## Operator-managed recovery requirements

State, sessions, custom skills, configuration and SOUL.md live on the persistent
volume. Image seeds are defaults, not a backup of user changes. Do not drop or
recreate a service until an independent backup has been verified.

An operator must design and validate the following steps for their environment:

1. Establish an approved authenticated shell and file-transfer path. A file in the
   container's `/tmp` is not a file on the operator's machine or GitHub runner.
2. Coordinate application writers and take a consistent database backup, using
   SQLite's backup facilities or an appropriately quiesced state. Copying a live
   database and WAL at different times is not sufficient.
3. Include configuration, sessions, skills and any required node identity state.
   Decide explicitly whether `/root/tailscale` is included; `.hermes` alone does
   not contain it. Never start two nodes with the same restored identity.
4. Transfer the archive off the volume over the approved authenticated path.
   Store it in an access-restricted, encrypted location with retention and a
   checksum. Archives may contain credentials, auth state and private conversations.
   Never upload them as public GitHub artifacts or commit them to Git.
5. Restore into an isolated destination without affecting the original service.
   Verify database integrity, expected sessions and customizations, and ensure
   scheduled jobs and messaging are not accidentally activated during the drill.
6. Record the backup timestamp, checksum, scope, restore outcome and operator.

No end-to-end backup/restore drill has been performed for this repository's
deployment path. These are acceptance requirements, not executable instructions
or a claim that backups exist. Account-specific access and deployment belong in
the operator's private runbook, never in this public repository.