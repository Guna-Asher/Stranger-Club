#!/usr/bin/env python3
"""Provisioning-time bucket protection: enable and verify S3 object
versioning for the payment-proof/QR bucket.

This is an ADMINISTRATIVE, one-time-per-bucket provisioning action — never
run by the application at request time, and never using the application's
own runtime storage credential (SC_STORAGE_*). It reads the same
privileged, separate credential as scripts/reconcile_storage.py
(SC_STORAGE_ADMIN_ACCESS_KEY_ID / SC_STORAGE_ADMIN_SECRET_ACCESS_KEY),
because changing bucket-level configuration is exactly the kind of
administrative action that credential is scoped for and the runtime
application credential should not be able to do.

What this buys you, and what it doesn't
-----------------------------------------
Versioning protects against an accidental overwrite (a PUT to an existing
key — should never happen given this application's opaque, uuid4-generated
keys, but a real safety net against a bug) and gives a recovery path for a
DELETE performed with a credential that should not have had delete
permission in the first place (see docs/storage.md — the runtime credential
is IAM-scoped to have no s3:DeleteObject on proofs/, so this is defence in
depth, not the primary control).

It does NOT replace the IAM policy restricting delete — a credential with
DeleteObjectVersion permission can still permanently remove a version.
It does NOT replace off-site pg_dump / restore testing (docs/backups.md) —
this only protects object bytes, not the database rows that make them
meaningful.

Provider support is NOT assumed
---------------------------------
Not every S3-compatible provider implements the versioning API the same
way, and capabilities change over time — this script never assumes R2 (or
any provider) supports it; it calls PutBucketVersioning and then
GET-verifies the actual reported status, and reports plainly if the
provider rejects the call (NotImplemented / MethodNotAllowed / similar) or
if the reported status doesn't match what was requested. Do not read
"versioning configured in code" as "versioning is protecting production" —
run this against the real bucket and read its output before believing it.

Usage
------
    python scripts/configure_bucket_protection.py --bucket <bucket> --enable
    python scripts/configure_bucket_protection.py --bucket <bucket>   # status check only
"""
from __future__ import annotations

import argparse
import os
import sys

import boto3
from botocore.exceptions import ClientError


def _admin_s3_client():
    access_key = os.environ["SC_STORAGE_ADMIN_ACCESS_KEY_ID"]
    secret_key = os.environ["SC_STORAGE_ADMIN_SECRET_ACCESS_KEY"]
    endpoint_url = os.environ["SC_STORAGE_ENDPOINT_URL"]
    region = os.environ.get("SC_STORAGE_REGION", "auto")
    return boto3.client(
        "s3", endpoint_url=endpoint_url, region_name=region,
        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
    )


def get_versioning_status(client, bucket: str) -> str:
    """Returns the bucket's actual reported versioning status: 'Enabled',
    'Suspended', or 'NotConfigured' (the API's own representation of
    "never turned on"). Never inferred — always the provider's own answer."""
    response = client.get_bucket_versioning(Bucket=bucket)
    return response.get("Status", "NotConfigured")


def enable_versioning(client, bucket: str) -> dict:
    """Attempts to enable versioning, then reads the status back to confirm
    the provider actually applied it. Returns a result dict describing what
    genuinely happened — never claims success without the read-back
    confirming it."""
    result = {"bucket": bucket, "requested": "Enabled"}
    try:
        client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        result["outcome"] = "PROVIDER_REJECTED"
        result["error_code"] = code
        result["note"] = (
            f"The provider rejected PutBucketVersioning ({code}). This provider/endpoint may not "
            "support S3 bucket versioning, or the credential lacks permission to change bucket "
            "configuration. Check the provider's current documentation — do not assume versioning "
            "is protecting this bucket."
        )
        return result

    try:
        confirmed_status = get_versioning_status(client, bucket)
    except ClientError as exc:
        result["outcome"] = "PUT_SUCCEEDED_BUT_VERIFY_FAILED"
        result["error_code"] = exc.response.get("Error", {}).get("Code", "Unknown")
        return result

    result["confirmed_status"] = confirmed_status
    result["outcome"] = "VERIFIED_ENABLED" if confirmed_status == "Enabled" else "VERIFY_MISMATCH"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--enable", action="store_true", help="Attempt to enable versioning (default: status check only)")
    args = parser.parse_args()

    client = _admin_s3_client()

    if args.enable:
        result = enable_versioning(client, args.bucket)
        print(result)
        if result["outcome"] != "VERIFIED_ENABLED":
            print(
                "\nVersioning is NOT confirmed enabled. This is expected if the provider/endpoint "
                "doesn't support it (e.g. some MinIO deployments without erasure-coded backends, "
                "or an S3-compatible provider that hasn't implemented this API) — in that case, "
                "document the limitation and rely on the IAM delete-restriction + reconciliation "
                "script as the primary protections instead.",
                file=sys.stderr,
            )
            return 1
        print("\nVersioning VERIFIED enabled (read back from the provider, not assumed).")
        return 0

    try:
        status = get_versioning_status(client, args.bucket)
    except ClientError as exc:
        print(f"Could not read versioning status: {exc.response.get('Error', {}).get('Code', 'Unknown')}", file=sys.stderr)
        return 1
    print(f"Bucket '{args.bucket}' versioning status: {status}")
    return 0 if status == "Enabled" else 1


if __name__ == "__main__":
    sys.exit(main())
