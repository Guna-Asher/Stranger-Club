"""Phase 3 infrastructure tests: configuration fail-fast behaviour, storage
failure injection, rate-limit backend failure, readiness under DB failure,
and connection-pool exhaustion. A small, targeted set — not a chaos-engineering
framework — per the Phase 3 spec's own instruction.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.config import ConfigError, load_config
from backend.app.main import create_app
from backend.app.storage_fake import FakeStorage


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
