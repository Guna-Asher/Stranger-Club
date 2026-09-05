"""Phase 5 — MatchParticipant: eligibility, duplicate/cross-event/wrong-team
rejection, transactional set-replace, and IDOR-style security cases.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from tests.test_api import client, create_second_organizer, login, new_event
from tests.test_teams import confirm_registration

__all__ = ["client"]


def make_completed_two_team_match(client: TestClient, headers: dict):
    event = new_event(client, headers)
    team_a = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    team_b = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Bravo"}, headers=headers).json()
    reg1, _ = confirm_registration(client, event["public_id"], "9500000001", "Player One")
    reg2, _ = confirm_registration(client, event["public_id"], "9500000002", "Player Two")
    client.post(f"/api/admin/teams/{team_a['id']}/members", json={"registration_id": reg1["id"]}, headers=headers)
    client.post(f"/api/admin/teams/{team_b['id']}/members", json={"registration_id": reg2["id"]}, headers=headers)
    fixture = client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=headers,
    ).json()
    client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"status": "IN_PROGRESS"}, headers=headers)
    client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"status": "COMPLETED"}, headers=headers)
    return event, fixture, team_a, team_b, reg1, reg2


def test_set_participants_happy_path(client: TestClient):
    headers = login(client)
    _, fixture, team_a, team_b, reg1, reg2 = make_completed_two_team_match(client, headers)
    response = client.put(
        f"/api/admin/fixtures/{fixture['id']}/participants",
        json=[{"registration_id": reg1["id"], "team_id": team_a["id"]}, {"registration_id": reg2["id"], "team_id": team_b["id"]}],
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert {p["registration_id"] for p in body} == {reg1["id"], reg2["id"]}

    listed = client.get(f"/api/admin/fixtures/{fixture['id']}/participants", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 2


def test_participation_requires_completed_fixture(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    team_b = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Bravo"}, headers=headers).json()
    reg1, _ = confirm_registration(client, event["public_id"], "9500000003", "Scheduled Player")
    client.post(f"/api/admin/teams/{team_a['id']}/members", json={"registration_id": reg1["id"]}, headers=headers)
    fixture = client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=headers,
    ).json()
    response = client.put(
        f"/api/admin/fixtures/{fixture['id']}/participants", json=[{"registration_id": reg1["id"], "team_id": team_a["id"]}], headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FIXTURE_NOT_COMPLETED"


def test_wrong_team_participant_rejected(client: TestClient):
    headers = login(client)
    _, fixture, team_a, team_b, reg1, reg2 = make_completed_two_team_match(client, headers)
    response = client.put(
        f"/api/admin/fixtures/{fixture['id']}/participants", json=[{"registration_id": reg1["id"], "team_id": team_b["id"]}], headers=headers,
    )
    assert response.status_code == 422


def test_cross_event_participant_rejected(client: TestClient):
    headers = login(client)
    _, fixture, team_a, team_b, reg1, reg2 = make_completed_two_team_match(client, headers)
    other_event = new_event(client, headers, name="Other Event")
    outside_reg, _ = confirm_registration(client, other_event["public_id"], "9500000004", "Outsider")
    response = client.put(
        f"/api/admin/fixtures/{fixture['id']}/participants", json=[{"registration_id": outside_reg["id"], "team_id": team_a["id"]}], headers=headers,
    )
    assert response.status_code == 422


def test_duplicate_registration_in_same_request_rejected(client: TestClient):
    headers = login(client)
    _, fixture, team_a, team_b, reg1, reg2 = make_completed_two_team_match(client, headers)
    response = client.put(
        f"/api/admin/fixtures/{fixture['id']}/participants",
        json=[{"registration_id": reg1["id"], "team_id": team_a["id"]}, {"registration_id": reg1["id"], "team_id": team_a["id"]}],
        headers=headers,
    )
    assert response.status_code == 422


def test_set_participants_is_transactional_replace(client: TestClient):
    headers = login(client)
    _, fixture, team_a, team_b, reg1, reg2 = make_completed_two_team_match(client, headers)
    client.put(
        f"/api/admin/fixtures/{fixture['id']}/participants",
        json=[{"registration_id": reg1["id"], "team_id": team_a["id"]}, {"registration_id": reg2["id"], "team_id": team_b["id"]}],
        headers=headers,
    )
    # Replace with only reg1 — reg2 should be cleanly removed, not left dangling.
    replaced = client.put(
        f"/api/admin/fixtures/{fixture['id']}/participants", json=[{"registration_id": reg1["id"], "team_id": team_a["id"]}], headers=headers,
    )
    assert replaced.status_code == 200
    assert len(replaced.json()) == 1
    assert replaced.json()[0]["registration_id"] == reg1["id"]


def test_cannot_remove_participant_who_is_an_award_recipient(client: TestClient):
    headers = login(client)
    _, fixture, team_a, team_b, reg1, reg2 = make_completed_two_team_match(client, headers)
    client.put(
        f"/api/admin/fixtures/{fixture['id']}/participants",
        json=[{"registration_id": reg1["id"], "team_id": team_a["id"]}, {"registration_id": reg2["id"], "team_id": team_b["id"]}],
        headers=headers,
    )
    client.post(
        f"/api/admin/fixtures/{fixture['id']}/result",
        json={"result_type": "TEAM_A_WIN", "winning_team_id": team_a["id"], "player_of_match_registration_id": reg1["id"]},
        headers=headers,
    )
    blocked = client.put(
        f"/api/admin/fixtures/{fixture['id']}/participants", json=[{"registration_id": reg2["id"], "team_id": team_b["id"]}], headers=headers,
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "PARTICIPANT_IS_AWARD_RECIPIENT"


def test_organizer_ownership_enforced_for_participants(client: TestClient):
    headers = login(client)
    _, fixture, team_a, team_b, reg1, reg2 = make_completed_two_team_match(client, headers)
    other_client, other_headers = create_second_organizer(client)
    forbidden = other_client.put(
        f"/api/admin/fixtures/{fixture['id']}/participants", json=[{"registration_id": reg1["id"], "team_id": team_a["id"]}], headers=other_headers,
    )
    assert forbidden.status_code == 404
    forbidden_list = other_client.get(f"/api/admin/fixtures/{fixture['id']}/participants", headers=other_headers)
    assert forbidden_list.status_code == 404


def test_player_cannot_set_participants(client: TestClient):
    headers = login(client)
    _, fixture, team_a, team_b, reg1, reg2 = make_completed_two_team_match(client, headers)
    player_client = TestClient(client.app)
    forbidden = player_client.put(
        f"/api/admin/fixtures/{fixture['id']}/participants", json=[{"registration_id": reg1["id"], "team_id": team_a["id"]}],
    )
    assert forbidden.status_code == 401


def test_concurrent_duplicate_participant_writes_produce_one_row(client: TestClient):
    """Two concurrent PUTs with the same single entry must never leave two
    MatchParticipant rows — the Fixture row lock inside set_match_participants
    serializes them, and the DB unique constraint is the backstop either way."""
    headers = login(client)
    _, fixture, team_a, team_b, reg1, reg2 = make_completed_two_team_match(client, headers)

    def set_once(_):
        return client.put(
            f"/api/admin/fixtures/{fixture['id']}/participants", json=[{"registration_id": reg1["id"], "team_id": team_a["id"]}], headers=headers,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(set_once, range(4)))
    assert all(r.status_code == 200 for r in results), [r.json() for r in results]

    final = client.get(f"/api/admin/fixtures/{fixture['id']}/participants", headers=headers)
    assert len(final.json()) == 1


@pytest.mark.skipif("SC_TEST_DATABASE_URL" not in os.environ, reason="requires a real PostgreSQL instance (set SC_TEST_DATABASE_URL) to prove DB-level enforcement, not just SQLite")
def test_database_itself_rejects_wrong_team_participant(client: TestClient):
    """Bypasses services.set_match_participants and inserts a raw
    MatchParticipant row for a registration on a team it was never actually
    assigned to — proving the composite FK to team_members (not just the
    Python check) makes this impossible."""
    from sqlalchemy.exc import IntegrityError

    from backend.app.models import MatchParticipant

    headers = login(client)
    _, fixture, team_a, team_b, reg1, reg2 = make_completed_two_team_match(client, headers)

    with client.app.state.session_factory() as session:
        session.add(MatchParticipant(fixture_id=fixture["id"], registration_id=reg1["id"], team_id=team_b["id"], event_id=fixture["event_id"]))
        with pytest.raises(IntegrityError):
            session.commit()
