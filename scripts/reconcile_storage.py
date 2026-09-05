#!/usr/bin/env python3
"""Object storage reconciliation — an exceptional, manually-run operational
tool. NEVER invoked by application request-handling code.

Payment-proof evidence is append-only: the application's runtime storage
credential has no delete permission over the proofs/ prefix (see
docs/storage.md). This means two things can legitimately drift out of sync
over time, both harmlessly:

  1. Orphaned objects: a proof upload that lost a concurrency race, or a
     retried request whose duplicate write was recognised and not
     referenced (see backend/app/services.py submit_payment_proof) — an
     object exists in storage with no PaymentProof.storage_key pointing to
     it.
  2. Missing objects (the serious direction): a PaymentProof row references
     a storage_key that no longer exists in the bucket — e.g. an operator
     error, a provider incident, or (should never happen given the IAM
     policy) an unauthorized deletion.

This script only REPORTS both directions. It never deletes anything itself
— deleting a candidate orphan requires a human to look at the report and
decide, using a separate, deliberately more-privileged credential from the
one the running application holds (see --confirm-delete below, which reads
that privileged credential from its own distinct environment variables,
never from the application's SC_STORAGE_* configuration).

Usage
------
    python scripts/reconcile_storage.py --database-url ... [--json-report path]

    # After reviewing a report and confirming specific keys are safe to
    # remove (e.g. confirmed orphans older than N days with no matching
    # row), delete them explicitly and individually:
    python scripts/reconcile_storage.py --database-url ... \\
        --confirm-delete proofs/<key1>.png proofs/<key2>.png
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import boto3
from sqlalchemy import create_engine, text


def _admin_s3_client():
    """Deliberately reads a SEPARATE set of environment variables from the
    application's own SC_STORAGE_* configuration — this script's delete
    capability must never be reachable via the credential the running
    application holds."""
    access_key = os.environ["SC_STORAGE_ADMIN_ACCESS_KEY_ID"]
    secret_key = os.environ["SC_STORAGE_ADMIN_SECRET_ACCESS_KEY"]
    endpoint_url = os.environ["SC_STORAGE_ENDPOINT_URL"]
    region = os.environ.get("SC_STORAGE_REGION", "auto")
    return boto3.client(
        "s3", endpoint_url=endpoint_url, region_name=region,
        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
    )


def _list_bucket_keys(client, bucket: str, prefix: str) -> set[str]:
    keys = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"][len(prefix):] if obj["Key"].startswith(prefix) else obj["Key"])
    return keys


def _referenced_proof_keys(database_url: str) -> set[str]:
    engine = create_engine(database_url)
    with engine.connect() as conn:
        keys = {row[0] for row in conn.execute(text("SELECT storage_key FROM payment_proofs"))}
    engine.dispose()
    return keys


def _referenced_qr_keys(database_url: str) -> set[str]:
    engine = create_engine(database_url)
    with engine.connect() as conn:
        current = {row[0] for row in conn.execute(text(
            "SELECT qr_storage_key FROM event_payment_configurations WHERE qr_storage_key IS NOT NULL"
        ))}
        historical = {row[0] for row in conn.execute(text(
            "SELECT qr_storage_key_snapshot FROM payments WHERE qr_storage_key_snapshot IS NOT NULL"
        ))}
    engine.dispose()
    return current | historical


def reconcile(database_url: str, bucket: str, *, s3_client=None) -> dict:
    client = s3_client or _admin_s3_client()
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "bucket": bucket}

    stored_proof_keys = _list_bucket_keys(client, bucket, "proofs/")
    referenced_proof_keys = _referenced_proof_keys(database_url)
    report["proofs"] = {
        "stored_count": len(stored_proof_keys),
        "referenced_count": len(referenced_proof_keys),
        "orphaned_in_storage": sorted(stored_proof_keys - referenced_proof_keys),
        "missing_from_storage": sorted(referenced_proof_keys - stored_proof_keys),
    }

    stored_qr_keys = _list_bucket_keys(client, bucket, "qr/")
    referenced_qr_keys = _referenced_qr_keys(database_url)
    report["qr"] = {
        "stored_count": len(stored_qr_keys),
        "referenced_count": len(referenced_qr_keys),
        "orphaned_in_storage": sorted(stored_qr_keys - referenced_qr_keys),
        "missing_from_storage": sorted(referenced_qr_keys - stored_qr_keys),
    }

    if report["proofs"]["missing_from_storage"] or report["qr"]["missing_from_storage"]:
        report["ALERT"] = (
            "One or more PaymentProof/EventPaymentConfiguration rows reference an object that does not "
            "exist in storage. This means financial evidence is unavailable — investigate immediately "
            "(see docs/disaster-recovery.md, scenario F)."
        )
    return report


def confirm_delete(bucket: str, keys: list[str], *, s3_client=None) -> None:
    client = s3_client or _admin_s3_client()
    for key in keys:
        client.delete_object(Bucket=bucket, Key=key)
        print(f"deleted: {key}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--bucket", default=os.environ.get("SC_STORAGE_BUCKET"))
    parser.add_argument("--json-report", default=None)
    parser.add_argument("--confirm-delete", nargs="+", default=None, metavar="KEY")
    args = parser.parse_args()

    if not args.bucket:
        print("Provide --bucket or set SC_STORAGE_BUCKET", file=sys.stderr)
        return 1

    if args.confirm_delete:
        confirm_delete(args.bucket, args.confirm_delete)
        return 0

    report = reconcile(args.database_url, args.bucket)
    report_json = json.dumps(report, indent=2)
    print(report_json)
    if args.json_report:
        with open(args.json_report, "w") as handle:
            handle.write(report_json)
    return 1 if "ALERT" in report else 0


if __name__ == "__main__":
    sys.exit(main())
