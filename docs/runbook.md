# Runbook

Day-to-day operator tasks. See `disaster-recovery.md` for incident
scenarios, `backups.md` for the backup/restore relationship, `database.md`
and `storage.md` for the underlying design.

## Check application health

```bash
curl https://<host>/health   # process alive?
curl https://<host>/ready    # DB reachable + migration head matches + config valid?
```

## Check operational internals (PLATFORM_ADMIN only)

```bash
curl -b <organizer-session-cookie> https://<host>/internal/diagnostics
```

Returns DB pool stats, realtime backend type + active topic/subscriber
counts, and which storage backend is active. Never returns connection
strings, credentials, session tokens, or user/payment data.

## Run a migration

```bash
DATABASE_URL=postgresql+psycopg://stranger_club_migrator:...@host/db python -m backend.app.migrate
```

Safe to run concurrently from multiple pipelines — serializes on a
PostgreSQL advisory lock.

## Cut over from SQLite to PostgreSQL (first-time production launch)

1. Provision the PostgreSQL database and both roles (`database.md`).
2. `python -m backend.app.migrate` against the empty PostgreSQL database.
3. Stop the SQLite-backed application (maintenance window).
4. Rehearse first: `python scripts/migrate_sqlite_to_postgres.py --sqlite-path <path> --postgres-url <url> --dry-run`
5. Run for real (omit `--dry-run`), adding `--copy-objects-from <uploads-dir>`
   if the source used `LocalFilesystemStorage`, to also copy proof/QR
   binaries into the destination S3 bucket.
6. `python scripts/reconcile_storage.py --database-url <url> --bucket <bucket>`
   — confirm zero `missing_from_storage`.
7. Point the application's `DATABASE_URL`/`SC_STORAGE_*` at the new
   infrastructure and start it.

## Rotate a credential

See `disaster-recovery.md` scenario H — provision new, roll instances one
at a time, verify, then revoke the old credential.

## Test that backups actually restore

Do this **at least quarterly** and after any significant schema change —
a backup is not verified until this has been run:

```bash
python scripts/restore_test.py \
  --backup-file <latest-backup>.dump \
  --target-url <fresh-scratch-postgres-url> \
  --storage-bucket <bucket> --storage-endpoint-url <url> \
  --admin-access-key-id <admin-key> --admin-secret-access-key <admin-secret>
```

Record the printed restore time in `backups.md`'s RTO row. Never claim the
RTO target is met without this.

## Investigate a storage discrepancy

```bash
python scripts/reconcile_storage.py --database-url <url> --bucket <bucket> --json-report report.json
```

- `orphaned_in_storage`: objects with no referencing DB row — harmless
  (see `storage.md`), but review before ever deleting one.
- `missing_from_storage`: DB rows referencing objects that don't exist —
  **investigate immediately**, see `disaster-recovery.md` scenario F.

Delete a confirmed orphan only after manual review, using the script's
separate, more-privileged credential:

```bash
SC_STORAGE_ADMIN_ACCESS_KEY_ID=... SC_STORAGE_ADMIN_SECRET_ACCESS_KEY=... \
  python scripts/reconcile_storage.py --database-url <url> --bucket <bucket> \
  --confirm-delete proofs/<key1> proofs/<key2>
```

## Common failure signatures in logs

Full category-by-category coverage (including what's deliberately never
logged) is in `observability.md`. Quick reference for the most
operationally relevant lines:

| Log line | Meaning | Where to look |
|---|---|---|
| `migration_lock_acquired` / `migration_lock_released` | Normal migration run | `database.md` |
| `readiness_failed reason=alembic_head_mismatch` | `/ready` found the DB at an unexpected migration revision | New code deployed before its migration ran, or a migration partially failed — see `disaster-recovery.md` scenario G |
| `readiness_failed reason=database_unavailable` | `/ready` couldn't reach the database at all | Check DB connectivity/credentials |
| `realtime_listener_disconnected, reconnecting in Ns` | PostgreSQL LISTEN connection dropped (DB restart/network blip) | Expect a `realtime_listener_connected` shortly after; if not, check DB connectivity |
| `realtime_notify_failed` | A single realtime publish failed (best-effort) | Never affects correctness — the next fetch/reconnect self-heals; investigate if frequent |
| `rate_limit_backend_unavailable` | The rate-limit table couldn't be read/written | Requests fail closed (429) — check DB connectivity |
| `storage_put_failed` / `storage_get_failed` / `storage_presign_failed` | Object storage call failed after bounded retries | Client sees a clean 503 — check the storage provider's status |
| `csrf_invalid` | A request's CSRF header didn't match the session's token | Occasional: stale frontend tab. Frequent from one IP/user: investigate |
| `otp_locked` | A phone's OTP challenge hit its max wrong-attempt count | Expected for a player who mistyped repeatedly; frequent across many phones from one IP suggests abuse — cross-check the IP-scoped OTP rate limits |
| `unexpected_error` | An unhandled exception — full traceback logged server-side only | Client only ever sees a generic message + `request_id`; grep logs for that ID |

Every log line and every error response carries a `request_id` — use it to
correlate a specific user-reported issue with the exact server-side logs
for that request, across DB, storage, and realtime.
