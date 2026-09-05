#!/usr/bin/env python3
"""Restore-test runbook, automated.

A backup is not considered valid until restoration has been tested. This
script performs the full documented procedure (docs/backups.md) against a
disposable target database and reports a measured RTO — never claim the
RPO/RTO targets in docs/backups.md are met without running this and looking
at the actual numbers it prints.

    backup file (pg_dump custom format)
        -> pg_restore into an EMPTY target PostgreSQL database
        -> schema/FK/invariant validation (reuses the same checks the
           SQLite->PostgreSQL migration script validates with)
        -> application-level verification (start the app against the
           restored database, hit /ready and a handful of real endpoints)
        -> for object storage: verify every referenced PaymentProof/QR
           storage_key still resolves (delegates to reconcile_storage.py)

Refuses to run against a target database that already has tables in it —
this is a restore-into-a-fresh-instance test, never a merge into existing
data.

Usage
------
    python scripts/restore_test.py \\
        --backup-file /path/to/backup.dump \\
        --target-url postgresql+psycopg://user:pass@scratch-host/db \\
        [--storage-bucket ... --admin-access-key-id ... --admin-secret-access-key ... --storage-endpoint-url ...]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parent))
from migrate_sqlite_to_postgres import FK_CHECKS, INVARIANT_QUERIES  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _libpq_url(sqlalchemy_url: str) -> str:
    return make_url(sqlalchemy_url).set(drivername="postgresql").render_as_string(hide_password=False)


def restore_backup(backup_file: Path, target_url: str) -> float:
    if shutil.which("pg_restore") is None:
        raise RuntimeError("pg_restore is not on PATH — install the postgresql-client tools to run this script.")
    engine = create_engine(target_url)
    existing_tables = inspect(engine).get_table_names()
    if existing_tables:
        engine.dispose()
        raise RuntimeError(
            f"Target database already has {len(existing_tables)} table(s): {existing_tables}. "
            "This script only restores into an EMPTY database — point it at a fresh scratch instance."
        )
    engine.dispose()

    started = time.monotonic()
    subprocess.run(
        ["pg_restore", "--no-owner", "--no-privileges", "--dbname", _libpq_url(target_url), str(backup_file)],
        check=True, capture_output=True, text=True,
    )
    return time.monotonic() - started


def validate_alembic_head(target_url: str) -> dict:
    """Explicit, standalone check — deliberately not folded into the
    application-level check below, because create_app() defaults to
    auto-migrating (SC_AUTO_MIGRATE=true), which would silently apply a
    missing migration to the restored database and mask exactly the
    mismatch this is supposed to catch."""
    from backend.app.main import ALEMBIC_EXPECTED_HEAD

    engine = create_engine(target_url)
    with engine.connect() as conn:
        if "alembic_version" not in inspect(engine).get_table_names():
            engine.dispose()
            return {"expected": ALEMBIC_EXPECTED_HEAD, "actual": None, "match": False}
        actual = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    engine.dispose()
    return {"expected": ALEMBIC_EXPECTED_HEAD, "actual": actual, "match": actual == ALEMBIC_EXPECTED_HEAD}


def validate_restored_schema(target_url: str) -> dict:
    engine = create_engine(target_url)
    results = {}
    with engine.connect() as conn:
        for fk_table, fk_column, ref_table, ref_column, nullable in FK_CHECKS:
            if fk_table not in inspect(engine).get_table_names():
                continue
            null_clause = f"{fk_table}.{fk_column} IS NOT NULL AND " if nullable else ""
            orphan_count = conn.execute(text(f"""
                SELECT COUNT(*) FROM {fk_table}
                WHERE {null_clause} NOT EXISTS (
                    SELECT 1 FROM {ref_table} WHERE {ref_table}.{ref_column} = {fk_table}.{fk_column}
                )
            """)).scalar_one()
            results[f"fk:{fk_table}.{fk_column}->{ref_table}.{ref_column}"] = orphan_count
        for name, query in INVARIANT_QUERIES.items():
            results[name] = len(conn.execute(text(query)).fetchall())

        results["row_counts"] = {}
        for table in ("organizers", "users", "matches", "registrations", "payments", "payment_proofs", "audit_logs"):
            if table in inspect(engine).get_table_names():
                results["row_counts"][table] = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
    engine.dispose()
    return results


def validate_application_level(target_url: str) -> dict:
    from backend.app.main import create_app
    from fastapi.testclient import TestClient

    # Force auto-migrate off for this check specifically: the point of
    # validate_alembic_head() (and of this /ready check) is to confirm the
    # RESTORE itself left the database at the correct migration state — if
    # create_app() were allowed to auto-migrate here, a genuinely missing
    # migration would get silently applied before /ready ever saw it,
    # masking exactly the failure this script exists to catch.
    previous = os.environ.get("SC_AUTO_MIGRATE")
    os.environ["SC_AUTO_MIGRATE"] = "false"
    try:
        app = create_app(database_url=target_url, admin_username="__restore_test_probe__", admin_password="restore-test-probe-password")
        results = {}
        with TestClient(app) as client:
            results["ready"] = client.get("/ready").status_code
            results["public_events"] = client.get("/api/events").status_code
        return results
    finally:
        if previous is None:
            os.environ.pop("SC_AUTO_MIGRATE", None)
        else:
            os.environ["SC_AUTO_MIGRATE"] = previous


def validate_storage(database_url: str, bucket: str | None, storage_kwargs: dict) -> dict | None:
    if not bucket:
        return None
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from reconcile_storage import reconcile
    import boto3
    client = boto3.client("s3", **storage_kwargs)
    return reconcile(database_url, bucket, s3_client=client)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backup-file", required=True, type=Path)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--storage-bucket", default=None)
    parser.add_argument("--storage-endpoint-url", default=None)
    parser.add_argument("--admin-access-key-id", default=None)
    parser.add_argument("--admin-secret-access-key", default=None)
    args = parser.parse_args()

    if not args.backup_file.is_file():
        print(f"Backup file not found: {args.backup_file}", file=sys.stderr)
        return 1

    print(f"Restoring {args.backup_file} into {args.target_url} ...")
    try:
        restore_seconds = restore_backup(args.backup_file, args.target_url)
    except subprocess.CalledProcessError as exc:
        print(f"pg_restore FAILED: {exc.stderr}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"pg_restore completed in {restore_seconds:.1f}s")

    alembic_result = validate_alembic_head(args.target_url)
    print("Alembic head check:", alembic_result)
    if not alembic_result["match"]:
        print(
            f"ALEMBIC HEAD MISMATCH: restored database is at {alembic_result['actual']!r}, "
            f"code expects {alembic_result['expected']!r}. The backup is either stale relative to "
            f"the deployed code, or the restore is incomplete.",
            file=sys.stderr,
        )
        return 1

    schema_results = validate_restored_schema(args.target_url)
    violations = {k: v for k, v in schema_results.items() if k != "row_counts" and v}
    print("Row counts:", schema_results["row_counts"])
    if violations:
        print(f"SCHEMA VALIDATION FAILED: {violations}", file=sys.stderr)
        return 1
    print("Schema/FK/invariant validation: PASSED")

    app_results = validate_application_level(args.target_url)
    print("Application-level checks:", app_results)
    if app_results["ready"] != 200:
        print("APPLICATION-LEVEL VERIFICATION FAILED: /ready did not return 200", file=sys.stderr)
        return 1

    if args.storage_bucket:
        storage_kwargs = {
            "endpoint_url": args.storage_endpoint_url,
            "aws_access_key_id": args.admin_access_key_id,
            "aws_secret_access_key": args.admin_secret_access_key,
        }
        storage_results = validate_storage(args.target_url, args.storage_bucket, storage_kwargs)
        print("Storage reconciliation:", storage_results)
        if storage_results and "ALERT" in storage_results:
            print("STORAGE VALIDATION FAILED: referenced proof objects are missing", file=sys.stderr)
            return 1

    print(f"\nRESTORE TEST PASSED. Measured restore time (pg_restore only): {restore_seconds:.1f}s")
    print("Record this alongside the RTO target in docs/backups.md — do not claim the target without this number.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
