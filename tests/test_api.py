from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text

from backend.app.deps import (
    OTP_REQUEST_IP_LIMIT, OTP_REQUEST_PHONE_LIMIT, OTP_VERIFY_IP_LIMIT,
    PAYMENT_UPLOAD_RATE_LIMIT, REGISTRATION_RATE_LIMIT,
)
from backend.app.main import create_app
from backend.app.models import OrganizerSession, PlayerSession, Registration, now_ist
from backend.app.services import review_payment
from backend.app.services_player import OTP_MAX_ATTEMPTS

# Set SC_TEST_DATABASE_URL to run this entire suite against PostgreSQL
# instead of SQLite, e.g.:
#   SC_TEST_DATABASE_URL=postgresql+psycopg://user:pass@host/db pytest tests/test_api.py
# Deliberately a *different* env var from DATABASE_URL (which backend.app.main
# reads for its own module-level `app = create_app()` on import) so running
# the Postgres suite never accidentally points that unrelated instance at
# a real database too.
TEST_DATABASE_URL = os.getenv("SC_TEST_DATABASE_URL")


def _reset_postgres_schema(database_url: str) -> None:
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()


@pytest.fixture()
def client(tmp_path: Path):
    if TEST_DATABASE_URL:
        _reset_postgres_schema(TEST_DATABASE_URL)
        with TestClient(create_app(database_url=TEST_DATABASE_URL, admin_password="correct-horse")) as test_client:
            yield test_client
    else:
        with TestClient(create_app(data_dir=tmp_path / "data", admin_password="correct-horse")) as test_client:
            yield test_client


def image_file():
    image = Image.new("RGB", (12, 12), "green")
    content = BytesIO(); image.save(content, format="PNG")
    return {"screenshot": ("proof.png", content.getvalue(), "image/png")}


def different_image_file():
    """A different screenshot from image_file() — different hash — for tests
    that need to distinguish a genuine resubmission from an identical retry."""
    image = Image.new("RGB", (12, 12), "blue")
    content = BytesIO(); image.save(content, format="PNG")
    return {"screenshot": ("proof2.png", content.getvalue(), "image/png")}


def login(client: TestClient) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"username": "organizer", "password": "correct-horse"})
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def player_login(client: TestClient, phone: str = "9876543210") -> dict[str, str]:
    requested = client.post("/api/player/otp/request", json={"phone": phone})
    assert requested.status_code == 200, requested.json()
    code = client.app.state.otp_provider.last_code_for(phone)
    verified = client.post("/api/player/otp/verify", json={"phone": phone, "code": code})
    assert verified.status_code == 200, verified.json()
    return {"X-CSRF-Token": verified.json()["csrf_token"]}


def create_second_organizer(client: TestClient, username: str = "second_organizer", password: str = "correct-horse-2") -> dict[str, str]:
    from argon2 import PasswordHasher
    from backend.app.models import Organizer
    with client.app.state.session_factory() as session:
        session.add(Organizer(username=username, password_hash=PasswordHasher().hash(password)))
        session.commit()
    other_client = TestClient(client.app)
    response = other_client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.json()
    return other_client, {"X-CSRF-Token": response.json()["csrf_token"]}


def make_platform_admin(client: TestClient, username: str = "organizer") -> None:
    from backend.app.models import Organizer
    with client.app.state.session_factory() as session:
        organizer = session.query(Organizer).filter_by(username=username).one()
        organizer.role = "PLATFORM_ADMIN"
        session.commit()


def new_event(client: TestClient, headers: dict[str, str], *, capacity: int = 4, name: str = "Friday Cricket") -> dict:
    response = client.post("/api/admin/events", headers=headers, json={
        "name": name, "date": "2026-10-02", "start_time": "07:00:00", "end_time": "10:00:00",
        "venue": "PlayArena, Bellandur", "capacity": capacity, "fee": 350,
        "registration_deadline": "2026-10-01T20:00:00", "upi_id": "strangerclub@upi",
    })
    assert response.status_code == 201, response.json()
    return response.json()


def register(client: TestClient, public_id: str, headers: dict[str, str], position: str = "NO_PREFERENCE", email: str | None = "rahul@example.com"):
    payload = {"name": "Rahul Kumar", "preferred_position": position}
    if email is not None:
        payload["email"] = email
    return client.post(f"/api/events/{public_id}/registrations", json=payload, headers=headers)


def submit_payment(client: TestClient, registration: dict, headers: dict[str, str]):
    return client.post(f"/api/registrations/{registration['public_id']}/payment", files=image_file(), headers=headers)


def test_health_public_event_and_migration(client: TestClient):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}
    assert client.get("/api/events/sunday-cricket-2026").status_code == 200
    with client.app.state.session_factory() as session:
        # schema_migrations is bookkeeping for the frozen, SQLite-only
        # pre-Alembic bootstrap (backend/app/migrations.py) — PostgreSQL
        # never had pre-Alembic history and never runs that code path at
        # all (see database.py / 0000_postgres_bootstrap.py).
        if session.bind.dialect.name == "sqlite":
            versions = set(session.execute(text("SELECT version FROM schema_migrations")).scalars())
            assert {"20260818_domain_foundation", "20260818_payment_state_cleanup", "20260906_player_identity"}.issubset(versions)
        else:
            current_head = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert current_head == "0008"


def test_legacy_database_is_upgraded_without_losing_event_or_payment(tmp_path: Path):
    data_dir = tmp_path / "legacy"; data_dir.mkdir(); database = data_dir / "stranger_club.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE matches (id INTEGER PRIMARY KEY, public_id VARCHAR(64) UNIQUE, name VARCHAR(120), date DATE,
                start_time TIME, end_time TIME, venue VARCHAR(200), capacity INTEGER, fee INTEGER,
                registration_deadline DATETIME, upi_id VARCHAR(120), qr_code_path VARCHAR(255), status VARCHAR(32),
                created_at DATETIME, updated_at DATETIME);
            CREATE TABLE registrations (id INTEGER PRIMARY KEY, match_id INTEGER, name VARCHAR(120), phone VARCHAR(16),
                email VARCHAR(254), status VARCHAR(32), created_at DATETIME, updated_at DATETIME,
                CONSTRAINT uq_registration_match_phone UNIQUE(match_id, phone));
            CREATE TABLE payments (id INTEGER PRIMARY KEY, registration_id INTEGER UNIQUE, amount INTEGER,
                screenshot_path VARCHAR(255), screenshot_token VARCHAR(64) UNIQUE, status VARCHAR(32), submitted_at DATETIME,
                verified_at DATETIME, rejection_reason TEXT);
            INSERT INTO matches VALUES (1, 'legacy-event', 'Legacy Cricket', '2026-10-02', '07:00:00', '10:00:00',
                'Legacy Ground', 22, 300, '2026-10-01 20:00:00', 'legacy@upi', NULL, 'ACTIVE', '2026-08-01', '2026-08-01');
            INSERT INTO registrations VALUES (1, 1, 'Legacy Player', '9999999999', 'legacy@example.com', 'PAYMENT_SUBMITTED', '2026-08-01', '2026-08-01');
            INSERT INTO payments VALUES (1, 1, 300, 'missing.png', 'legacy-token', 'PAYMENT_SUBMITTED', '2026-08-01', NULL, NULL);
        """)
    with TestClient(create_app(data_dir=data_dir, admin_password="correct-horse")) as legacy_client:
        event = legacy_client.get("/api/events/legacy-event").json()
        assert event["status"] == "OPEN"
        assert event["payment_submitted_count"] == 1

        with legacy_client.app.state.session_factory() as session:
            row = session.get(Registration, 1)
            public_id = row.public_id
            assert row.status == "PENDING"
            assert row.user_id is None  # migrated row: no authenticated owner
            from backend.app.models import Match, Organizer
            match = session.get(Match, 1)
            organizer = session.query(Organizer).one()
            assert match.owner_organizer_id == organizer.id  # unambiguous backfill (exactly one organizer)

        # No session at all: the old "public_id is enough" path must be gone.
        assert legacy_client.get(f"/api/registrations/{public_id}").status_code == 401

        # Even a real, freshly authenticated player cannot claim a legacy,
        # ownerless registration just by knowing its public_id.
        headers = player_login(legacy_client, "9000000001")
        cross = legacy_client.get(f"/api/registrations/{public_id}", headers=headers)
        assert cross.status_code == 404

        # The organizer created by this app's own bootstrap CAN manage the
        # backfilled legacy event, since ownership resolved unambiguously.
        org_headers = login(legacy_client)
        assert legacy_client.get("/api/admin/events/1", headers=org_headers).status_code == 200


def test_authentication_csrf_logout_and_expiration(client: TestClient):
    assert client.get("/api/admin/events").status_code == 401
    assert client.post("/api/auth/login", json={"username": "organizer", "password": "wrong"}).json()["error"]["message"] == "Invalid username or password."
    headers = login(client)
    assert client.get("/api/auth/me").json()["organizer"] == {"username": "organizer", "role": "ORGANIZER"}
    assert client.post("/api/admin/events", json={}).status_code == 403
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/admin/events").status_code == 401
    headers = login(client)
    with client.app.state.session_factory() as session:
        stored = session.query(OrganizerSession).one(); stored.expires_at = now_ist(); session.commit()
    assert client.get("/api/admin/events").status_code == 401


def test_event_lifecycle_and_multi_event_isolation(client: TestClient):
    headers = login(client)
    first = new_event(client, headers, name="Friday Cricket")
    second = new_event(client, headers, name="Sunday Cricket Two")
    events = client.get("/api/admin/events").json()
    assert {first["id"], second["id"]}.issubset({item["id"] for item in events})
    assert client.patch(f"/api/admin/events/{first['id']}", headers=headers, json={"status": "COMPLETED"}).status_code == 409
    assert client.patch(f"/api/admin/events/{first['id']}", headers=headers, json={"status": "FULL"}).status_code == 200
    assert client.patch(f"/api/admin/events/{first['id']}", headers=headers, json={"status": "OPEN"}).status_code == 200
    player_headers = player_login(client)
    assert register(client, first["public_id"], player_headers).status_code == 201
    summary_a = client.get(f"/api/events/{first['public_id']}/summary").json()
    summary_b = client.get(f"/api/events/{second['public_id']}/summary").json()
    assert summary_a["pending"] == 1
    assert summary_b["pending"] == 0


def test_registration_position_duplicate_and_realtime_event(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    channel = client.app.state.broadcaster.subscribe(event["public_id"])
    player_headers = player_login(client)
    response = register(client, event["public_id"], player_headers, position="ALL_ROUNDER")
    assert response.status_code == 201
    registration = response.json()
    assert registration["status"] == "PENDING"
    assert registration["preferred_position"] == "ALL_ROUNDER"
    # get(timeout=...), not get_nowait(): the PostgreSQL-backed broadcaster
    # delivers asynchronously (DB commit -> best-effort NOTIFY -> background
    # listener thread -> local queue), unlike the in-process one, which
    # delivers synchronously. Realtime is a freshness optimisation, not a
    # correctness guarantee, so a short wait here is the correct way to
    # observe it regardless of which backend is active.
    realtime = channel.get(timeout=5)
    assert realtime["type"] == "REGISTRATION_CREATED"
    assert realtime["summary"]["pending"] == 1
    duplicate = register(client, event["public_id"], player_headers, position="BOWLER")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "REGISTRATION_EXISTS"


def test_registration_owner_is_the_authenticated_player(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9876500055")
    registration = register(client, event["public_id"], player_headers).json()
    with client.app.state.session_factory() as session:
        row = session.get(Registration, registration["id"])
        user_id = session.scalar(text("SELECT id FROM users WHERE phone = :phone"), {"phone": "9876500055"})
        assert row.user_id == user_id
        assert row.phone == "9876500055"


@pytest.mark.parametrize(
    ("email_field", "expect_success", "expected_email"),
    [
        pytest.param({"email": ""}, True, None, id="empty-string-treated-as-omitted"),
        pytest.param({}, True, None, id="omitted-entirely"),
        pytest.param({"email": "player@example.com"}, True, "player@example.com", id="valid-email"),
        pytest.param({"email": "not-an-email"}, False, None, id="invalid-email-still-rejected"),
    ],
)
def test_registration_email_is_optional_but_still_validated(client: TestClient, email_field, expect_success, expected_email):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9876500056")
    payload = {"name": "Optional Email Player", "preferred_position": "NO_PREFERENCE", **email_field}
    response = client.post(f"/api/events/{event['public_id']}/registrations", json=payload, headers=player_headers)
    if expect_success:
        assert response.status_code == 201, response.json()
        assert response.json()["email"] == expected_email
    else:
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_payment_lifecycle_summary_and_idempotency(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client)
    registration = register(client, event["public_id"], player_headers).json()
    uploaded = submit_payment(client, registration, player_headers)
    assert uploaded.status_code == 200
    assert uploaded.json()["payment"]["status"] == "SUBMITTED"
    # Resubmitting the byte-identical screenshot while still pending is an
    # idempotent retry (e.g. a client that timed out and retried the upload),
    # not a conflict — and must not create a second proof row.
    retry = submit_payment(client, registration, player_headers)
    assert retry.status_code == 200
    assert retry.json()["payment"]["status"] == "SUBMITTED"
    pending = client.get("/api/admin/payments/pending").json()
    assert len(pending) == 1
    assert pending[0]["payment"]["proof_count"] == 1
    payment_id = pending[0]["payment"]["id"]
    confirmed = client.post(f"/api/admin/payments/{payment_id}/confirm", headers=headers)
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "CONFIRMED"
    assert confirmed.json()["payment"]["status"] == "VERIFIED"
    assert client.post(f"/api/admin/payments/{payment_id}/confirm", headers=headers).status_code == 409
    # VERIFIED is terminal: opening the UPI app or anything else on the
    # player's side can never move it, and no further proof is accepted.
    assert submit_payment(client, registration, player_headers).status_code == 409
    summary = client.get(f"/api/events/{event['public_id']}/summary").json()
    assert summary == {"event_id": event["public_id"], "capacity": 4, "confirmed": 1, "pending": 0, "payment_submitted": 0, "waitlisted": 0, "available": 3, "collected_amount": 350}


def test_payment_resubmission_before_review_supersedes_previous_proof(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client)
    registration = register(client, event["public_id"], player_headers).json()
    first = submit_payment(client, registration, player_headers)
    assert first.status_code == 200
    # A different screenshot, uploaded before the organizer has reviewed
    # anything: this is the player realising they picked the wrong file, not
    # a retry — it must be accepted and supersede the first proof rather than
    # being rejected as "already in review".
    second = client.post(
        f"/api/registrations/{registration['public_id']}/payment",
        files=different_image_file(), headers=player_headers,
    )
    assert second.status_code == 200
    assert second.json()["payment"]["status"] == "SUBMITTED"
    pending = client.get("/api/admin/payments/pending").json()
    assert len(pending) == 1
    assert pending[0]["payment"]["proof_count"] == 2  # both proofs kept, nothing deleted
    with client.app.state.session_factory() as session:
        from backend.app.models import PaymentProof as ProofModel
        proofs = session.query(ProofModel).filter_by(payment_id=pending[0]["payment"]["id"]).order_by(ProofModel.uploaded_at).all()
        assert [p.status for p in proofs] == ["SUPERSEDED", "PENDING"]


def test_payment_rejection(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client)
    registration = register(client, event["public_id"], player_headers).json(); submit_payment(client, registration, player_headers)
    payment_id = client.get("/api/admin/payments/pending").json()[0]["payment"]["id"]
    rejected = client.post(f"/api/admin/payments/{payment_id}/reject", headers=headers, json={"reason": "Reference number is not visible"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert rejected.json()["payment"]["status"] == "REJECTED"


def test_capacity_and_fifo_waitlist(client: TestClient):
    headers = login(client); event = new_event(client, headers, capacity=2)
    for phone in ("9876543201", "9876543202"):
        player_headers = player_login(client, phone)
        player = register(client, event["public_id"], player_headers).json(); submit_payment(client, player, player_headers)
        payment_id = client.get("/api/admin/payments/pending").json()[0]["payment"]["id"]
        assert client.post(f"/api/admin/payments/{payment_id}/confirm", headers=headers).status_code == 200
    second = register(client, event["public_id"], player_login(client, "9876543203")).json()
    third = register(client, event["public_id"], player_login(client, "9876543204")).json()
    assert second["status"] == third["status"] == "WAITLISTED"
    assert client.post(f"/api/admin/registrations/{third['id']}/promote", headers=headers).json()["error"]["code"] == "WAITLIST_ORDER"
    assert client.post(f"/api/admin/registrations/{second['id']}/promote", headers=headers).status_code == 409
    summary = client.get(f"/api/events/{event['public_id']}/summary").json()
    assert summary["confirmed"] == 2 and summary["waitlisted"] == 2 and summary["available"] == 0


def test_concurrent_payment_confirmation_cannot_exceed_capacity(client: TestClient):
    headers = login(client); event = new_event(client, headers, capacity=2)
    # Take one confirmed spot, then put two independent proofs in review.
    first_headers = player_login(client, "9876543211")
    first = register(client, event["public_id"], first_headers).json(); submit_payment(client, first, first_headers)
    first_payment = client.get("/api/admin/payments/pending").json()[0]["payment"]["id"]
    client.post(f"/api/admin/payments/{first_payment}/confirm", headers=headers)
    for phone in ("9876543212", "9876543213"):
        player_headers = player_login(client, phone)
        player = register(client, event["public_id"], player_headers).json(); submit_payment(client, player, player_headers)
    pending_ids = [row["payment"]["id"] for row in client.get("/api/admin/payments/pending").json()]

    def confirm(payment_id: int):
        with client.app.state.session_factory() as session:
            return review_payment(session, payment_id, approve=True, actor_id=1).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, pending_ids))
    summary = client.get(f"/api/events/{event['public_id']}/summary").json()
    assert sorted(results) == ["CONFIRMED", "WAITLISTED"]
    assert summary["confirmed"] == 2 and summary["available"] == 0


def test_admin_proof_is_not_public(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client)
    registration = register(client, event["public_id"], player_headers).json(); submit_payment(client, registration, player_headers)
    proof = client.get("/api/admin/payments/pending").json()[0]["payment"]["screenshot_url"]
    client.post("/api/auth/logout", headers=headers)
    assert client.get(proof).status_code == 401


def test_numeric_registration_id_no_longer_resolves(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client)
    registration = register(client, event["public_id"], player_headers).json()
    assert client.get(f"/api/registrations/{registration['id']}", headers=player_headers).status_code == 404
    numeric_lookup = submit_payment(client, {"public_id": str(registration["id"])}, player_headers)
    assert numeric_lookup.status_code == 404


def test_registration_rate_limit(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client)
    for _ in range(REGISTRATION_RATE_LIMIT):
        register(client, event["public_id"], player_headers)
    limited = register(client, event["public_id"], player_headers)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


def test_payment_upload_rate_limit(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client)
    registration = register(client, event["public_id"], player_headers).json()
    for _ in range(PAYMENT_UPLOAD_RATE_LIMIT):
        submit_payment(client, registration, player_headers)
    limited = submit_payment(client, registration, player_headers)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


# --- Player authentication -------------------------------------------------

def test_otp_request_and_verify_happy_path_establishes_session(client: TestClient):
    phone = "9812300001"
    requested = client.post("/api/player/otp/request", json={"phone": phone})
    assert requested.status_code == 200
    assert requested.json() == {"status": "sent"}
    code = client.app.state.otp_provider.last_code_for(phone)
    assert code is not None and len(code) == 6

    verified = client.post("/api/player/otp/verify", json={"phone": phone, "code": code})
    assert verified.status_code == 200
    assert verified.json()["phone"] == phone
    assert "sc_player_session" in client.cookies

    me = client.get("/api/player/me")
    assert me.status_code == 200
    assert me.json()["phone"] == phone


def test_otp_request_response_is_identical_for_new_and_existing_phone(client: TestClient):
    existing = player_login(client, "9812300002")
    fresh_response = client.post("/api/player/otp/request", json={"phone": "9812300099"})
    existing_response = client.post("/api/player/otp/request", json={"phone": "9812300002"})
    assert fresh_response.status_code == existing_response.status_code == 200
    assert fresh_response.json() == existing_response.json()


def test_otp_verify_wrong_code_does_not_burn_the_challenge(client: TestClient):
    phone = "9812300003"
    client.post("/api/player/otp/request", json={"phone": phone})
    code = client.app.state.otp_provider.last_code_for(phone)
    wrong = client.post("/api/player/otp/verify", json={"phone": phone, "code": "000000" if code != "000000" else "111111"})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "INVALID_OTP"
    correct = client.post("/api/player/otp/verify", json={"phone": phone, "code": code})
    assert correct.status_code == 200


def test_otp_verify_expired_code_is_rejected(client: TestClient):
    phone = "9812300004"
    client.post("/api/player/otp/request", json={"phone": phone})
    code = client.app.state.otp_provider.last_code_for(phone)
    with client.app.state.session_factory() as session:
        from backend.app.models import OtpChallenge
        challenge = session.query(OtpChallenge).filter_by(phone=phone).one()
        challenge.expires_at = now_ist()
        session.commit()
    expired = client.post("/api/player/otp/verify", json={"phone": phone, "code": code})
    assert expired.status_code == 401
    assert expired.json()["error"]["code"] == "INVALID_OTP"


def test_otp_verify_maximum_attempts_locks_the_challenge(client: TestClient):
    phone = "9812300005"
    client.post("/api/player/otp/request", json={"phone": phone})
    code = client.app.state.otp_provider.last_code_for(phone)
    wrong_code = "000000" if code != "000000" else "111111"
    for _ in range(OTP_MAX_ATTEMPTS):
        response = client.post("/api/player/otp/verify", json={"phone": phone, "code": wrong_code})
        assert response.status_code == 401
    locked = client.post("/api/player/otp/verify", json={"phone": phone, "code": code})
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "OTP_LOCKED"


def test_otp_new_request_invalidates_the_previous_challenge(client: TestClient):
    phone = "9812300006"
    client.post("/api/player/otp/request", json={"phone": phone})
    first_code = client.app.state.otp_provider.last_code_for(phone)
    client.post("/api/player/otp/request", json={"phone": phone})
    stale = client.post("/api/player/otp/verify", json={"phone": phone, "code": first_code})
    assert stale.status_code == 401


def test_otp_request_rate_limit_by_phone(client: TestClient):
    phone = "9812300007"
    for _ in range(OTP_REQUEST_PHONE_LIMIT):
        assert client.post("/api/player/otp/request", json={"phone": phone}).status_code == 200
    limited = client.post("/api/player/otp/request", json={"phone": phone})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


def test_otp_request_rate_limit_by_ip(client: TestClient):
    for i in range(OTP_REQUEST_IP_LIMIT):
        assert client.post("/api/player/otp/request", json={"phone": f"98123500{i:02d}"}).status_code == 200
    limited = client.post("/api/player/otp/request", json={"phone": "9812399999"})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


def test_otp_verify_rate_limit_by_ip(client: TestClient):
    for _ in range(OTP_VERIFY_IP_LIMIT):
        assert client.post("/api/player/otp/verify", json={"phone": "9812300008", "code": "123456"}).status_code == 401
    limited = client.post("/api/player/otp/verify", json={"phone": "9812300008", "code": "123456"})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


def test_player_session_expiration(client: TestClient):
    headers = player_login(client, "9812300009")
    assert client.get("/api/player/me").status_code == 200
    with client.app.state.session_factory() as session:
        stored = session.query(PlayerSession).one(); stored.expires_at = now_ist(); session.commit()
    assert client.get("/api/player/me").status_code == 401


def test_player_logout_revokes_the_session(client: TestClient):
    headers = player_login(client, "9812300010")
    assert client.get("/api/player/me").status_code == 200
    assert client.post("/api/player/logout", headers=headers).status_code == 204
    assert client.get("/api/player/me").status_code == 401
    with client.app.state.session_factory() as session:
        assert session.query(PlayerSession).count() == 0


def test_player_session_rotates_on_reverification(client: TestClient):
    phone = "9812300011"
    player_login(client, phone)
    old_cookie = client.cookies.get("sc_player_session")
    player_login(client, phone)
    new_cookie = client.cookies.get("sc_player_session")
    assert old_cookie != new_cookie
    with client.app.state.session_factory() as session:
        assert session.query(PlayerSession).count() == 1


# --- Authorization -----------------------------------------------------------

def test_unauthenticated_registration_creation_is_rejected(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    response = client.post(f"/api/events/{event['public_id']}/registrations", json={"name": "Nobody", "preferred_position": "NO_PREFERENCE"})
    assert response.status_code == 401


def test_player_cannot_access_another_players_registration(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    owner_headers = player_login(client, "9812400001")
    registration = register(client, event["public_id"], owner_headers).json()

    other_headers = player_login(client, "9812400002")
    cross = client.get(f"/api/registrations/{registration['public_id']}", headers=other_headers)
    assert cross.status_code == 404


def test_player_cannot_upload_payment_proof_for_another_players_registration(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    owner_headers = player_login(client, "9812400003")
    registration = register(client, event["public_id"], owner_headers).json()

    # A separate client/cookie-jar, so the second player's session doesn't
    # overwrite the owner's session cookie in this test's shared client.
    other_client = TestClient(client.app)
    other_headers = player_login(other_client, "9812400004")
    cross = submit_payment(other_client, registration, other_headers)
    assert cross.status_code == 404
    # The rightful owner (original session) is unaffected and can still submit their own proof.
    assert submit_payment(client, registration, owner_headers).status_code == 200


def test_knowing_the_public_id_is_insufficient_without_a_session(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    owner_headers = player_login(client, "9812400005")
    registration = register(client, event["public_id"], owner_headers).json()
    fresh_client = TestClient(client.app)
    assert fresh_client.get(f"/api/registrations/{registration['public_id']}").status_code == 401


def test_organizer_flow_is_unaffected_by_player_authentication(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    player_headers = player_login(client, "9812400006")
    registration = register(client, event["public_id"], player_headers).json()
    submit_payment(client, registration, player_headers)
    payment_id = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["id"]
    confirmed = client.post(f"/api/admin/payments/{payment_id}/confirm", headers=headers)
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "CONFIRMED"
    assert client.get("/api/auth/me").json()["organizer"] == {"username": "organizer", "role": "ORGANIZER"}


# --- Cancellation ------------------------------------------------------------

def test_player_can_cancel_own_pending_registration(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9878100001")
    reg = register(client, event["public_id"], player_headers).json()
    response = client.post(f"/api/registrations/{reg['public_id']}/cancel", headers=player_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


def test_player_can_cancel_own_confirmed_registration_and_frees_capacity(client: TestClient):
    headers = login(client); event = new_event(client, headers, capacity=2)
    p1_headers = player_login(client, "9878100002")
    reg1 = register(client, event["public_id"], p1_headers).json(); submit_payment(client, reg1, p1_headers)
    payment_id_1 = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["id"]
    client.post(f"/api/admin/payments/{payment_id_1}/confirm", headers=headers)

    p2_client = TestClient(client.app)
    p2_headers = player_login(p2_client, "9878100022")
    reg2 = register(p2_client, event["public_id"], p2_headers).json(); submit_payment(p2_client, reg2, p2_headers)
    payment_id_2 = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["id"]
    client.post(f"/api/admin/payments/{payment_id_2}/confirm", headers=headers)

    summary_before = client.get(f"/api/events/{event['public_id']}/summary").json()
    assert summary_before["confirmed"] == 2 and summary_before["available"] == 0
    assert client.get(f"/api/events/{event['public_id']}").json()["status"] == "FULL"

    response = client.post(f"/api/registrations/{reg1['public_id']}/cancel", headers=p1_headers)
    assert response.status_code == 200

    summary_after = client.get(f"/api/events/{event['public_id']}/summary").json()
    assert summary_after["confirmed"] == 1 and summary_after["available"] == 1
    assert client.get(f"/api/events/{event['public_id']}").json()["status"] == "OPEN"


def test_player_cannot_cancel_another_players_registration(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    owner_headers = player_login(client, "9878100003")
    reg = register(client, event["public_id"], owner_headers).json()
    other_client = TestClient(client.app)
    other_headers = player_login(other_client, "9878100004")
    response = other_client.post(f"/api/registrations/{reg['public_id']}/cancel", headers=other_headers)
    assert response.status_code == 404


def test_cancelling_already_cancelled_registration_is_idempotent(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9878100005")
    reg = register(client, event["public_id"], player_headers).json()
    first = client.post(f"/api/registrations/{reg['public_id']}/cancel", headers=player_headers)
    second = client.post(f"/api/registrations/{reg['public_id']}/cancel", headers=player_headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "CANCELLED"


def test_organizer_can_cancel_registration_on_own_event(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9878100006")
    reg = register(client, event["public_id"], player_headers).json()
    response = client.post(f"/api/admin/registrations/{reg['id']}/cancel", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


def test_rejected_registration_cannot_be_cancelled(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9878000001")
    reg = register(client, event["public_id"], player_headers).json(); submit_payment(client, reg, player_headers)
    payment_id = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["id"]
    client.post(f"/api/admin/payments/{payment_id}/reject", headers=headers, json={"reason": "bad proof"})
    response = client.post(f"/api/registrations/{reg['public_id']}/cancel", headers=player_headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE_TRANSITION"


def test_cancellation_blocked_after_event_has_started(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9878000002")
    reg = register(client, event["public_id"], player_headers).json()
    assert client.patch(f"/api/admin/events/{event['id']}", headers=headers, json={"status": "ONGOING"}).status_code == 200
    response = client.post(f"/api/registrations/{reg['public_id']}/cancel", headers=player_headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EVENT_ALREADY_STARTED"


# --- Re-registration after cancellation --------------------------------------

def test_player_can_register_again_after_cancelling_as_a_new_record(client: TestClient):
    from backend.app.models import Registration as RegModel
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9878200001")
    first = register(client, event["public_id"], player_headers).json()
    client.post(f"/api/registrations/{first['public_id']}/cancel", headers=player_headers)

    second = register(client, event["public_id"], player_headers)
    assert second.status_code == 201
    second_data = second.json()
    assert second_data["id"] != first["id"]
    assert second_data["public_id"] != first["public_id"]
    assert second_data["status"] == "PENDING"

    with client.app.state.session_factory() as session:
        original = session.get(RegModel, first["id"])
        assert original.status == "CANCELLED"
        assert original.public_id == first["public_id"]  # untouched, not reused


def test_cancelled_registration_does_not_count_toward_capacity(client: TestClient):
    headers = login(client); event = new_event(client, headers, capacity=2)
    p1_headers = player_login(client, "9878200002")
    reg1 = register(client, event["public_id"], p1_headers).json(); submit_payment(client, reg1, p1_headers)
    payment_id_1 = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["id"]
    client.post(f"/api/admin/payments/{payment_id_1}/confirm", headers=headers)

    p2_client = TestClient(client.app)
    p2_headers = player_login(p2_client, "9878200023")
    reg2 = register(p2_client, event["public_id"], p2_headers).json(); submit_payment(p2_client, reg2, p2_headers)
    payment_id_2 = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["id"]
    client.post(f"/api/admin/payments/{payment_id_2}/confirm", headers=headers)
    assert client.get(f"/api/events/{event['public_id']}").json()["status"] == "FULL"

    # Cancel one of the two confirmed players, freeing exactly one slot.
    client.post(f"/api/registrations/{reg1['public_id']}/cancel", headers=p1_headers)

    other_client = TestClient(client.app)
    other_headers = player_login(other_client, "9878200003")
    third = register(other_client, event["public_id"], other_headers).json()
    assert third["status"] == "PENDING"  # capacity available again, not waitlisted


def test_still_only_one_active_registration_per_event_after_cancellation(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9878200004")
    first = register(client, event["public_id"], player_headers).json()
    client.post(f"/api/registrations/{first['public_id']}/cancel", headers=player_headers)
    second = register(client, event["public_id"], player_headers)
    assert second.status_code == 201
    third = register(client, event["public_id"], player_headers)
    assert third.status_code == 409
    assert third.json()["error"]["code"] == "REGISTRATION_EXISTS"


def test_database_rejects_duplicate_active_registration_bypassing_service_layer(client: TestClient):
    import uuid as uuid_module
    from sqlalchemy.exc import IntegrityError
    from backend.app.models import Registration as RegModel
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9878300001")
    reg = register(client, event["public_id"], player_headers).json()

    with client.app.state.session_factory() as session:
        existing = session.get(RegModel, reg["id"])
        duplicate = RegModel(
            public_id=uuid_module.uuid4().hex, match_id=existing.match_id, user_id=existing.user_id,
            name="Bypass Attempt", phone=existing.phone, status="PENDING", preferred_position="NO_PREFERENCE",
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


# --- Organizer ownership / RBAC ----------------------------------------------

def test_organizer_cannot_access_another_organizers_event(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    other_client, other_headers = create_second_organizer(client)
    assert other_client.get(f"/api/admin/events/{event['id']}", headers=other_headers).status_code == 404
    assert other_client.patch(f"/api/admin/events/{event['id']}", headers=other_headers, json={"status": "FULL"}).status_code == 404
    assert other_client.get(f"/api/admin/events/{event['id']}/summary", headers=other_headers).status_code == 404
    assert other_client.get(f"/api/admin/events/{event['id']}/registrations", headers=other_headers).status_code == 404


def test_organizer_cannot_review_payments_on_another_organizers_event(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9877600001")
    reg = register(client, event["public_id"], player_headers).json(); submit_payment(client, reg, player_headers)
    payment_id = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["id"]
    proof_url = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["screenshot_url"]

    other_client, other_headers = create_second_organizer(client)
    assert other_client.post(f"/api/admin/payments/{payment_id}/confirm", headers=other_headers).status_code == 404
    assert other_client.post(f"/api/admin/payments/{payment_id}/reject", headers=other_headers, json={"reason": "not yours"}).status_code == 404
    assert other_client.get(proof_url, headers=other_headers).status_code == 404
    # the rightful organizer still can
    assert client.post(f"/api/admin/payments/{payment_id}/confirm", headers=headers).status_code == 200


def test_organizer_cannot_promote_or_cancel_on_another_organizers_event(client: TestClient):
    headers = login(client); event = new_event(client, headers, capacity=2)
    p1 = player_login(client, "9877700001"); reg1 = register(client, event["public_id"], p1).json()
    submit_payment(client, reg1, p1)
    pay_id_1 = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["id"]
    client.post(f"/api/admin/payments/{pay_id_1}/confirm", headers=headers)

    p2_client = TestClient(client.app)
    p2 = player_login(p2_client, "9877700002"); reg2 = register(p2_client, event["public_id"], p2).json()
    submit_payment(p2_client, reg2, p2)
    pay_id_2 = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["id"]
    client.post(f"/api/admin/payments/{pay_id_2}/confirm", headers=headers)

    p3_client = TestClient(client.app)
    p3 = player_login(p3_client, "9877700003"); reg3 = register(p3_client, event["public_id"], p3).json()
    assert reg3["status"] == "WAITLISTED"

    other_client, other_headers = create_second_organizer(client)
    assert other_client.post(f"/api/admin/registrations/{reg3['id']}/promote", headers=other_headers).status_code == 404
    assert other_client.post(f"/api/admin/registrations/{reg1['id']}/cancel", headers=other_headers).status_code == 404


def test_admin_events_list_is_filtered_by_ownership(client: TestClient):
    headers = login(client); mine = new_event(client, headers, name="Mine")
    other_client, other_headers = create_second_organizer(client)
    theirs_response = other_client.post("/api/admin/events", headers=other_headers, json={
        "name": "Theirs", "date": "2026-10-05", "start_time": "07:00:00", "end_time": "10:00:00",
        "venue": "Other Ground", "capacity": 4, "fee": 100,
        "registration_deadline": "2026-10-04T20:00:00", "upi_id": "other@upi",
    })
    assert theirs_response.status_code == 201
    theirs = theirs_response.json()

    mine_ids = {item["id"] for item in client.get("/api/admin/events", headers=headers).json()}
    theirs_ids = {item["id"] for item in other_client.get("/api/admin/events", headers=other_headers).json()}
    assert mine["id"] in mine_ids and theirs["id"] not in mine_ids
    assert theirs["id"] in theirs_ids and mine["id"] not in theirs_ids


def test_platform_admin_can_access_any_organizers_event(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    other_client, other_headers = create_second_organizer(client)
    make_platform_admin(other_client, "second_organizer")
    assert other_client.get(f"/api/admin/events/{event['id']}", headers=other_headers).status_code == 200


# --- Concurrency / production failure cases ----------------------------------

def test_cancel_and_promote_race_never_exceeds_capacity(client: TestClient):
    headers = login(client); event = new_event(client, headers, capacity=2)
    a_headers = player_login(client, "9877000001")
    a = register(client, event["public_id"], a_headers).json(); submit_payment(client, a, a_headers)
    a_payment_id = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["id"]
    client.post(f"/api/admin/payments/{a_payment_id}/confirm", headers=headers)

    a2_client = TestClient(client.app)
    a2_headers = player_login(a2_client, "9877000003")
    a2 = register(a2_client, event["public_id"], a2_headers).json(); submit_payment(a2_client, a2, a2_headers)
    a2_payment_id = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["id"]
    client.post(f"/api/admin/payments/{a2_payment_id}/confirm", headers=headers)

    b_client = TestClient(client.app)
    b_headers = player_login(b_client, "9877000002")
    b = register(b_client, event["public_id"], b_headers).json()
    assert b["status"] == "WAITLISTED"

    a_cookie = client.cookies.get("sc_player_session")
    org_cookie = client.cookies.get("sc_organizer_session")

    def do_cancel():
        thread_client = TestClient(client.app)
        thread_client.cookies.set("sc_player_session", a_cookie)
        return thread_client.post(f"/api/registrations/{a['public_id']}/cancel", headers=a_headers).status_code

    def do_promote():
        thread_client = TestClient(client.app)
        thread_client.cookies.set("sc_organizer_session", org_cookie)
        return thread_client.post(f"/api/admin/registrations/{b['id']}/promote", headers=headers).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancel_future = pool.submit(do_cancel)
        promote_future = pool.submit(do_promote)
        cancel_status = cancel_future.result()
        promote_status = promote_future.result()

    assert cancel_status == 200
    assert promote_status in (200, 409)
    if promote_status == 409:
        assert client.post(f"/api/admin/registrations/{b['id']}/promote", headers=headers).status_code == 200

    # Promotion moves WAITLISTED -> PENDING (payment still owed), not straight
    # to CONFIRMED — so the invariant here is "A2 stays confirmed, B is no
    # longer waitlisted, and capacity (confirmed count) was never exceeded."
    summary = client.get(f"/api/events/{event['public_id']}/summary").json()
    assert summary["confirmed"] == 1 and summary["waitlisted"] == 0 and summary["pending"] == 1


def test_concurrent_confirmation_after_cancellation_frees_slot(client: TestClient):
    headers = login(client); event = new_event(client, headers, capacity=2)
    a_headers = player_login(client, "9877100001")
    a = register(client, event["public_id"], a_headers).json(); submit_payment(client, a, a_headers)
    a_payment_id = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["id"]
    client.post(f"/api/admin/payments/{a_payment_id}/confirm", headers=headers)

    a2_client = TestClient(client.app)
    a2_headers = player_login(a2_client, "9877100004")
    a2 = register(a2_client, event["public_id"], a2_headers).json(); submit_payment(a2_client, a2, a2_headers)
    a2_payment_id = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["id"]
    client.post(f"/api/admin/payments/{a2_payment_id}/confirm", headers=headers)
    assert client.get(f"/api/events/{event['public_id']}").json()["status"] == "FULL"

    # A cancels, freeing exactly one of the two slots (A2 remains confirmed).
    client.post(f"/api/registrations/{a['public_id']}/cancel", headers=a_headers)

    for phone in ("9877100002", "9877100003"):
        p_client = TestClient(client.app)
        p_headers = player_login(p_client, phone)
        reg = register(p_client, event["public_id"], p_headers).json()
        submit_payment(p_client, reg, p_headers)
    payment_ids = [row["payment"]["id"] for row in client.get("/api/admin/payments/pending", headers=headers).json()]
    assert len(payment_ids) == 2

    def confirm(payment_id: int):
        with client.app.state.session_factory() as session:
            return review_payment(session, payment_id, approve=True, actor_id=1).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, payment_ids))

    summary = client.get(f"/api/events/{event['public_id']}/summary").json()
    assert sorted(results) == ["CONFIRMED", "WAITLISTED"]
    assert summary["confirmed"] == 2  # A2 (already confirmed) + exactly one of the two new competitors


def test_concurrent_duplicate_cancellation_requests_are_safe(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9877200001")
    reg = register(client, event["public_id"], player_headers).json()
    cookie = client.cookies.get("sc_player_session")

    def do_cancel(_):
        thread_client = TestClient(client.app)
        thread_client.cookies.set("sc_player_session", cookie)
        return thread_client.post(f"/api/registrations/{reg['public_id']}/cancel", headers=player_headers).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(do_cancel, range(4)))
    assert all(status == 200 for status in results)


def test_concurrent_reregistration_after_cancellation_only_one_wins(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    phone = "9877300001"
    player_headers = player_login(client, phone)
    first = register(client, event["public_id"], player_headers).json()
    client.post(f"/api/registrations/{first['public_id']}/cancel", headers=player_headers)
    cookie = client.cookies.get("sc_player_session")

    def do_register(_):
        thread_client = TestClient(client.app)
        thread_client.cookies.set("sc_player_session", cookie)
        return thread_client.post(f"/api/events/{event['public_id']}/registrations", json={"name": "Race Player", "preferred_position": "NO_PREFERENCE"}, headers=player_headers).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(do_register, range(4)))

    assert results.count(201) == 1
    assert results.count(409) == 3

    with client.app.state.session_factory() as session:
        count = session.execute(
            text("SELECT COUNT(*) FROM registrations WHERE match_id = :mid AND status != 'CANCELLED'"), {"mid": event["id"]},
        ).scalar_one()
        assert count == 1


def test_concurrent_payment_submission_has_no_race_or_orphaned_files(client: TestClient):
    from backend.app.models import PaymentProof as ProofModel, Registration as RegModel
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9877400001")
    reg = register(client, event["public_id"], player_headers).json()
    cookie = client.cookies.get("sc_player_session")

    def do_submit(_):
        thread_client = TestClient(client.app)
        thread_client.cookies.set("sc_player_session", cookie)
        return thread_client.post(f"/api/registrations/{reg['public_id']}/payment", files=image_file(), headers=player_headers).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(do_submit, range(6)))

    # All six requests upload byte-identical screenshots from the same
    # player. Only the first actually creates a proof; the rest are
    # recognised as the same logical submission (the idempotent-retry path in
    # submit_payment_proof) and succeed without creating duplicates, rather
    # than racing into a false conflict.
    assert results.count(200) == 6

    with client.app.state.session_factory() as session:
        row = session.get(RegModel, reg["id"])
        assert row.payment is not None
        proofs = session.query(ProofModel).filter_by(payment_id=row.payment.id).all()
        assert len(proofs) == 1  # no duplicate proof rows from the race
        # The application's storage credential has no delete permission over
        # payment-proof objects (Phase 3 requirement: evidence is
        # append-only), so a request that loses the race leaves its
        # just-written file in place as a harmless, unreferenced orphan
        # rather than deleting it — reconciliation (a separate,
        # privileged, manually-run process) is what would report/clean
        # those up, never normal request-handling code. The invariant that
        # actually matters is the other direction: the DB never points at a
        # file that doesn't exist.
        proofs_dir = client.app.state.uploads_dir / "proofs"
        files_on_disk = {p.name for p in proofs_dir.iterdir()} if proofs_dir.exists() else set()
        assert proofs[0].storage_key in files_on_disk


def test_app_startup_fails_loudly_on_ambiguous_ownership_backfill(tmp_path: Path):
    data_dir = tmp_path / "ambiguous"; data_dir.mkdir(); database = data_dir / "stranger_club.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE matches (id INTEGER PRIMARY KEY, public_id VARCHAR(64) UNIQUE, name VARCHAR(120), date DATE,
                start_time TIME, end_time TIME, venue VARCHAR(200), capacity INTEGER, fee INTEGER,
                registration_deadline DATETIME, upi_id VARCHAR(120), qr_code_path VARCHAR(255), status VARCHAR(32),
                created_at DATETIME, updated_at DATETIME);
            CREATE TABLE registrations (id INTEGER PRIMARY KEY, match_id INTEGER, name VARCHAR(120), phone VARCHAR(16),
                email VARCHAR(254), status VARCHAR(32), created_at DATETIME, updated_at DATETIME,
                CONSTRAINT uq_registration_match_phone UNIQUE(match_id, phone));
            CREATE TABLE payments (id INTEGER PRIMARY KEY, registration_id INTEGER UNIQUE, amount INTEGER,
                screenshot_path VARCHAR(255), screenshot_token VARCHAR(64) UNIQUE, status VARCHAR(32), submitted_at DATETIME,
                verified_at DATETIME, rejection_reason TEXT);
            CREATE TABLE organizers (id INTEGER PRIMARY KEY, username VARCHAR(80) UNIQUE, password_hash VARCHAR(255),
                role VARCHAR(32), is_active BOOLEAN, created_at DATETIME, updated_at DATETIME);
            INSERT INTO matches VALUES (1, 'ambiguous-event', 'Ambiguous Cricket', '2026-10-02', '07:00:00', '10:00:00',
                'Ground', 22, 300, '2026-10-01 20:00:00', 'x@upi', NULL, 'ACTIVE', '2026-08-01', '2026-08-01');
            INSERT INTO organizers VALUES (1, 'existing_organizer', 'hash', 'ORGANIZER', 1, '2026-08-01', '2026-08-01');
            INSERT INTO organizers VALUES (2, 'another_organizer', 'hash2', 'ORGANIZER', 1, '2026-08-01', '2026-08-01');
        """)
    with pytest.raises(RuntimeError, match="Cannot safely backfill"):
        with TestClient(create_app(data_dir=data_dir, admin_username="existing_organizer", admin_password="correct-horse")):
            pass


# --- Audit attribution --------------------------------------------------------

def test_audit_log_records_actor_for_registration_cancellation(client: TestClient):
    from backend.app.models import AuditLog
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9877800001")
    reg = register(client, event["public_id"], player_headers).json()
    client.post(f"/api/registrations/{reg['public_id']}/cancel", headers=player_headers)
    with client.app.state.session_factory() as session:
        entry = session.query(AuditLog).filter_by(event_type="REGISTRATION_CANCELLED").one()
        assert entry.actor_type == "PLAYER"
        assert entry.actor_id is not None
        assert entry.before_json == {"status": "PENDING"}
        assert entry.after_json == {"status": "CANCELLED"}


def test_audit_log_records_organizer_actor_for_event_and_payment_actions(client: TestClient):
    from backend.app.models import AuditLog
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9877800002")
    reg = register(client, event["public_id"], player_headers).json(); submit_payment(client, reg, player_headers)
    payment_id = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["id"]
    client.post(f"/api/admin/payments/{payment_id}/confirm", headers=headers)
    with client.app.state.session_factory() as session:
        created = session.query(AuditLog).filter_by(event_type="EVENT_CREATED").order_by(AuditLog.id.desc()).first()
        # PAYMENT_VERIFIED is recorded twice on purpose — once against the
        # registration (it moved to CONFIRMED) and once against the payment
        # itself (see review_payment / transition_payment) — disambiguated by
        # entity_type.
        verified_registration = session.query(AuditLog).filter_by(event_type="PAYMENT_VERIFIED", entity_type="registration").one()
        verified_payment = session.query(AuditLog).filter_by(event_type="PAYMENT_VERIFIED", entity_type="payment").one()
        assert created.actor_type == "ORGANIZER" and created.actor_id is not None
        assert verified_registration.actor_type == "ORGANIZER" and verified_registration.actor_id is not None
        assert verified_payment.actor_type == "ORGANIZER" and verified_payment.actor_id is not None


# --- Player profile -----------------------------------------------------------

def test_player_can_view_and_update_own_profile(client: TestClient):
    player_headers = player_login(client, "9877900001")
    profile = client.get("/api/player/profile", headers=player_headers)
    assert profile.status_code == 200
    assert profile.json() == {"display_name": None, "cricket_role": "NO_PREFERENCE", "skill_rating": None, "bio": None}
    updated = client.patch("/api/player/profile", headers=player_headers, json={"display_name": "Test Player", "cricket_role": "BOWLER", "skill_rating": 7})
    assert updated.status_code == 200
    assert updated.json() == {"display_name": "Test Player", "cricket_role": "BOWLER", "skill_rating": 7, "bio": None}


def test_profile_requires_authentication(client: TestClient):
    assert client.get("/api/player/profile").status_code == 401


# --- Payment configuration / snapshot integrity (Phase 2C) -------------------

def test_payment_configuration_change_does_not_rewrite_existing_payment_snapshot(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9879000001")
    registration = register(client, event["public_id"], player_headers).json()
    assert registration["payment"]["amount_due"] == 350
    assert registration["payment"]["payee_upi_id_snapshot"] == "strangerclub@upi"

    updated = client.patch(
        f"/api/admin/events/{event['id']}/payment-configuration", headers=headers,
        json={"payee_upi_id": "newhandle@upi", "payee_name": "New Payee"},
    )
    assert updated.status_code == 200, updated.json()
    assert updated.json()["payee_upi_id"] == "newhandle@upi"

    # The event's live configuration changed, but this player's own Payment
    # record — created before the change — must keep showing what applied
    # when they registered, not the new configuration.
    unchanged = client.get(f"/api/registrations/{registration['public_id']}", headers=player_headers).json()
    assert unchanged["payment"]["payee_upi_id_snapshot"] == "strangerclub@upi"
    assert "strangerclub%40upi" in unchanged["payment"]["upi_uri"]  # URL-encoded '@' — still the old, snapshotted handle
    assert "newhandle" not in unchanged["payment"]["upi_uri"]

    # A player registering AFTER the change gets the new configuration.
    other_headers = player_login(client, "9879000002")
    later = register(client, event["public_id"], other_headers).json()
    assert later["payment"]["payee_upi_id_snapshot"] == "newhandle@upi"
    assert later["payment"]["payee_name_snapshot"] == "New Payee"


def test_payment_configuration_update_requires_event_ownership(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    other_client, other_headers = create_second_organizer(client)
    response = other_client.patch(
        f"/api/admin/events/{event['id']}/payment-configuration", headers=other_headers, json={"payee_upi_id": "hijack@upi"},
    )
    assert response.status_code == 404


def test_payment_configuration_rejects_malformed_upi_id(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    response = client.patch(f"/api/admin/events/{event['id']}/payment-configuration", headers=headers, json={"payee_upi_id": "not-a-upi-id"})
    assert response.status_code == 422


def test_custom_qr_upload_and_retrieval_is_owner_scoped(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    qr_bytes = image_file()["screenshot"][1]
    uploaded = client.post(
        f"/api/admin/events/{event['id']}/payment-configuration/qr", headers=headers,
        files={"qr_image": ("qr.png", qr_bytes, "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.json()
    assert uploaded.json()["qr_source"] == "UPLOADED"
    assert uploaded.json()["has_custom_qr"] is True

    player_headers = player_login(client, "9879000003")
    registration = register(client, event["public_id"], player_headers).json()
    assert registration["payment"]["qr_source_snapshot"] == "UPLOADED"
    qr_url = registration["payment"]["qr_image_url"]
    assert qr_url is not None

    # Only the owning, authenticated player can fetch it.
    fresh_client = TestClient(client.app)
    assert fresh_client.get(qr_url).status_code == 401

    other_client = TestClient(client.app)
    other_headers = player_login(other_client, "9879000004")
    other_registration = register(other_client, event["public_id"], other_headers).json()
    assert other_client.get(other_registration["payment"]["qr_image_url"], headers=other_headers).status_code == 200
    # Cross-player access to someone else's payment-qr URL is refused.
    assert other_client.get(qr_url, headers=other_headers).status_code == 404
    # The original owner (untouched session) can still fetch their own.
    assert client.get(qr_url, headers=player_headers).status_code == 200


def test_qr_upload_requires_event_ownership(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    other_client, other_headers = create_second_organizer(client)
    response = other_client.post(
        f"/api/admin/events/{event['id']}/payment-configuration/qr", headers=other_headers,
        files={"qr_image": ("qr.png", image_file()["screenshot"][1], "image/png")},
    )
    assert response.status_code == 404


def test_cancellation_never_rewrites_a_verified_payment(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9879100001")
    reg = register(client, event["public_id"], player_headers).json(); submit_payment(client, reg, player_headers)
    payment_id = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["id"]
    client.post(f"/api/admin/payments/{payment_id}/confirm", headers=headers)

    cancelled = client.post(f"/api/admin/registrations/{reg['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["payment"]["status"] == "VERIFIED"  # untouched — no refund state invented

    with client.app.state.session_factory() as session:
        from backend.app.models import Payment as PaymentModel, PaymentProof as ProofModel
        payment = session.get(PaymentModel, payment_id)
        assert payment.status == "VERIFIED"
        proofs = session.query(ProofModel).filter_by(payment_id=payment_id).all()
        assert len(proofs) == 1 and proofs[0].status == "ACCEPTED"


def test_duplicate_screenshot_across_registrations_is_flagged_not_blocked(client: TestClient):
    headers = login(client); event = new_event(client, headers, capacity=10)
    a_headers = player_login(client, "9879200001")
    a = register(client, event["public_id"], a_headers).json()
    assert submit_payment(client, a, a_headers).status_code == 200

    b_client = TestClient(client.app)
    b_headers = player_login(b_client, "9879200002")
    b = register(b_client, event["public_id"], b_headers).json()
    # Same screenshot bytes as A's submission, from a different player/registration.
    second = b_client.post(f"/api/registrations/{b['public_id']}/payment", files=image_file(), headers=b_headers)
    assert second.status_code == 200  # never automatically blocked — only flagged for the organizer

    pending = client.get("/api/admin/payments/pending", headers=headers).json()
    by_reg = {row["id"]: row for row in pending}
    b_payment = by_reg[b["id"]]["payment"]
    assert b_payment["duplicate_of"], "organizer should see a duplicate warning for B's proof"
    assert b_payment["duplicate_of"][0]["registration_id"] == a["id"]


def test_concurrent_review_of_the_same_payment_only_one_succeeds(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9879300001")
    reg = register(client, event["public_id"], player_headers).json()
    submit_payment(client, reg, player_headers)
    payment_id = client.get("/api/admin/payments/pending", headers=headers).json()[0]["payment"]["id"]
    org_cookie = client.cookies.get("sc_organizer_session")

    def do_confirm(_):
        thread_client = TestClient(client.app)
        thread_client.cookies.set("sc_organizer_session", org_cookie)
        return thread_client.post(f"/api/admin/payments/{payment_id}/confirm", headers=headers).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(do_confirm, range(4)))

    assert results.count(200) == 1
    assert results.count(409) == 3
    with client.app.state.session_factory() as session:
        from backend.app.models import PaymentProof as ProofModel
        accepted = session.query(ProofModel).filter_by(payment_id=payment_id, status="ACCEPTED").all()
        assert len(accepted) == 1  # exactly one proof accepted, never double-reviewed


def test_utr_is_validated_and_stored_on_the_proof_not_logged_in_full(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9879400001")
    reg = register(client, event["public_id"], player_headers).json()

    bad = client.post(f"/api/registrations/{reg['public_id']}/payment", files=image_file(), data={"utr": "!!!"}, headers=player_headers)
    assert bad.status_code == 422

    good = client.post(f"/api/registrations/{reg['public_id']}/payment", files=image_file(), data={"utr": "ABC123456789"}, headers=player_headers)
    assert good.status_code == 200
    assert good.json()["payment"]["utr_reference"] == "ABC123456789"

    with client.app.state.session_factory() as session:
        from backend.app.models import AuditLog
        submitted = session.query(AuditLog).filter_by(event_type="PAYMENT_PROOF_SUBMITTED").order_by(AuditLog.id.desc()).first()
        blob = str(submitted.metadata_json) + str(submitted.before_json) + str(submitted.after_json)
        assert "ABC123456789" not in blob


def test_player_payment_response_never_exposes_organizer_only_fields(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    player_headers = player_login(client, "9879500001")
    reg = register(client, event["public_id"], player_headers).json(); submit_payment(client, reg, player_headers)
    status = client.get(f"/api/registrations/{reg['public_id']}", headers=player_headers).json()
    assert status["payment"]["screenshot_url"] is None
    assert status["payment"]["pending_proof_id"] is None
    assert status["payment"]["proof_count"] is None
    assert status["payment"]["duplicate_of"] is None
