# Backups

Two independent recovery mechanisms are required — neither alone is
sufficient.

## 1. Managed PostgreSQL provider backups / PITR

Use a managed PostgreSQL provider (Neon, Supabase, Render, RDS, or similar)
specifically because point-in-time recovery is not worth self-hosting for
this project's scale. **Verify the specific plan tier actually includes
PITR** before relying on it — some free tiers cap retention very short.

## 2. Independent encrypted `pg_dump` archives

**Status: a concrete, working template exists — `.github/workflows/backup.yml` —
but it is not active.** GitHub Actions never runs a `schedule:`-triggered
workflow from a local working tree, and every secret it references is
unset in this repository. It does nothing until an operator (1) reviews it,
(2) commits it to the default branch, and (3) adds the real secrets listed
in the file. Do not read its presence as "backups are running in
production" — they are not, anywhere, yet.

The dump/upload/verify sequence the workflow runs was exercised for real
in this environment (a local PostgreSQL container's `stranger_club_backup`-
equivalent role, dumped and uploaded to a local MinIO bucket, then the
upload was confirmed present via `head_object`) — the *logic* is proven;
the *schedule actually firing in production* is deployment-time-only, since
no production GitHub repository/secrets exist here.

```bash
# What the workflow runs, using the read-only backup role (see database.md)
# — never the application or migrator credential:
pg_dump --dbname="$BACKUP_DATABASE_URL" -Fc -f backup-$(date -u +%Y-%m-%dT%H-%M-%SZ).dump
```

- **Custom format** (`-Fc`): compressed, supports selective table restore.
- Runs as `stranger_club_backup` (read-only via `pg_read_all_data` — see
  `database.md`) — verified locally that this role has everything `pg_dump`
  needs and nothing more.
- Uploaded to the **same object storage account, a separate bucket/prefix**
  (`backups/postgres/`) from application storage, using a credential scoped
  only to that prefix — distinct from the application's `SC_STORAGE_*`
  runtime credential, the storage-admin credential used for
  `scripts/reconcile_storage.py`, and the database credentials.
- **Neither the application runtime credential nor the backup-storage
  credential should be able to delete backup archives** — the application
  credential never has any relationship to `backups/` at all (it is never
  given a credential scoped there); the backup-storage credential's own
  IAM policy should also exclude `s3:DeleteObject`/`s3:DeleteObjectVersion`
  on that prefix — retention/expiry is a deliberate lifecycle-policy or
  manual decision, never something a routine backup run can perform.
- Retention: 30 daily + 12 monthly snapshots — a practical starting point,
  not an invented enterprise number; revisit if the product's actual risk
  tolerance changes. Implement via the object storage provider's lifecycle
  rules once provisioned (not yet configured against a real account here).
- Encryption: the object storage provider's server-side encryption at rest
  is sufficient at this stage; client-side encryption before upload is a
  reasonable future hardening step if the backup bucket's own access
  control is ever in question.
- **Monitoring**: the workflow itself fails (a red GitHub Actions run) if
  the dump is missing or under a size floor — a near-empty backup is worse
  than no backup, since it creates false confidence. A red run is the
  signal; wire GitHub's own notification settings (or a status-check
  webhook) to actually alert a human — the workflow file does not send a
  notification anywhere by itself.

## RPO / RTO

| | Target | Status |
|---|---|---|
| RPO (PITR available) | ≤ 5 minutes | Depends on the managed provider's PITR being enabled and verified — not yet configured against a real provider account in this environment |
| RPO (fallback, PITR unavailable) | ≤ 24 hours | Achieved by the daily `pg_dump` schedule once deployed |
| RTO | ≤ 1 hour | **Measured** via `scripts/restore_test.py` against a representative dataset — see below |

**These are targets, not claims.** A controlled restore test was run in
this environment (small test dataset: a handful of organizers, events,
registrations, payments, and proofs) — `pg_restore` completed in under one
second, then `scripts/restore_test.py` validated the Alembic head, every
foreign key, every product invariant, and a live application `/ready`
check, all against the restored database. That timing number is **not
representative of a real production restore** and must be re-measured with
`scripts/restore_test.py` against a realistic data volume before this
table's RTO row is treated as verified for a real deployment. Re-run the
script and update this table with the actual measured time whenever the
database grows meaningfully or at least quarterly.

The Alembic-head check is deliberately its own explicit assertion inside
`restore_test.py` (`validate_alembic_head`), not just inferred from
`/ready` returning 200 — `/ready`'s own check would otherwise be
short-circuited by `create_app()`'s default auto-migrate behaviour silently
"fixing" a genuinely incomplete restore before anyone could observe the
mismatch. The script forces `SC_AUTO_MIGRATE=false` for its
application-level check for exactly this reason, and this behaviour has an
automated regression test
(`test_restore_scripts_alembic_check_detects_a_real_mismatch`) that
deliberately corrupts a restored database's recorded migration version and
confirms the check actually catches it, not just reports success by
default.

## Payment-proof evidence: DB + object storage together

PostgreSQL backups alone are not sufficient — proof binaries live in
object storage, not the database. A restored database must never result in
silently missing financial evidence. The relationship is:

```
Postgres backup  (metadata: PaymentProof.storage_key, hash, status, ...)
       +
Object storage    (the actual screenshot bytes, referenced by storage_key)
       =
Complete, recoverable evidence
```

After **any** database restore, run:

```bash
python scripts/reconcile_storage.py --database-url <restored-db-url> --bucket <bucket>
```

This confirms every `PaymentProof.storage_key` (and QR `storage_key`)
referenced in the restored database still resolves to a real object. A
non-empty `missing_from_storage` list in its report means evidence is
unavailable and must be investigated immediately (see
`disaster-recovery.md`, scenario F) — a restore is not "done" until this
check is clean.

Object storage itself is durable at the provider level (R2/S3 class
durability); bucket versioning is evaluated but not yet enabled against a
real account (see `storage.md`) — enabling it adds a second, independent
recovery path specifically for the proof binaries (recovering an
accidentally overwritten or deleted object even before falling back to any
backup process).

## Backup verification is not restore verification

A backup job succeeding (`pg_dump` exits 0, file uploaded) proves nothing
about whether it can actually be restored. Section "Restore testing" below
— and `scripts/restore_test.py` — is what actually validates a backup.
