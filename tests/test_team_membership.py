"""Phase 4 — TeamMember domain: eligibility, capacity, concurrency, and
cross-event integrity (the "core of Phase 4" invariant). No mocking of
DB/business logic — concurrent-assignment races run real threads against a
real running app, and the cross-event-injection test below reaches past the
service layer to prove the *database* itself (not just Python) rejects it,
against real PostgreSQL when SC_TEST_DATABASE_URL is set.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from tests.test_api import client, image_file, login, new_event, player_login
from tests.test_teams import confirm_registration
from backend.app.services import review_payment

__all__ = ["client"]


def test_only_confirmed_registration_can_be_assigned(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()

    player_headers = player_login(client, "9111111111")
    pending = client.post(f"/api/events/{event['public_id']}/registrations", json={"name": "Pending Player"}, headers=player_headers).json()

    assign = client.post(f"/api/admin/teams/{team['id']}/members", json={"registration_id": pending["id"]}, headers=headers)
    assert assign.status_code == 409
    assert assign.json()["error"]["code"] == "REGISTRATION_NOT_CONFIRMED"


def test_waitlisted_registration_cannot_be_assigned(client: TestClient):
    headers = login(client)
    event = new_event(client, headers, capacity=2)
    team = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()

    confirm_registration(client, event["public_id"], "9111111112", "Filler One")
    confirm_registration(client, event["public_id"], "9111111113", "Filler Two")
    waitlisted_headers = player_login(client, "9111111114")
    waitlisted = client.post(f"/api/events/{event['public_id']}/registrations", json={"name": "Waitlisted Player"}, headers=waitlisted_headers).json()
    assert waitlisted["status"] == "WAITLISTED"

    assign = client.post(f"/api/admin/teams/{team['id']}/members", json={"registration_id": waitlisted["id"]}, headers=headers)
    assert assign.status_code == 409
    assert assign.json()["error"]["code"] == "REGISTRATION_NOT_CONFIRMED"


def test_rejected_registration_cannot_be_assigned(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()

    player_headers = player_login(client, "9111111115")
    reg = client.post(f"/api/events/{event['public_id']}/registrations", json={"name": "Rejected Player"}, headers=player_headers).json()
    upload = client.post(f"/api/registrations/{reg['public_id']}/payment", files=image_file(), headers=player_headers)
    with client.app.state.session_factory() as session:
        review_payment(session, upload.json()["payment"]["id"], approve=False, actor_id=1, reason="blurry")

    assign = client.post(f"/api/admin/teams/{team['id']}/members", json={"registration_id": reg["id"]}, headers=headers)
    assert assign.status_code == 409
    assert assign.json()["error"]["code"] == "REGISTRATION_NOT_CONFIRMED"


def test_cancelled_registration_cannot_be_assigned(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()

    confirmed, player_headers = confirm_registration(client, event["public_id"], "9111111116", "Cancelling Player")
    cancel = client.post(f"/api/registrations/{confirmed['public_id']}/cancel", headers=player_headers)
    assert cancel.status_code == 200

    assign = client.post(f"/api/admin/teams/{team['id']}/members", json={"registration_id": confirmed["id"]}, headers=headers)
    assert assign.status_code == 409
    assert assign.json()["error"]["code"] == "REGISTRATION_NOT_CONFIRMED"


def test_registration_from_another_event_cannot_be_assigned(client: TestClient):
    headers = login(client)
    event_one = new_event(client, headers, name="Event One")
    event_two = new_event(client, headers, name="Event Two")
    team_in_event_two = client.post(f"/api/admin/events/{event_two['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()

    confirmed_in_one, _ = confirm_registration(client, event_one["public_id"], "9111111117", "Cross Event Player")

    assign = client.post(
        f"/api/admin/teams/{team_in_event_two['id']}/members", json={"registration_id": confirmed_in_one["id"]}, headers=headers,
    )
    assert assign.status_code == 409
    assert assign.json()["error"]["code"] == "CROSS_EVENT_REGISTRATION"


def test_one_active_team_per_registration(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    team_b = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Bravo"}, headers=headers).json()
    confirmed, _ = confirm_registration(client, event["public_id"], "9111111118", "Double Assign Player")

    first = client.post(f"/api/admin/teams/{team_a['id']}/members", json={"registration_id": confirmed["id"]}, headers=headers)
    assert first.status_code == 201
    second = client.post(f"/api/admin/teams/{team_b['id']}/members", json={"registration_id": confirmed["id"]}, headers=headers)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ALREADY_ON_A_TEAM"


def test_capacity_enforced_and_full_team_fails_safely(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha", "max_size": 1}, headers=headers).json()
    first, _ = confirm_registration(client, event["public_id"], "9111111119", "Player One")
    second, _ = confirm_registration(client, event["public_id"], "9111111120", "Player Two")

    ok = client.post(f"/api/admin/teams/{team['id']}/members", json={"registration_id": first["id"]}, headers=headers)
    assert ok.status_code == 201
    full = client.post(f"/api/admin/teams/{team['id']}/members", json={"registration_id": second["id"]}, headers=headers)
    assert full.status_code == 409
    assert full.json()["error"]["code"] == "TEAM_FULL"


def test_concurrent_assignment_to_a_one_slot_team_admits_exactly_one(client: TestClient):
    """Two eligible registrations race for the last slot on a max_size=1
    team — a real ThreadPoolExecutor against the real running app, not a
    simulated race. Exactly one must win."""
    headers = login(client)
    event = new_event(client, headers)
    team = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha", "max_size": 1}, headers=headers).json()
    first, _ = confirm_registration(client, event["public_id"], "9111111121", "Racer One")
    second, _ = confirm_registration(client, event["public_id"], "9111111122", "Racer Two")

    def assign(registration_id: int):
        return client.post(f"/api/admin/teams/{team['id']}/members", json={"registration_id": registration_id}, headers=headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(assign, [first["id"], second["id"]]))

    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 409], f"expected exactly one winner, got {[r.status_code for r in results]}"

    roster = client.get(f"/api/admin/events/{event['id']}/teams", headers=headers).json()
    assert len(roster[0]["members"]) == 1


def test_move_between_teams_is_atomic(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    team_b = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Bravo"}, headers=headers).json()
    confirmed, _ = confirm_registration(client, event["public_id"], "9111111123", "Mover")

    assigned = client.post(f"/api/admin/teams/{team_a['id']}/members", json={"registration_id": confirmed["id"]}, headers=headers)
    member_id = assigned.json()["members"][0]["id"]

    moved = client.patch(f"/api/admin/team-members/{member_id}", json={"team_id": team_b["id"]}, headers=headers)
    assert moved.status_code == 200
    assert moved.json()["id"] == team_b["id"]
    assert len(moved.json()["members"]) == 1

    roster_a = client.get(f"/api/admin/events/{event['id']}/teams", headers=headers).json()
    alpha = next(t for t in roster_a if t["id"] == team_a["id"])
    assert alpha["members"] == []


def test_move_into_a_full_team_is_rejected(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    team_b = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Bravo", "max_size": 1}, headers=headers).json()
    mover, _ = confirm_registration(client, event["public_id"], "9111111124", "Mover")
    blocker, _ = confirm_registration(client, event["public_id"], "9111111125", "Blocker")

    client.post(f"/api/admin/teams/{team_b['id']}/members", json={"registration_id": blocker["id"]}, headers=headers)
    assigned = client.post(f"/api/admin/teams/{team_a['id']}/members", json={"registration_id": mover["id"]}, headers=headers)
    member_id = assigned.json()["members"][0]["id"]

    moved = client.patch(f"/api/admin/team-members/{member_id}", json={"team_id": team_b["id"]}, headers=headers)
    assert moved.status_code == 409
    assert moved.json()["error"]["code"] == "TEAM_FULL"


def test_cancelling_a_registration_removes_team_membership(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    confirmed, player_headers = confirm_registration(client, event["public_id"], "9111111126", "Cancels Later")
    client.post(f"/api/admin/teams/{team['id']}/members", json={"registration_id": confirmed["id"]}, headers=headers)

    cancel = client.post(f"/api/registrations/{confirmed['public_id']}/cancel", headers=player_headers)
    assert cancel.status_code == 200

    roster = client.get(f"/api/admin/events/{event['id']}/teams", headers=headers).json()
    assert roster[0]["members"] == []


def test_roster_locks_once_team_has_played(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    team_b = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Bravo"}, headers=headers).json()
    confirmed, _ = confirm_registration(client, event["public_id"], "9111111127", "Locked Player")
    assigned = client.post(f"/api/admin/teams/{team_a['id']}/members", json={"registration_id": confirmed["id"]}, headers=headers)
    member_id = assigned.json()["members"][0]["id"]

    client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=headers,
    )
    fixture_id = client.get(f"/api/admin/events/{event['id']}/fixtures", headers=headers).json()[0]["id"]
    client.patch(f"/api/admin/fixtures/{fixture_id}", json={"status": "IN_PROGRESS"}, headers=headers)

    remove = client.delete(f"/api/admin/team-members/{member_id}", headers=headers)
    assert remove.status_code == 409
    assert remove.json()["error"]["code"] == "TEAM_ROSTER_LOCKED"

    move = client.patch(f"/api/admin/team-members/{member_id}", json={"team_id": team_b["id"]}, headers=headers)
    assert move.status_code == 409
    assert move.json()["error"]["code"] == "TEAM_ROSTER_LOCKED"


def test_player_cannot_call_organizer_team_endpoints(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()

    # A fresh cookie jar on the same app/DB — carries only a player session,
    # never an organizer one — so this genuinely exercises "no organizer
    # session at all", not "organizer session + wrong CSRF token".
    player_client = TestClient(client.app)
    confirmed, player_headers = confirm_registration(player_client, event["public_id"], "9111111128", "Sneaky Player")

    forbidden = player_client.post(
        f"/api/admin/teams/{team['id']}/members", json={"registration_id": confirmed["id"]}, headers=player_headers,
    )
    assert forbidden.status_code == 401


@pytest.mark.skipif("SC_TEST_DATABASE_URL" not in os.environ, reason="requires a real PostgreSQL instance (set SC_TEST_DATABASE_URL) to prove DB-level enforcement, not just SQLite")
def test_database_itself_rejects_cross_event_team_membership(client: TestClient):
    """Bypasses services.assign_team_member entirely and inserts a raw
    TeamMember row linking a team and a registration from two different
    events, proving the composite foreign keys — not just the Python check
    in assign_team_member — make this impossible. Real PostgreSQL only:
    this is exactly the constraint enforcement that must not be faked."""
    from sqlalchemy.exc import IntegrityError

    from backend.app.models import TeamMember

    headers = login(client)
    event_one = new_event(client, headers, name="Event One")
    event_two = new_event(client, headers, name="Event Two")
    confirmed_in_one, _ = confirm_registration(client, event_one["public_id"], "9111111129", "DB Level Player")
    team_in_two = client.post(f"/api/admin/events/{event_two['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()

    with client.app.state.session_factory() as session:
        session.add(TeamMember(
            team_id=team_in_two["id"], registration_id=confirmed_in_one["id"], event_id=event_two["id"],
        ))
        with pytest.raises(IntegrityError):
            session.commit()
