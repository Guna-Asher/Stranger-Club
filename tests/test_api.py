from __future__ import annotations

from io import BytesIO
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text

from backend.app.main import PAYMENT_UPLOAD_RATE_LIMIT, REGISTRATION_RATE_LIMIT, create_app
from backend.app.models import OrganizerSession, now_ist
from backend.app.services import verify_payment


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


def new_event(client: TestClient, headers: dict[str, str], *, capacity: int = 4, name: str = "Friday Cricket") -> dict:
    response = client.post("/api/admin/events", headers=headers, json={
        "name": name, "date": "2026-10-02", "start_time": "07:00:00", "end_time": "10:00:00",
        "venue": "PlayArena, Bellandur", "capacity": capacity, "fee": 350,
        "registration_deadline": "2026-10-01T20:00:00", "upi_id": "strangerclub@upi",
    })
    assert response.status_code == 201, response.json()
    return response.json()


def register(client: TestClient, public_id: str, phone: str = "9876543210", position: str = "NO_PREFERENCE"):
    return client.post(f"/api/events/{public_id}/registrations", json={"name": "Rahul Kumar", "phone": phone, "email": "rahul@example.com", "preferred_position": position})


def submit_payment(client: TestClient, registration: dict):
    return client.post(f"/api/registrations/{registration['public_id']}/payment", files=image_file())


def test_health_public_event_and_migration(client: TestClient):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}
    assert client.get("/api/events/sunday-cricket-2026").status_code == 200
    with client.app.state.session_factory() as session:
        versions = set(session.execute(text("SELECT version FROM schema_migrations")).scalars())
        assert {"20260818_domain_foundation", "20260818_payment_state_cleanup"}.issubset(versions)


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
        with legacy_client.app.state.session_factory() as session:
            public_id = session.execute(text("SELECT public_id FROM registrations WHERE id = 1")).scalar_one()
        registration = legacy_client.get(f"/api/registrations/{public_id}").json()
        assert event["status"] == "OPEN"
        assert event["payment_submitted_count"] == 1
        assert registration["status"] == "PENDING"
        assert registration["payment"]["status"] == "SUBMITTED"


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
    assert register(client, first["public_id"]).status_code == 201
    summary_a = client.get(f"/api/events/{first['public_id']}/summary").json()
    summary_b = client.get(f"/api/events/{second['public_id']}/summary").json()
    assert summary_a["pending"] == 1
    assert summary_b["pending"] == 0


def test_registration_position_duplicate_and_realtime_event(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    channel = client.app.state.broadcaster.subscribe(event["public_id"])
    response = register(client, event["public_id"], position="ALL_ROUNDER")
    assert response.status_code == 201
    registration = response.json()
    assert registration["status"] == "PENDING"
    assert registration["preferred_position"] == "ALL_ROUNDER"
    realtime = channel.get_nowait()
    assert realtime["type"] == "REGISTRATION_CREATED"
    assert realtime["summary"]["pending"] == 1
    duplicate = register(client, event["public_id"], position="BOWLER")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "REGISTRATION_EXISTS"


def test_payment_lifecycle_summary_and_idempotency(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    registration = register(client, event["public_id"]).json()
    uploaded = submit_payment(client, registration)
    assert uploaded.status_code == 200
    assert uploaded.json()["payment"]["status"] == "SUBMITTED"
    assert submit_payment(client, registration).status_code == 409
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
    registration = register(client, event["public_id"]).json(); submit_payment(client, registration)
    payment_id = client.get("/api/admin/payments/pending").json()[0]["payment"]["id"]
    rejected = client.post(f"/api/admin/payments/{payment_id}/reject", headers=headers, json={"reason": "Reference number is not visible"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert rejected.json()["payment"]["status"] == "REJECTED"


def test_capacity_and_fifo_waitlist(client: TestClient):
    headers = login(client); event = new_event(client, headers, capacity=2)
    for phone in ("9876543201", "9876543202"):
        player = register(client, event["public_id"], phone).json(); submit_payment(client, player)
        payment_id = client.get("/api/admin/payments/pending").json()[0]["payment"]["id"]
        assert client.post(f"/api/admin/payments/{payment_id}/confirm", headers=headers).status_code == 200
    second = register(client, event["public_id"], "9876543203").json()
    third = register(client, event["public_id"], "9876543204").json()
    assert second["status"] == third["status"] == "WAITLISTED"
    assert client.post(f"/api/admin/registrations/{third['id']}/promote", headers=headers).json()["error"]["code"] == "WAITLIST_ORDER"
    assert client.post(f"/api/admin/registrations/{second['id']}/promote", headers=headers).status_code == 409
    summary = client.get(f"/api/events/{event['public_id']}/summary").json()
    assert summary["confirmed"] == 2 and summary["waitlisted"] == 2 and summary["available"] == 0


def test_concurrent_payment_confirmation_cannot_exceed_capacity(client: TestClient):
    headers = login(client); event = new_event(client, headers, capacity=2)
    # Take one confirmed spot, then put two independent proofs in review.
    first = register(client, event["public_id"], "9876543211").json(); submit_payment(client, first)
    first_payment = client.get("/api/admin/payments/pending").json()[0]["payment"]["id"]
    client.post(f"/api/admin/payments/{first_payment}/confirm", headers=headers)
    for phone in ("9876543212", "9876543213"):
        player = register(client, event["public_id"], phone).json(); submit_payment(client, player)
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
    registration = register(client, event["public_id"]).json(); submit_payment(client, registration)
    proof = client.get("/api/admin/payments/pending").json()[0]["payment"]["screenshot_url"]
    client.post("/api/auth/logout", headers=headers)
    assert client.get(proof).status_code == 401


def test_numeric_registration_id_no_longer_resolves(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    registration = register(client, event["public_id"]).json()
    assert client.get(f"/api/registrations/{registration['id']}").status_code == 404
    numeric_lookup = submit_payment(client, {"public_id": str(registration["id"])})
    assert numeric_lookup.status_code == 404


def test_registration_rate_limit(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    for _ in range(REGISTRATION_RATE_LIMIT):
        register(client, event["public_id"], "9100000002")
    limited = register(client, event["public_id"], "9100000002")
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


def test_payment_upload_rate_limit(client: TestClient):
    headers = login(client); event = new_event(client, headers)
    registration = register(client, event["public_id"], "9100000003").json()
    for _ in range(PAYMENT_UPLOAD_RATE_LIMIT):
        submit_payment(client, registration)
    limited = submit_payment(client, registration)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
