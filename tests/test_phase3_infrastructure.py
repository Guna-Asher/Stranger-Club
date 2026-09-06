"""Phase 3 infrastructure tests: configuration fail-fast behaviour, storage
failure injection, rate-limit backend failure, readiness under DB failure,
and connection-pool exhaustion. A small, targeted set — not a chaos-engineering
framework — per the Phase 3 spec's own instruction.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.config import ConfigError, load_config
from backend.app.main import create_app
from backend.app.storage_fake import FakeStorage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


# --- Configuration fail-fast (section 15 / S) --------------------------------

def test_production_requires_postgres_database_url(monkeypatch):
    monkeypatch.setenv("SC_ENV", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SC_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("SC_STORAGE_BUCKET", "b")
    monkeypatch.setenv("SC_STORAGE_ENDPOINT_URL", "https://example.test")
    monkeypatch.setenv("SC_STORAGE_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("SC_STORAGE_SECRET_ACCESS_KEY", "y")
    monkeypatch.setenv("SC_TRUSTED_PROXY_IPS", "10.0.0.1")
    with pytest.raises(ConfigError, match="PostgreSQL"):
        load_config()


def test_production_requires_s3_storage_backend(monkeypatch):
    monkeypatch.setenv("SC_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.delenv("SC_STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("SC_TRUSTED_PROXY_IPS", "10.0.0.1")
    with pytest.raises(ConfigError, match="SC_STORAGE_BACKEND=s3"):
        load_config()


def test_s3_backend_requires_storage_credentials(monkeypatch):
    monkeypatch.setenv("SC_ENV", "development")
    monkeypatch.setenv("SC_STORAGE_BACKEND", "s3")
    for var in ("SC_STORAGE_BUCKET", "SC_STORAGE_ENDPOINT_URL", "SC_STORAGE_ACCESS_KEY_ID", "SC_STORAGE_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ConfigError, match="SC_STORAGE_BUCKET"):
        load_config()


def test_production_requires_trusted_proxy_ips(monkeypatch):
    monkeypatch.setenv("SC_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("SC_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("SC_STORAGE_BUCKET", "b")
    monkeypatch.setenv("SC_STORAGE_ENDPOINT_URL", "https://example.test")
    monkeypatch.setenv("SC_STORAGE_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("SC_STORAGE_SECRET_ACCESS_KEY", "y")
    monkeypatch.delenv("SC_TRUSTED_PROXY_IPS", raising=False)
    with pytest.raises(ConfigError, match="SC_TRUSTED_PROXY_IPS"):
        load_config()


def test_staging_requires_trusted_proxy_ips(monkeypatch):
    """Staging is deployed the same way production is (real PostgreSQL, real
    S3 — see the two checks above) and is just as likely to sit behind a real
    reverse proxy, so it must get the same X-Forwarded-For-spoofing
    protection production does, not just a production-only check."""
    monkeypatch.setenv("SC_ENV", "staging")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("SC_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("SC_STORAGE_BUCKET", "b")
    monkeypatch.setenv("SC_STORAGE_ENDPOINT_URL", "https://example.test")
    monkeypatch.setenv("SC_STORAGE_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("SC_STORAGE_SECRET_ACCESS_KEY", "y")
    monkeypatch.delenv("SC_TRUSTED_PROXY_IPS", raising=False)
    with pytest.raises(ConfigError, match="SC_TRUSTED_PROXY_IPS"):
        load_config()


def test_invalid_sc_env_value_rejected(monkeypatch):
    monkeypatch.setenv("SC_ENV", "not-a-real-environment")
    with pytest.raises(ConfigError, match="SC_ENV"):
        load_config()


def test_development_defaults_work_with_no_configuration_at_all(monkeypatch):
    for var in ("SC_ENV", "DATABASE_URL", "SC_STORAGE_BACKEND", "SC_TRUSTED_PROXY_IPS", "SC_COOKIE_SECURE"):
        monkeypatch.delenv(var, raising=False)
    config = load_config()
    assert config.env == "development"
    assert config.is_sqlite
    assert config.storage.backend == "local"
    assert config.secure_cookies is False


def test_secure_cookies_default_on_in_non_development_environments(monkeypatch):
    monkeypatch.setenv("SC_ENV", "staging")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("SC_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("SC_STORAGE_BUCKET", "b")
    monkeypatch.setenv("SC_STORAGE_ENDPOINT_URL", "https://example.test")
    monkeypatch.setenv("SC_STORAGE_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("SC_STORAGE_SECRET_ACCESS_KEY", "y")
    monkeypatch.setenv("SC_TRUSTED_PROXY_IPS", "10.0.0.1")
    monkeypatch.delenv("SC_COOKIE_SECURE", raising=False)
    config = load_config()
    assert config.secure_cookies is True


# --- Storage failure injection (section 11 / I) ------------------------------

def test_payment_proof_upload_returns_clean_503_when_storage_unavailable(tmp_path: Path):
    proof_storage = FakeStorage()
    proof_storage.fail_on_put = True
    app = create_app(data_dir=tmp_path / "data", admin_password="x", proof_storage=proof_storage, qr_storage=FakeStorage())
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "organizer", "password": "x"})
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        event = client.post("/api/admin/events", headers=headers, json={
            "name": "Storage Failure Test", "date": "2026-10-02", "start_time": "07:00:00", "end_time": "10:00:00",
            "venue": "Ground", "capacity": 4, "fee": 100, "registration_deadline": "2026-10-01T20:00:00", "upi_id": "xtest@upi",
        }).json()

        client.post("/api/player/otp/request", json={"phone": "9812340001"})
        code = client.app.state.otp_provider.last_code_for("9812340001")
        verified = client.post("/api/player/otp/verify", json={"phone": "9812340001", "code": code})
        player_headers = {"X-CSRF-Token": verified.json()["csrf_token"]}
        reg = client.post(f"/api/events/{event['public_id']}/registrations", json={"name": "Player One", "preferred_position": "NO_PREFERENCE"}, headers=player_headers).json()

        from io import BytesIO
        from PIL import Image
        buf = BytesIO(); Image.new("RGB", (10, 10), "red").save(buf, format="PNG")
        response = client.post(
            f"/api/registrations/{reg['public_id']}/payment", headers=player_headers,
            files={"screenshot": ("p.png", buf.getvalue(), "image/png")},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "STORAGE_UNAVAILABLE"

        # No orphaned PaymentProof row was created for the failed upload.
        with client.app.state.session_factory() as session:
            from backend.app.models import PaymentProof
            assert session.query(PaymentProof).count() == 0
        assert proof_storage.put_calls == []


@pytest.mark.skipif("SC_TEST_S3_ENDPOINT_URL" not in os.environ, reason="requires a real S3-compatible endpoint (set SC_TEST_S3_ENDPOINT_URL etc.)")
def test_s3_storage_put_get_exists_presign_against_a_real_endpoint():
    """Runs against a real S3-compatible backend (MinIO in CI, or any
    S3/R2 endpoint locally) — proves the adapter isn't just correct against
    mocks. See docs/storage.md for the env vars this reads."""
    import uuid
    import urllib.request
    from backend.app.config import StorageConfig
    from backend.app.storage_s3 import S3Storage

    config = StorageConfig(
        backend="s3",
        bucket=os.environ["SC_TEST_S3_BUCKET"],
        endpoint_url=os.environ["SC_TEST_S3_ENDPOINT_URL"],
        region=os.environ.get("SC_TEST_S3_REGION", "us-east-1"),
        access_key_id=os.environ["SC_TEST_S3_ACCESS_KEY_ID"],
        secret_access_key=os.environ["SC_TEST_S3_SECRET_ACCESS_KEY"],
    )
    storage = S3Storage(config, prefix="proofs")
    key = f"{uuid.uuid4().hex}.png"

    assert storage.exists(key) is False
    assert storage.get(key) is None

    storage.put(key, b"integration-test-bytes", "image/png")
    assert storage.exists(key) is True
    content, content_type = storage.get(key)
    assert content == b"integration-test-bytes"
    assert content_type == "image/png"

    url = storage.presigned_url(key, expires_in=30)
    assert url is not None
    assert urllib.request.urlopen(url).read() == b"integration-test-bytes"

    missing_url = storage.presigned_url(f"{uuid.uuid4().hex}.png", expires_in=30)
    assert missing_url is None


def test_storage_never_silently_reports_success_without_a_real_object(tmp_path: Path):
    """The application must never declare a proof stored when the DB has no
    valid object reference — verified structurally: submit_payment_proof only
    ever creates a PaymentProof row AFTER persist_image() (storage.put)
    returns successfully, never before."""
    from backend.app import services
    import inspect
    source = inspect.getsource(services.submit_payment_proof)
    persist_index = source.index("persist_image(")
    proof_creation_index = source.index("PaymentProof(")
    assert persist_index < proof_creation_index


# --- Readiness under failure (section 21 / R) --------------------------------

def test_ready_returns_503_when_database_unreachable(tmp_path: Path):
    app = create_app(data_dir=tmp_path / "data", admin_password="x")
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        # Point the session factory at a nonexistent database file's directory
        # gone, simulating "database unreachable" without touching the
        # already-open connection pool of the real one.
        import sqlalchemy
        broken_engine = sqlalchemy.create_engine("sqlite:////nonexistent-directory/does-not-exist.db")
        client.app.state.session_factory = sqlalchemy.orm.sessionmaker(bind=broken_engine)
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "NOT_READY"


# --- Rate limiting fails closed (section 16 / L) -----------------------------

def test_login_rate_limit_fails_closed_when_backend_unavailable(tmp_path: Path, monkeypatch):
    app = create_app(data_dir=tmp_path / "data", admin_password="correct-horse")
    with TestClient(app) as client:
        import backend.app.routers.auth as auth_module

        def broken_peek(*args, **kwargs):
            from backend.app.rate_limit import RateLimitBackendError
            raise RateLimitBackendError("simulated backend outage")

        monkeypatch.setattr(auth_module, "peek_rate_limit_db", broken_peek)
        response = client.post("/api/auth/login", json={"username": "organizer", "password": "correct-horse"})
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "RATE_LIMITED"
        # No raw database error leaked to the client.
        assert "Traceback" not in response.text and "sqlalchemy" not in response.text.lower()


# --- Malformed environment configuration (section S / Y) --------------------

def test_missing_admin_password_fails_loudly_on_startup(tmp_path: Path):
    app = create_app(data_dir=tmp_path / "data", admin_password=None)
    with pytest.raises(RuntimeError, match="SC_ADMIN_PASSWORD"):
        with TestClient(app):
            pass


# --- Readiness detects a migration mismatch (section 21 / R) ----------------

@pytest.mark.skipif("SC_TEST_DATABASE_URL" not in os.environ, reason="requires a real PostgreSQL instance (set SC_TEST_DATABASE_URL)")
def test_ready_returns_503_when_alembic_head_does_not_match_expected(monkeypatch):
    from sqlalchemy import create_engine, text as sa_text

    database_url = os.environ["SC_TEST_DATABASE_URL"]
    reset_engine = create_engine(database_url)
    with reset_engine.begin() as conn:
        conn.execute(sa_text("DROP SCHEMA public CASCADE"))
        conn.execute(sa_text("CREATE SCHEMA public"))
    reset_engine.dispose()

    app = create_app(database_url=database_url, admin_password="x")
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200

        # Simulate "new code deployed before its migration ran": stamp the
        # database back to an earlier revision without actually reverting
        # the schema.
        engine = create_engine(database_url)
        with engine.begin() as conn:
            conn.execute(sa_text("UPDATE alembic_version SET version_num = '0002'"))
        engine.dispose()

        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "NOT_READY"


@pytest.mark.skipif("SC_TEST_DATABASE_URL" not in os.environ, reason="requires a real PostgreSQL instance (set SC_TEST_DATABASE_URL)")
def test_restore_scripts_alembic_check_detects_a_real_mismatch():
    """scripts/restore_test.py's validate_alembic_head() must actually
    detect a stale/incomplete restore, not just always report a match."""
    from sqlalchemy import create_engine, text as sa_text
    from restore_test import validate_alembic_head

    database_url = os.environ["SC_TEST_DATABASE_URL"]
    reset_engine = create_engine(database_url)
    with reset_engine.begin() as conn:
        conn.execute(sa_text("DROP SCHEMA public CASCADE"))
        conn.execute(sa_text("CREATE SCHEMA public"))
    reset_engine.dispose()

    app = create_app(database_url=database_url, admin_password="x")
    with TestClient(app):
        pass  # runs migrations to head via the normal startup path

    good = validate_alembic_head(database_url)
    assert good["match"] is True

    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(sa_text("UPDATE alembic_version SET version_num = '0002'"))
    engine.dispose()

    bad = validate_alembic_head(database_url)
    assert bad["match"] is False
    assert bad["actual"] == "0002"


@pytest.mark.skipif("SC_TEST_DATABASE_URL" not in os.environ, reason="requires a real PostgreSQL instance (set SC_TEST_DATABASE_URL)")
def test_connection_pool_exhaustion_fails_fast_rather_than_hanging(monkeypatch):
    """Holds every pooled connection open, then confirms the next request
    fails within pool_timeout rather than hanging indefinitely."""
    import time
    from sqlalchemy import create_engine, text as sa_text

    database_url = os.environ["SC_TEST_DATABASE_URL"]
    engine = create_engine(database_url, pool_size=2, max_overflow=0, pool_timeout=2)
    held = []
    try:
        for _ in range(2):
            conn = engine.connect()
            conn.execute(sa_text("SELECT 1"))
            held.append(conn)

        started = time.monotonic()
        with pytest.raises(Exception):
            extra = engine.connect()
            extra.close()
        elapsed = time.monotonic() - started
        assert elapsed < 5, f"pool_timeout=2 should fail fast, took {elapsed:.1f}s"
    finally:
        for conn in held:
            conn.close()
        engine.dispose()


# --- Bucket versioning / overwrite-and-delete recovery (Phase 3 hardening, section A) ---

@pytest.mark.skipif("SC_TEST_S3_ENDPOINT_URL" not in os.environ, reason="requires a real S3-compatible endpoint (set SC_TEST_S3_ENDPOINT_URL etc.)")
def test_bucket_versioning_can_be_enabled_and_is_verified_by_readback():
    """scripts/configure_bucket_protection.py must never claim success
    without reading the status back from the provider — this exercises the
    real enable+verify round trip against a real endpoint (MinIO in CI)."""
    from configure_bucket_protection import enable_versioning, get_versioning_status

    client = _test_s3_admin_client()
    bucket = os.environ["SC_TEST_S3_BUCKET"]

    result = enable_versioning(client, bucket)
    assert result["outcome"] == "VERIFIED_ENABLED", (
        f"This endpoint did not confirm versioning enabled: {result}. If this is a genuine "
        f"provider limitation (not a bug), this test documents that limitation rather than "
        f"hiding it — do not weaken this assertion to force a pass."
    )
    assert get_versioning_status(client, bucket) == "Enabled"


@pytest.mark.skipif("SC_TEST_S3_ENDPOINT_URL" not in os.environ, reason="requires a real S3-compatible endpoint (set SC_TEST_S3_ENDPOINT_URL etc.)")
def test_versioned_bucket_recovers_overwritten_and_deleted_objects():
    """The actual protection versioning buys: even after an overwrite and a
    delete, every prior version's bytes remain fetchable by VersionId. This
    is what makes "the runtime credential can't delete proofs" a
    belt-and-suspenders guarantee rather than the only line of defence."""
    import uuid
    from configure_bucket_protection import enable_versioning

    client = _test_s3_admin_client()
    bucket = os.environ["SC_TEST_S3_BUCKET"]
    enable_versioning(client, bucket)  # idempotent if already enabled

    key = f"proofs/{uuid.uuid4().hex}.png"
    client.put_object(Bucket=bucket, Key=key, Body=b"original-evidence")
    client.put_object(Bucket=bucket, Key=key, Body=b"overwritten-bytes")
    client.delete_object(Bucket=bucket, Key=key)

    # Normal GET now reports not-found, exactly as the application's own
    # Storage.get() would see it.
    from botocore.exceptions import ClientError
    with pytest.raises(ClientError):
        client.get_object(Bucket=bucket, Key=key)

    # But both real versions are still recoverable by an operator with the
    # admin credential.
    versions = client.list_object_versions(Bucket=bucket, Prefix=key)
    bodies = {
        client.get_object(Bucket=bucket, Key=key, VersionId=v["VersionId"])["Body"].read()
        for v in versions.get("Versions", [])
    }
    assert bodies == {b"original-evidence", b"overwritten-bytes"}
    assert len(versions.get("DeleteMarkers", [])) == 1


def _test_s3_admin_client():
    import boto3
    return boto3.client(
        "s3", endpoint_url=os.environ["SC_TEST_S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["SC_TEST_S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["SC_TEST_S3_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("SC_TEST_S3_REGION", "us-east-1"),
    )


# --- Database least-privilege roles (Phase 3 hardening, section B) ----------
#
# Requires two role-scoped connection strings against a real PostgreSQL
# instance where the two roles (matching scripts/provision_database_roles.sql)
# already exist — these are NOT the same as SC_TEST_DATABASE_URL (which
# connects as whatever role owns the test database, typically superuser-ish
# in a disposable test container). Skipped entirely otherwise: this proves
# real role boundaries against real PostgreSQL, never fabricated.
#
# Re-grants privileges on every test (autouse fixture below) rather than
# assuming they were set up once and left alone: other tests in this suite
# legitimately DROP SCHEMA public CASCADE between runs (see the `client`
# fixture's _reset_postgres_schema), which also wipes any grants made to
# these roles on the schema's previous incarnation — this class must not
# silently start reporting false "permission denied" results (for the wrong
# reason: the schema/tables not being visible at all) just because it ran
# after another test reset the schema.

@pytest.mark.skipif(
    "SC_TEST_APP_ROLE_URL" not in os.environ or "SC_TEST_BACKUP_ROLE_URL" not in os.environ or "SC_TEST_DATABASE_URL" not in os.environ,
    reason="requires SC_TEST_APP_ROLE_URL, SC_TEST_BACKUP_ROLE_URL, and SC_TEST_DATABASE_URL (used to (re-)apply grants) all pointing at the same real PostgreSQL instance",
)
class TestDatabaseLeastPrivilege:
    @pytest.fixture(autouse=True)
    def _regrant_privileges(self):
        from sqlalchemy import create_engine, text as sa_text
        from sqlalchemy.engine import make_url

        app_role = make_url(os.environ["SC_TEST_APP_ROLE_URL"]).username
        backup_role = make_url(os.environ["SC_TEST_BACKUP_ROLE_URL"]).username
        engine = create_engine(os.environ["SC_TEST_DATABASE_URL"])
        with engine.begin() as conn:
            conn.execute(sa_text(f'GRANT USAGE ON SCHEMA public TO "{app_role}"'))
            conn.execute(sa_text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{app_role}"'))
            conn.execute(sa_text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{app_role}"'))
            conn.execute(sa_text(f'GRANT USAGE ON SCHEMA public TO "{backup_role}"'))
            conn.execute(sa_text(f'GRANT pg_read_all_data TO "{backup_role}"'))
        engine.dispose()
        yield
    def test_app_role_cannot_perform_ddl(self):
        from sqlalchemy import create_engine, text as sa_text
        from sqlalchemy.exc import DBAPIError

        engine = create_engine(os.environ["SC_TEST_APP_ROLE_URL"])
        try:
            with engine.connect() as conn:
                with pytest.raises(DBAPIError, match="permission denied"):
                    conn.execute(sa_text("CREATE TABLE sc_privilege_test_should_never_exist (id int)"))
        finally:
            engine.dispose()

    def test_app_role_can_perform_dml(self):
        from sqlalchemy import create_engine, text as sa_text

        engine = create_engine(os.environ["SC_TEST_APP_ROLE_URL"])
        try:
            with engine.connect() as conn:
                # Any existing table works for this check; organizers always
                # exists post-migration.
                result = conn.execute(sa_text("SELECT COUNT(*) FROM organizers"))
                assert result.scalar_one() >= 0
        finally:
            engine.dispose()

    def test_backup_role_can_read_but_not_write_or_ddl(self):
        from sqlalchemy import create_engine, text as sa_text
        from sqlalchemy.exc import DBAPIError

        engine = create_engine(os.environ["SC_TEST_BACKUP_ROLE_URL"])
        try:
            with engine.connect() as conn:
                result = conn.execute(sa_text("SELECT COUNT(*) FROM organizers"))
                assert result.scalar_one() >= 0

            with engine.connect() as conn:
                with pytest.raises(DBAPIError, match="permission denied"):
                    conn.execute(sa_text(
                        "INSERT INTO organizers (username, password_hash, role, is_active) "
                        "VALUES ('sc_privilege_test_intruder', 'x', 'ORGANIZER', true)"
                    ))

            with engine.connect() as conn:
                with pytest.raises(DBAPIError):
                    conn.execute(sa_text("DROP TABLE organizers"))
        finally:
            engine.dispose()


# --- Reverse-proxy trust configuration (Phase 3 hardening, section F) -------
#
# The actual production load balancer/proxy cannot be live-tested in this
# environment — that part is deployment-time verification only (see
# deployment.md). What CAN be tested here, and is: that
# docker-entrypoint.sh actually translates SC_TRUSTED_PROXY_IPS into the
# uvicorn flag that makes X-Forwarded-* trust conditional on it, and that it
# does NOT add that flag (i.e. trusts nothing) when the variable is unset —
# a fake `uvicorn` on PATH captures exactly what args the real one would
# have received, without starting a real server.

def _run_entrypoint_and_capture_uvicorn_args(tmp_path: Path, env: dict) -> list[str]:
    import stat
    import subprocess

    captured = tmp_path / "captured_args.txt"
    fake_uvicorn = tmp_path / "uvicorn"
    fake_uvicorn.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{captured}"\n')
    fake_uvicorn.chmod(fake_uvicorn.stat().st_mode | stat.S_IEXEC)

    entrypoint = Path(__file__).resolve().parents[1] / "docker-entrypoint.sh"
    run_env = {**os.environ, **env, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    subprocess.run(["sh", str(entrypoint)], env=run_env, check=True, capture_output=True, text=True)
    return captured.read_text().splitlines()


def test_entrypoint_trusts_no_forwarded_headers_when_proxy_ips_unset(tmp_path: Path):
    args = _run_entrypoint_and_capture_uvicorn_args(tmp_path, {"SC_TRUSTED_PROXY_IPS": ""})
    assert "--forwarded-allow-ips" not in " ".join(args)
    assert "--proxy-headers" not in args


def test_entrypoint_trusts_forwarded_headers_only_from_configured_proxy(tmp_path: Path):
    args = _run_entrypoint_and_capture_uvicorn_args(tmp_path, {"SC_TRUSTED_PROXY_IPS": "10.0.0.5"})
    joined = " ".join(args)
    assert "--proxy-headers" in args
    assert "--forwarded-allow-ips=10.0.0.5" in joined


def test_entrypoint_passes_through_multiple_trusted_proxy_cidrs(tmp_path: Path):
    args = _run_entrypoint_and_capture_uvicorn_args(tmp_path, {"SC_TRUSTED_PROXY_IPS": "10.0.0.5,172.16.0.0/12"})
    joined = " ".join(args)
    assert "--forwarded-allow-ips=10.0.0.5,172.16.0.0/12" in joined
