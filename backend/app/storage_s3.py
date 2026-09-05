from __future__ import annotations

import logging

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from .config import StorageConfig
from .storage import StorageUnavailableError, _guard_key

logger = logging.getLogger("stranger_club.storage")

# Bounded, observable retry — never infinite. Transient network errors and
# throttling only; validation failures never reach this layer at all (they
# fail in services.py before any storage call).
_BOTO_CONFIG = BotoConfig(
    connect_timeout=5,
    read_timeout=10,
    retries={"max_attempts": 2, "mode": "standard"},
)


class S3Storage:
    """Production object storage: any S3-compatible provider (Cloudflare R2
    by default, AWS S3, or MinIO for local integration tests) via the
    generic S3 API — the application is never tied to a specific vendor.

    Deliberately has no delete method — matches the Storage protocol exactly
    (see storage.py). The runtime credential backing this client should
    itself have no s3:DeleteObject permission on the payment-proof prefix;
    this class not exposing delete is defence in depth on top of that IAM
    boundary, not a substitute for it (see docs/storage.md for the exact
    policy).
    """

    def __init__(self, config: StorageConfig, *, prefix: str):
        if config.backend != "s3":
            raise ValueError("S3Storage requires a StorageConfig with backend='s3'")
        self._bucket = config.bucket
        self._prefix = prefix.rstrip("/") + "/"
        self._client = boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            region_name=config.region,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            config=_BOTO_CONFIG,
        )

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}{_guard_key(key)}"

    def put(self, key: str, data: bytes, content_type: str) -> None:
        try:
            self._client.put_object(Bucket=self._bucket, Key=self._full_key(key), Body=data, ContentType=content_type)
        except (ClientError, BotoCoreError) as exc:
            logger.error("storage_put_failed key=%s error=%s", key, _safe_error(exc))
            raise StorageUnavailableError("Could not store the uploaded file") from exc

    def get(self, key: str) -> tuple[bytes, str] | None:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._full_key(key))
        except self._client.exceptions.NoSuchKey:
            return None
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            logger.error("storage_get_failed key=%s error=%s", key, _safe_error(exc))
            raise StorageUnavailableError("Could not retrieve the stored file") from exc
        except BotoCoreError as exc:
            logger.error("storage_get_failed key=%s error=%s", key, _safe_error(exc))
            raise StorageUnavailableError("Could not retrieve the stored file") from exc
        return response["Body"].read(), response.get("ContentType", "application/octet-stream")

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._full_key(key))
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
                return False
            logger.error("storage_exists_check_failed key=%s error=%s", key, _safe_error(exc))
            raise StorageUnavailableError("Could not check the stored file") from exc
        except BotoCoreError as exc:
            logger.error("storage_exists_check_failed key=%s error=%s", key, _safe_error(exc))
            raise StorageUnavailableError("Could not check the stored file") from exc

    def presigned_url(self, key: str, *, expires_in: int) -> str | None:
        if not self.exists(key):
            return None
        try:
            return self._client.generate_presigned_url(
                "get_object", Params={"Bucket": self._bucket, "Key": self._full_key(key)}, ExpiresIn=expires_in,
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error("storage_presign_failed key=%s error=%s", key, _safe_error(exc))
            raise StorageUnavailableError("Could not authorize file access") from exc


def _safe_error(exc: Exception) -> str:
    """Never let a boto3 error string leak the endpoint/credentials into
    logs — botocore error messages don't include the secret key itself, but
    strip anything that looks like a query string just in case a presigned
    URL ever ends up embedded in an exception message."""
    text = str(exc)
    return text.split("?")[0]
