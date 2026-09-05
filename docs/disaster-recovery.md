# Disaster Recovery

Each scenario: **Detection → Containment → Recovery → Verification.**
Written to be executable by another engineer, not just understandable by
whoever wrote it — every command referenced actually exists in this repo.
Not every scenario's *full sequence* has been run end-to-end against real
production infrastructure (none exists in this environment); each
scenario below states plainly which parts were actually exercised here
(**VERIFIED LOCALLY**) versus which are a documented, code-grounded
procedure not yet run as a drill (**NOT YET DRILLED**) versus which
require infrastructure this environment doesn't have
(**DEPLOYMENT-TIME VERIFICATION**).

## A. Application instance failure

- **Detection**: load balancer health check against `/health` fails.
- **Containment**: LB stops routing to the failed instance automatically.
- **Recovery**: the platform restarts/replaces the instance. No data to
  recover — instances are stateless (sessions, rate limits, and realtime
  fan-out all live in PostgreSQL).
- **Verification**: `curl https://.../health` and `/ready` both return
  200 from the replacement instance. **NOT YET DRILLED** as a full scenario
  (no load balancer exists in this environment to actually fail an
  instance behind) — `/health` and `/ready` themselves are exercised by
  every test run (**VERIFIED LOCALLY**).

## B. All instances restart

- **Detection**: expected during a deploy, or a platform-wide event.
- **Containment**: none needed.
- **Recovery**: each instance re-establishes its DB connection pool and
  (if `PostgresBroadcaster` is active) its LISTEN connection on boot; no
  state is lost.
- **Verification**: `/ready` green on every instance; open the app and
  confirm a live SSE connection receives the `summary` event on connect.
  **NOT YET DRILLED** as a literal "restart every instance" exercise — the
  underlying mechanisms (pool re-establishment, LISTEN reconnect) are each
  individually **VERIFIED LOCALLY** via the automated realtime tests.

## C. PostgreSQL restart

- **Detection**: connection errors spike in logs; `pool_pre_ping` converts
  stale pooled connections into clean retries rather than opaque failures.
- **Containment**: none needed for a normal, brief restart.
- **Recovery**: automatic — the pool reconnects, and `PostgresBroadcaster`'s
  listener thread detects the dropped connection, reconnects with backoff,
  re-`LISTEN`s every tracked topic, and pushes a `RESYNC` event to every
  locally-connected SSE client so nothing is silently stale.
- **Verification**: `/ready` returns 200; check logs for
  `realtime_listener_connected` after the restart. **VERIFIED LOCALLY**:
  the listener reconnect-and-RESYNC behavior this depends on was directly
  exercised while building `PostgresBroadcaster` (killing and
  re-establishing the LISTEN connection); a literal `docker restart` of
  the production Postgres instance mid-traffic has not been drilled here.

## D. PostgreSQL data restoration required

- **Detection**: data corruption or loss identified (manual report or
  alert).
- **Containment**: stop write traffic (maintenance mode, or scale
  application instances to zero) before touching the database.
- **Recovery**: follow `backups.md`'s restore procedure —
  `scripts/restore_test.py` against the target (a fresh instance if this is
  a "restore into a new database" scenario, or the provider's own PITR
  restore flow if recovering the same instance to a point in time) — then
  `scripts/reconcile_storage.py` to confirm every referenced proof/QR
  object still resolves.
- **Verification**: the restore script's own schema/FK/invariant/
  Alembic-head/application-level checks all pass; reconciliation reports
  no `missing_from_storage`; resume traffic only after both are clean.
  **VERIFIED LOCALLY**: this exact sequence (`pg_dump` -> fresh instance ->
  `pg_restore` -> `scripts/restore_test.py` -> `scripts/reconcile_storage.py`)
  was run for real against disposable local infrastructure this session.

## E. Object storage temporarily unavailable

- **Detection**: `storage_put_failed`/`storage_get_failed`/
  `storage_presign_failed` log lines (with correlation IDs) from
  `storage_s3.py`; clients see `503 STORAGE_UNAVAILABLE` on
  upload/retrieval endpoints specifically.
- **Containment**: none needed — the rest of the application (browsing
  events, registering, logging in) keeps working, since `/ready` does not
  hard-depend on storage (a deliberate choice to avoid an unnecessary
  cascading outage).
- **Recovery**: automatic once the provider restores service; bounded
  retries in `S3Storage` already absorb brief blips.
- **Verification**: manually retry a proof upload/retrieval; confirm the
  `503` rate in logs returns to zero. **VERIFIED LOCALLY**: a simulated
  storage failure (via `FakeStorage.fail_on_put`) is covered by an
  automated test confirming the clean `503` and that no orphaned DB row is
  created.

## F. Payment-proof object deleted or missing

- **Detection**: `scripts/reconcile_storage.py` reports a non-empty
  `missing_from_storage` list, or an organizer/player reports a 404 on a
  proof they previously saw.
- **Containment**: investigate how it happened — the runtime credential has
  no delete permission on `proofs/` by design (see `storage.md`), so this
  should be structurally impossible via normal application behaviour;
  treat an occurrence as a credential-scoping or provider-incident
  investigation, not routine cleanup.
- **Recovery**: no automatic recovery of the bytes unless bucket versioning
  was enabled (see `storage.md`) — if so, restore the prior version. If
  not, the evidence is genuinely gone; document this plainly to the
  affected organizer/player rather than fabricating a replacement.
- **Verification**: re-run `scripts/reconcile_storage.py` and confirm the
  key resolves again (if recovered) or is explicitly documented as
  permanently lost (if not); review and tighten the IAM policy that made
  this possible. **VERIFIED LOCALLY**: the recovery path itself (an
  overwritten-then-deleted object's prior versions remaining fetchable by
  `VersionId`) was proven against a real MinIO bucket with versioning
  enabled — see `storage.md`.

## G. A deployment introduces a bad migration

- **Detection**: the migration test matrix (CI) should catch this before
  merge. In production, the migration step (`python -m backend.app.migrate`)
  itself fails, or `/ready`'s `alembic_version` check flags a mismatch
  after a partial apply.
- **Containment**: do not start new application instances against the
  partially-migrated database; the release pipeline should gate instance
  rollout on the migration step's exit code.
- **Recovery**: PostgreSQL DDL is transactional — a failed migration
  transaction rolls back completely (a real advantage over SQLite's
  documented non-transactional-DDL caveat), so the database is left exactly
  as it was before the attempt. Fix the migration, re-run
  `python -m backend.app.migrate`.
- **Verification**: `python -m backend.app.migrate` exits 0; `/ready`
  returns 200; re-run the migration test matrix against a copy of
  production data before retrying in production if the failure was
  data-dependent. **VERIFIED LOCALLY**: a deliberately broken migration
  (a valid `ADD COLUMN` followed by a statement referencing a nonexistent
  table) was run against a real PostgreSQL 16 instance in this session —
  both the column addition and the `alembic_version` bump were rolled back
  completely; the database was left exactly as it was before the attempt.

## H. Credentials must be rotated

- **Detection**: scheduled rotation, or suspected compromise.
- **Containment**: none needed if rotating proactively; if compromise is
  suspected, treat the old credential as burned immediately.
- **Recovery**: provision the new credential (DB role password, storage API
  token) alongside the old one; update the relevant environment variable;
  roll application instances one at a time (zero-downtime with 2+
  instances) so each picks up the new credential; only then revoke the old
  one.
- **Verification**: confirm new instances connect successfully with the new
  credential **before** revoking the old one; confirm the revoked
  credential no longer authenticates afterward. **NOT YET DRILLED** — no
  actual credential rotation has been performed in this environment; the
  underlying role/credential model this procedure rotates is
  **VERIFIED LOCALLY** (see `database.md`'s least-privilege tests), but the
  rotation *procedure itself* is documented, not rehearsed.
