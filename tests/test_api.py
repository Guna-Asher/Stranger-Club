from __future__ import annotations

from io import BytesIO
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text

from backend.app.deps import (
    OTP_REQUEST_IP_LIMIT, OTP_REQUEST_PHONE_LIMIT, OTP_VERIFY_IP_LIMIT,
    PAYMENT_UPLOAD_RATE_LIMIT, REGISTRATION_RATE_LIMIT,
)
from backend.app.main import create_app
from backend.app.models import OrganizerSession, PlayerSession, Registration, now_ist
from backend.app.services import verify_payment
from backend.app.services_player import OTP_MAX_ATTEMPTS


@pytest.fixture()
def client(tmp_path: Path):
    with TestClient(create_app(data_dir=tmp_path / "data", admin_password="correct-horse")) as test_client:
        yield test_client


def image_file():
    image = Image.new("RGB", (12, 12), "green")
    content = BytesIO(); image.save(content, format="PNG")
    return {"screenshot": ("proof.png", content.getvalue(), "image/png")}


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
        versions = set(session.execute(text("SELECT version FROM schema_migrations")).scalars())
        assert {"20260818_domain_foundation", "20260818_payment_state_cleanup", "20260906_player_identity"}.issubset(versions)


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

        # No session at all: the old "public_id is enough" path must be gone.
        assert legacy_client.get(f"/api/registrations/{public_id}").status_code == 401

        # Even a real, freshly authenticated player cannot claim a legacy,
        # ownerless registration just by knowing its public_id.
        headers = player_login(legacy_client, "9000000001")
        cross = legacy_client.get(f"/api/registrations/{public_id}", headers=headers)
        assert cross.status_code == 404


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
    realtime = channel.get_nowait()
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
    assert submit_payment(client, registration, player_headers).status_code == 409
    pending = client.get("/api/admin/payments/pending").json()
    payment_id = pending[0]["payment"]["id"]
    confirmed = client.post(f"/api/admin/payments/{payment_id}/confirm", headers=headers)
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "CONFIRMED"
    assert confirmed.json()["payment"]["status"] == "VERIFIED"
    assert client.post(f"/api/admin/payments/{payment_id}/confirm", headers=headers).status_code == 409
    summary = client.get(f"/api/events/{event['public_id']}/summary").json()
    assert summary == {"event_id": event["public_id"], "capacity": 4, "confirmed": 1, "pending": 0, "payment_submitted": 0, "waitlisted": 0, "available": 3, "collected_amount": 350}


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
            return verify_payment(session, payment_id, approve=True).status

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
