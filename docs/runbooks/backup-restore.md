# Backups and restore

## What is backed up
| Data | How | Where | Frequency |
|---|---|---|---|
| Postgres | Cloud SQL automated backups + point-in-time recovery (7 days of logs) | Cloud SQL | continuous |
| Postgres (logical) | `pg_dump -Fc` + manifest (row counts, migrations, audit-chain state) by the `backup-<env>` job | backups bucket (`backups/<UTC stamp>/`), versioned, retention policy | daily 02:15 UTC |
| Assets (video, audio, images) | object versioning on the assets bucket (30 days of old versions) + content-addressed keys | assets bucket | continuous |
| Secrets | Secret Manager versions | Secret Manager | on change |

The signing key private half lives only in the KMS; its recovery is the KMS's, not ours.

## The drill (do it monthly, and after any migration that touches many tables)
```
make restore-drill        # local/CI: migrate a scratch DB, back up, restore, verify
```
Against an environment: run the `backup` job, download the newest `backups/<stamp>/`
directory, then
```
BACKUP_CONNINFO="host=... dbname=postgres user=..." \
  python -m ytcore.ops.backup restore-drill --dir <downloaded dir>
```
The drill restores into a new scratch database (never over an existing one), then verifies:
dump checksum, applied migrations identical, **row counts of every table**, the audit hash
chain, the safety functions and triggers, pgvector, and (when a storage manifest exists) every
asset's sha256. It drops the scratch database and prints JSON; exit status 1 means the backup
is not trustworthy. Record the date and duration (`seconds`) in the ops log.

## Restoring for real
1. **Pause globally** ([kill-switch.md](kill-switch.md)). Stop workers and the hub.
2. Choose the source: PITR to a timestamp just before the damage (Cloud SQL console, to a new
   instance) or the newest logical backup. Prefer PITR for corruption/deletion; logical for
   loss of the instance.
3. Restore to a **new** database/instance, run the drill's verification against it
   (`verify_restore`), then point `database-url` at it (new secret version) and restart.
4. Restore storage objects from the assets bucket version history or the backup copy; keys are
   digests, so `verify_storage` proves integrity.
5. Reconcile: `ytctl ops pending-uploads` and compare uploads in YouTube Studio with the
   `uploads` table for the gap period ([stuck-job-recovery.md](stuck-job-recovery.md)).
6. Resume channel by channel, watching the first publish of each.

## Locking the backup retention
The backups bucket's retention policy ships unlocked so it can be tuned. Once the retention
period is agreed, lock it (irreversible): `gsutil retention lock gs://<backups bucket>`.
