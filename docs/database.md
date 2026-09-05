# Database

## Connection string

`DATABASE_URL`, SQLAlchemy-style:

- Development (default if unset): `sqlite:///data/stranger_club.db`
- Production/staging (required): `postgresql+psycopg://<user>:<password>@<host>/<database>`

`SC_ENV=production` or `staging` refuses to start with a non-PostgreSQL
`DATABASE_URL` (see `backend/app/config.py`).

## Connection pooling

```python
create_engine(
    database_url,
    pool_size=SC_DB_POOL_SIZE,      # default 5
    max_overflow=SC_DB_MAX_OVERFLOW,  # default 5
    pool_timeout=10,     # fail fast rather than queue indefinitely
    pool_recycle=1800,   # survive a managed provider silently dropping idle connections
    pool_pre_ping=True,  # turn a dead pooled connection into a clean retry, not a mystery error
    connect_args={"options": "-c statement_timeout=15000"},  # no single query holds a lock forever
)
```

### Sizing formula

```
max_connections_needed = app_instances × workers_per_instance × (pool_size + max_overflow)
```

Budget **80** connections out of Postgres's default `max_connections` (~100
on most small managed tiers), reserving headroom for the migration user,
manual `psql` access, and monitoring.

At **2 instances × 2 uvicorn workers** with the defaults above:
`2 × 2 × 10 = 40` ≤ 80 — comfortable headroom to scale to 4 instances before
needing to revisit this. Recompute this formula (and adjust
`SC_DB_POOL_SIZE`/`SC_DB_MAX_OVERFLOW`) before scaling past that.

PgBouncer is deliberately not introduced yet — only add it once the
connection budget above is genuinely tight.

## Database credentials (least privilege)

Do not run the application as a PostgreSQL superuser. Provision two roles:

```sql
-- Run once by a superuser/admin during provisioning.
CREATE ROLE stranger_club_migrator LOGIN PASSWORD '...';
CREATE ROLE stranger_club_app LOGIN PASSWORD '...';

CREATE DATABASE stranger_club OWNER stranger_club_migrator;

\c stranger_club
GRANT USAGE, CREATE ON SCHEMA public TO stranger_club_migrator;
GRANT USAGE ON SCHEMA public TO stranger_club_app;

-- After the migrator has run `alembic upgrade head` at least once:
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO stranger_club_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO stranger_club_app;
ALTER DEFAULT PRIVILEGES FOR ROLE stranger_club_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO stranger_club_app;
ALTER DEFAULT PRIVILEGES FOR ROLE stranger_club_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO stranger_club_app;
```

- `stranger_club_migrator`: DDL rights, owns the schema. Used **only** by
  `python -m backend.app.migrate` (the release-phase step). Never embedded
  in the running application's `DATABASE_URL`.
- `stranger_club_app`: DML only (`SELECT`/`INSERT`/`UPDATE`/`DELETE`), no
  `CREATE`/`DROP`/`ALTER`. This is the credential the running application
  uses.

## Migrations

Alembic is the sole authoritative migration system from its baseline
forward. `backend/app/migrations.py` remains frozen, historical
SQLite-only bootstrap logic — PostgreSQL never runs it (see
`0000_postgres_bootstrap.py`, which builds a fresh PostgreSQL schema
directly from the current models, and note that `render_as_batch` in
`alembic/env.py` is SQLite-only, so PostgreSQL migrations always run plain
`ALTER TABLE`, never a table-recreate).

**Multi-instance safety**: migrations are an explicit deployment step —
`python -m backend.app.migrate` — run once, before new application
instances start. `SC_AUTO_MIGRATE` (default `true`) lets `make_session_factory`
also run migrations inline for local dev/single-instance convenience; set
it to `false` in any multi-instance production deployment. Every
PostgreSQL migration run — inline or via the CLI — is guarded by a
session-scoped `pg_advisory_lock`, so two callers racing to migrate
serialize instead of corrupting each other's work; the lock releases
automatically if the holding connection drops.

`/ready` compares the database's `alembic_version` against
`ALEMBIC_EXPECTED_HEAD` in `main.py` and fails readiness on a mismatch —
bump that constant when you add a migration.

## Row locking (SQLite vs PostgreSQL)

Every capacity/state-sensitive mutation (registration creation, payment
submission/review, cancellation, promotion) uses
`services.begin_serialized_write()` + `services.lock_row()`:

- **SQLite**: `BEGIN IMMEDIATE` — a whole-database write lock (unchanged
  from pre-Phase-3 behaviour).
- **PostgreSQL**: a bare `SELECT id ... FOR UPDATE` on the single contended
  row (`Match`, `Payment`, or `Registration`, depending on the operation) —
  strictly finer-grained than SQLite's whole-database lock. Deliberately a
  separate, join-free statement: PostgreSQL rejects `FOR UPDATE` combined
  with the outer joins `joinedload()` produces for collection
  relationships, so the row is locked first, then the full object graph is
  loaded in a plain second query within the same transaction.

## Fresh-database bootstrap

A brand new PostgreSQL database is fully created by running
`alembic upgrade head` — nothing else. `0000_postgres_bootstrap.py` builds
the entire schema from the current models (`Base.metadata.create_all`) as
one reviewable, versioned migration step; every revision after it is a
guarded no-op against a database already at that shape. `create_all()` is
never called anywhere else for PostgreSQL — it is not used as a substitute
for the migration system.

## Schema-level invariants

Every important business invariant is enforced by the database itself, not
only application code:

| Invariant | Mechanism |
|---|---|
| One active registration per (event, player) | Partial unique index `uq_registration_active_per_user`, scoped to non-`CANCELLED` statuses |
| One pending proof per payment | Partial unique index `uq_payment_proof_one_pending`, scoped to `status='PENDING'` |
| One payment configuration per event | Unique constraint on `event_payment_configurations.match_id` |
| Valid state values | `CHECK` constraints on every status column |
| Referential integrity | Foreign keys on every relationship |
| QR source/key consistency | `CHECK` constraint tying `qr_source` to `qr_storage_key` nullability |

## JSONB

`audit_logs.metadata_json/before_json/after_json` use `JSONB` on
PostgreSQL (indexable/queryable) and plain `JSON` on SQLite — see
`models.JSONVariant` and `0006_jsonb_audit_columns.py`.
