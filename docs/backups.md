# Backups

Two independent recovery mechanisms are required — neither alone is
sufficient.

## 1. Managed PostgreSQL provider backups / PITR

Use a managed PostgreSQL provider (Neon, Supabase, Render, RDS, or similar)
specifically because point-in-time recovery is not worth self-hosting for
this project's scale. **Verify the specific plan tier actually includes
PITR** before relying on it — some free tiers cap retention very short.

## 2. Independent encrypted `pg_dump` archives

A scheduled job runs, at minimum daily:

```bash
pg_dump -U stranger_club_migrator -d <db> -Fc -f backup-$(date +%F).dump
```

- **Custom format** (`-Fc`): compressed, supports selective table restore.
- Uploaded to the **same object storage account, a separate bucket/prefix**
  (`backups/postgres/`) from application storage.
- **Distinct credentials** from both the application's runtime storage
  credential and its database credential — a credential scoped to write
  (and read, for restore) the `backups/` prefix only.
- **The application runtime credential must not be able to delete backup
  archives** — same IAM-scoping discipline as `proofs/` (see `storage.md`).
- Retention: 30 daily + 12 monthly snapshots — a practical starting point,
  not an invented enterprise number; revisit if the product's actual risk
  tolerance changes.
- Encryption: the object storage provider's server-side encryption at rest
  is sufficient at this stage; client-side encryption before upload is a
  reasonable future hardening step if the backup bucket's own access
  control is ever in question.
- **Monitoring**: the same scheduled job verifies the previous run's
  archive exists and exceeds a sane minimum size, alerting on failure or on
  a suspiciously small dump (a near-empty backup is worse than no backup —
  it creates false confidence).

## RPO / RTO

| | Target | Status |
|---|---|---|
| RPO (PITR available) | ≤ 5 minutes | Depends on the managed provider's PITR being enabled and verified — not yet configured against a real provider account in this environment |
| RPO (fallback, PITR unavailable) | ≤ 24 hours | Achieved by the daily `pg_dump` schedule once deployed |
| RTO | ≤ 1 hour | **Measured** via `scripts/restore_test.py` against a representative dataset — see below |

**These are targets, not claims.** A controlled restore test was run in
this environment (small test dataset: a handful of organizers, events,
registrations, payments, and proofs) — `pg_restore` completed in under one
second. That number is **not representative of a real production restore**
and must be re-measured with `scripts/restore_test.py` against a realistic
data volume before this table's RTO row is treated as verified for a real
deployment. Re-run the script and update this table with the actual
measured time whenever the database grows meaningfully or at least
quarterly.

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
