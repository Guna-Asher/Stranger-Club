#!/usr/bin/env python3
"""One-time SQLite -> PostgreSQL data migration.

This is a row-by-row COPY, not a schema transformation: the destination
PostgreSQL database must already be at the current Alembic head (run
`python -m backend.app.migrate` against it first — see docs/database.md).
Every table this script touches already has the identical shape on both
sides; this script only transports rows.

Source consistency strategy
----------------------------
Intended to run during an explicit maintenance window with the application
stopped (see docs/deployment.md's cutover runbook) — the SQLite file is
therefore static for the whole run, which is as strong a consistency
guarantee as SQLite's own MVCC would give under WAL mode. This script does
not attempt an online/zero-downtime migration; at current data sizes a
short maintenance window is the simplest safe design (see the Phase 3
architecture review for the reasoning).

Destination transaction strategy
---------------------------------
The entire copy runs inside ONE PostgreSQL transaction. Any failure —
including a validation failure at the end — rolls the whole thing back,
leaving the destination database exactly as it was before this script ran
(empty, if this is a fresh cutover). Nothing is left half-migrated.

Foreign-key ordering
---------------------
Tables are copied in an explicit order that respects every foreign key in
the schema (see TABLE_ORDER below) — never relying on the destination
temporarily disabling constraint checking.

Sequence reset
---------------
Primary keys are copied verbatim (never re-generated) so every foreign key
value copied afterward still resolves correctly. After each table's rows
are inserted, the table's auto-increment sequence is advanced past the
highest copied id, so the next INSERT the running application performs
gets a fresh, non-colliding id.

Memory
-------
Rows are streamed from SQLite in bounded batches (BATCH_SIZE), never
`.fetchall()`-ed as a whole table into memory — this script's memory
footprint does not grow with database size.

Explicitly NOT migrated
-------------------------
- rate_limit_buckets: transient rate-limiting counters, meaningless to
  carry across a cutover.
- alembic_version / schema_migrations: bookkeeping tables; the destination
  already has its own, correct state from having run migrations itself.

Usage
------
    python scripts/migrate_sqlite_to_postgres.py \\
        --sqlite-path data/stranger_club.db \\
        --postgres-url postgresql+psycopg://user:pass@host/db \\
        [--report-path migration_report.json] [--dry-run]

--dry-run performs the full copy and validation inside a transaction, then
rolls back instead of committing — use it to rehearse a cutover against a
disposable destination database before doing it for real.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import MetaData, Table, create_engine, func, select, text
from sqlalchemy.engine import Engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BATCH_SIZE = 500

# Explicit foreign-key-respecting order. Anything not listed here is not
# migrated by this script (see module docstring).
TABLE_ORDER = [
    "organizers",
    "users",
    "players",  # legacy, frozen — copied for completeness; registrations.player_id may reference it
    "player_profiles",
    "matches",
    "event_payment_configurations",
    "registrations",
    "payments",
    "payment_proofs",
    "sessions",  # organizer sessions
    "player_sessions",
    "otp_challenges",
    "audit_logs",
]

# Foreign keys checked during validation: (table, column, referenced_table, referenced_column, nullable)
FK_CHECKS = [
    ("player_profiles", "user_id", "users", "id", False),
    ("matches", "owner_organizer_id", "organizers", "id", True),
    ("event_payment_configurations", "match_id", "matches", "id", False),
    ("event_payment_configurations", "updated_by_organizer_id", "organizers", "id", True),
    ("registrations", "match_id", "matches", "id", False),
    ("registrations", "user_id", "users", "id", True),
    ("registrations", "player_id", "players", "id", True),
    ("payments", "registration_id", "registrations", "id", False),
    ("payment_proofs", "payment_id", "payments", "id", False),
    ("payment_proofs", "submitted_by_user_id", "users", "id", False),
    ("payment_proofs", "reviewed_by_organizer_id", "organizers", "id", True),
    ("sessions", "organizer_id", "organizers", "id", False),
    ("player_sessions", "user_id", "users", "id", False),
]

# Product invariants worth re-checking after copy, beyond raw FK integrity.
INVARIANT_QUERIES = {
    "duplicate_active_registrations": """
        SELECT match_id, user_id, COUNT(*) FROM registrations
        WHERE user_id IS NOT NULL AND status IN ('PENDING','WAITLISTED','CONFIRMED','REJECTED')
        GROUP BY match_id, user_id HAVING COUNT(*) > 1
    """,
    "multiple_pending_proofs_per_payment": """
        SELECT payment_id, COUNT(*) FROM payment_proofs
        WHERE status = 'PENDING' GROUP BY payment_id HAVING COUNT(*) > 1
    """,
    "payments_missing_configuration_per_event": """
        SELECT m.id FROM matches m
        LEFT JOIN event_payment_configurations c ON c.match_id = m.id
        WHERE c.id IS NULL
    """,
}


def _reflect(engine: Engine) -> MetaData:
    metadata = MetaData()
    metadata.reflect(bind=engine, only=lambda name, _: name in TABLE_ORDER)
    return metadata


def _row_count(engine: Engine, table: Table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def _copy_table(source_engine: Engine, dest_conn, table: Table) -> int:
    copied = 0
    with source_engine.connect() as source_conn:
        result = source_conn.execution_options(yield_per=BATCH_SIZE).execute(select(table))
        while True:
            batch = result.fetchmany(BATCH_SIZE)
            if not batch:
                break
            rows = [dict(row._mapping) for row in batch]
            dest_conn.execute(table.insert(), rows)
            copied += len(rows)
    return copied


def _reset_sequence(dest_conn, table_name: str) -> None:
    dest_conn.execute(text(
        "SELECT setval(pg_get_serial_sequence(:table, 'id'), "
        "COALESCE((SELECT MAX(id) FROM " + table_name + "), 1), "
        "(SELECT MAX(id) FROM " + table_name + ") IS NOT NULL)"
    ), {"table": table_name})


def _has_id_column(table: Table) -> bool:
    return "id" in table.c


def run(sqlite_path: Path, postgres_url: str, *, dry_run: bool) -> dict:
    source_engine = create_engine(f"sqlite:///{sqlite_path}")
    dest_engine = create_engine(postgres_url)

    source_metadata = _reflect(source_engine)
    report: dict = {"started_at": datetime.now(timezone.utc).isoformat(), "tables": {}, "validation": {}, "dry_run": dry_run}

    with dest_engine.begin() as dest_conn:
        dest_metadata = MetaData()
        dest_metadata.reflect(bind=dest_engine, only=lambda name, _: name in TABLE_ORDER)

        for table_name in TABLE_ORDER:
            if table_name not in source_metadata.tables:
                report["tables"][table_name] = {"source_rows": 0, "copied_rows": 0, "note": "not present in source"}
                continue
            source_table = source_metadata.tables[table_name]
            dest_table = dest_metadata.tables[table_name]

            existing = dest_conn.execute(select(func.count()).select_from(dest_table)).scalar_one()
            if existing:
                raise RuntimeError(
                    f"Destination table '{table_name}' already has {existing} row(s). "
                    "This script only targets an empty destination (a fresh cutover) — "
                    "refusing to risk duplicating or colliding with existing data."
                )

            source_rows = _row_count(source_engine, source_table)
            copied = _copy_table(source_engine, dest_conn, dest_table)
            if _has_id_column(dest_table):
                _reset_sequence(dest_conn, table_name)
            report["tables"][table_name] = {"source_rows": source_rows, "copied_rows": copied}
            if source_rows != copied:
                raise RuntimeError(f"Row count mismatch copying '{table_name}': source={source_rows} copied={copied}")

        # --- Validation, inside the same transaction, before commit ---
        for fk_table, fk_column, ref_table, ref_column, nullable in FK_CHECKS:
            if fk_table not in dest_metadata.tables or ref_table not in dest_metadata.tables:
                continue
            null_clause = f"{fk_table}.{fk_column} IS NOT NULL AND " if nullable else ""
            orphan_count = dest_conn.execute(text(f"""
                SELECT COUNT(*) FROM {fk_table}
                WHERE {null_clause} NOT EXISTS (
                    SELECT 1 FROM {ref_table} WHERE {ref_table}.{ref_column} = {fk_table}.{fk_column}
                )
            """)).scalar_one()
            key = f"fk:{fk_table}.{fk_column}->{ref_table}.{ref_column}"
            report["validation"][key] = {"orphan_count": orphan_count}
            if orphan_count:
                raise RuntimeError(f"Foreign key integrity violated after copy: {key} has {orphan_count} orphan row(s)")

        for name, query in INVARIANT_QUERIES.items():
            rows = dest_conn.execute(text(query)).fetchall()
            report["validation"][name] = {"violation_count": len(rows)}
            if rows:
                raise RuntimeError(f"Invariant '{name}' violated after copy: {len(rows)} offending row(s)")

        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["status"] = "validated"

        if dry_run:
            dest_conn.rollback()
            report["status"] = "dry_run_rolled_back"
        # else: the `with dest_engine.begin()` block commits on clean exit.

    source_engine.dispose()
    dest_engine.dispose()
    return report


def copy_local_objects_to_s3(local_uploads_dir: Path, storage_config) -> dict:
    """Companion step to the row copy above: PaymentProof/EventPaymentConfiguration
    rows reference storage keys, but the row copy never touches the
    underlying binary objects. If the source deployment used
    LocalFilesystemStorage (the dev/test backend), those bytes live under
    `local_uploads_dir/{proofs,qr}/<key>` and must be uploaded to the
    destination S3-compatible bucket under the SAME keys before the row
    copy's references resolve to anything real — DB metadata and object
    storage evidence must remain recoverable together (see
    docs/backups.md). Skips (does not overwrite) any key already present at
    the destination. Run scripts/reconcile_storage.py afterward to confirm
    every referenced key now resolves."""
    from backend.app.storage_s3 import S3Storage

    report = {"proofs": {"uploaded": 0, "skipped_existing": 0}, "qr": {"uploaded": 0, "skipped_existing": 0}}
    for prefix in ("proofs", "qr"):
        local_dir = local_uploads_dir / prefix
        if not local_dir.is_dir():
            continue
        storage = S3Storage(storage_config, prefix=prefix)
        content_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
        for path in local_dir.iterdir():
            if not path.is_file():
                continue
            if storage.exists(path.name):
                report[prefix]["skipped_existing"] += 1
                continue
            storage.put(path.name, path.read_bytes(), content_types.get(path.suffix.lower(), "application/octet-stream"))
            report[prefix]["uploaded"] += 1
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sqlite-path", required=True, type=Path)
    parser.add_argument("--postgres-url", required=True)
    parser.add_argument("--report-path", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--copy-objects-from", type=Path, default=None, metavar="UPLOADS_DIR",
        help="Local SC_DATA_DIR/uploads directory to also copy proof/QR binaries from into the destination "
             "S3 bucket (reads S3 config from SC_STORAGE_* env vars). Skipped entirely if omitted — run "
             "scripts/reconcile_storage.py afterward either way to confirm every referenced key resolves.",
    )
    args = parser.parse_args()

    if not args.sqlite_path.is_file():
        print(f"SQLite database not found: {args.sqlite_path}", file=sys.stderr)
        return 1

    try:
        report = run(args.sqlite_path, args.postgres_url, dry_run=args.dry_run)
        if args.copy_objects_from and not args.dry_run:
            from backend.app.config import load_config
            report["object_copy"] = copy_local_objects_to_s3(args.copy_objects_from, load_config().storage)
    except Exception as exc:
        print(f"MIGRATION FAILED, destination rolled back: {exc}", file=sys.stderr)
        return 1

    report_json = json.dumps(report, indent=2, default=str)
    print(report_json)
    if args.report_path:
        args.report_path.write_text(report_json)
    print(f"\nStatus: {report['status']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
