"""Phase 4 — Match domain (internal name Fixture; see models.py). Team-event
membership, transitions, editing-before-play, and organizer ownership.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from tests.test_api import client, create_second_organizer, login, new_event

__all__ = ["client"]


def make_two_teams(client: TestClient, headers: dict, event: dict) -> tuple[dict, dict]:
    team_a = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    team_b = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Bravo"}, headers=headers).json()
    return team_a, team_b


def test_create_and_list_fixture(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a, team_b = make_two_teams(client, headers, event)

    created = client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00", "sequence": 1},
        headers=headers,
    )
    assert created.status_code == 201, created.json()
    body = created.json()
    assert body["status"] == "SCHEDULED"
    assert body["team_a"]["id"] == team_a["id"]
    assert body["team_b"]["id"] == team_b["id"]

    listed = client.get(f"/api/admin/events/{event['id']}/fixtures", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 1


def test_team_cannot_play_itself(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a, _ = make_two_teams(client, headers, event)

    response = client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_a["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=headers,
    )
    assert response.status_code == 422


def test_cross_event_team_reference_rejected(client: TestClient):
    headers = login(client)
    event_one = new_event(client, headers, name="Event One")
    event_two = new_event(client, headers, name="Event Two")
    team_in_one, _ = make_two_teams(client, headers, event_one)
    team_in_two, _ = make_two_teams(client, headers, event_two)

    response = client.post(
        f"/api/admin/events/{event_one['id']}/fixtures",
        json={"team_a_id": team_in_one["id"], "team_b_id": team_in_two["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_duplicate_sequence_within_event_rejected(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a, team_b = make_two_teams(client, headers, event)
    team_c = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Charlie"}, headers=headers).json()

    first = client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00", "sequence": 1},
        headers=headers,
    )
    assert first.status_code == 201
    second = client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_c["id"], "scheduled_at": "2026-10-02T09:00:00", "sequence": 1},
        headers=headers,
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "FIXTURE_SEQUENCE_TAKEN"


def test_status_transitions_forward_only(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a, team_b = make_two_teams(client, headers, event)
    fixture = client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=headers,
    ).json()

    skip_ahead = client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"status": "COMPLETED"}, headers=headers)
    assert skip_ahead.status_code == 409
    assert skip_ahead.json()["error"]["code"] == "INVALID_STATE_TRANSITION"

    started = client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"status": "IN_PROGRESS"}, headers=headers)
    assert started.status_code == 200

    reverse = client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"status": "SCHEDULED"}, headers=headers)
    assert reverse.status_code == 409
    assert reverse.json()["error"]["code"] == "INVALID_STATE_TRANSITION"

    completed = client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"status": "COMPLETED"}, headers=headers)
    assert completed.status_code == 200

    reopen = client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"status": "IN_PROGRESS"}, headers=headers)
    assert reopen.status_code == 409
    assert reopen.json()["error"]["code"] == "INVALID_STATE_TRANSITION"


def test_cancelled_fixture_is_terminal(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a, team_b = make_two_teams(client, headers, event)
    fixture = client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=headers,
    ).json()
    cancelled = client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"status": "CANCELLED"}, headers=headers)
    assert cancelled.status_code == 200
    revive = client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"status": "SCHEDULED"}, headers=headers)
    assert revive.status_code == 409


def test_edits_blocked_once_match_has_started(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a, team_b = make_two_teams(client, headers, event)
    team_c = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Charlie"}, headers=headers).json()
    fixture = client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=headers,
    ).json()

    # Editing while SCHEDULED is fine.
    edited = client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"venue_override": "Backup Ground"}, headers=headers)
    assert edited.status_code == 200
    assert edited.json()["venue_override"] == "Backup Ground"

    client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"status": "IN_PROGRESS"}, headers=headers)

    blocked = client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"team_b_id": team_c["id"]}, headers=headers)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "FIXTURE_ALREADY_STARTED"


def test_organizer_ownership_enforced_for_fixtures(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a, team_b = make_two_teams(client, headers, event)
    fixture = client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=headers,
    ).json()

    other_client, other_headers = create_second_organizer(client)
    forbidden_list = other_client.get(f"/api/admin/events/{event['id']}/fixtures", headers=other_headers)
    assert forbidden_list.status_code == 404
    forbidden_create = other_client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=other_headers,
    )
    assert forbidden_create.status_code == 404
    forbidden_update = other_client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"status": "IN_PROGRESS"}, headers=other_headers)
    assert forbidden_update.status_code == 404


def test_creating_fixture_for_cancelled_event_is_rejected(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a, team_b = make_two_teams(client, headers, event)
    client.patch(f"/api/admin/events/{event['id']}", json={"status": "CANCELLED"}, headers=headers)

    response = client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EVENT_CANCELLED"


@pytest.mark.skipif("SC_TEST_DATABASE_URL" not in os.environ, reason="requires a real PostgreSQL instance (set SC_TEST_DATABASE_URL) to prove DB-level enforcement, not just SQLite")
def test_database_itself_rejects_cross_event_fixture(client: TestClient):
    """Same reasoning as test_team_membership's DB-level test: bypasses
    services.create_fixture and inserts a raw Fixture row spanning two
    events, proving the composite foreign keys reject it on real
    PostgreSQL."""
    from sqlalchemy.exc import IntegrityError

    from backend.app.models import Fixture

    headers = login(client)
    event_one = new_event(client, headers, name="Event One")
    event_two = new_event(client, headers, name="Event Two")
    team_in_one, _ = make_two_teams(client, headers, event_one)
    team_in_two, _ = make_two_teams(client, headers, event_two)

    with client.app.state.session_factory() as session:
        session.add(Fixture(
            event_id=event_one["id"], team_a_id=team_in_one["id"], team_b_id=team_in_two["id"],
            scheduled_at="2026-10-02T08:00:00",
        ))
        with pytest.raises(IntegrityError):
            session.commit()
