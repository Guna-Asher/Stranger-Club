"""Phase 5 — MatchResult: completed-only, one-per-fixture, winner
consistency, corrections, ownership, and IDOR-style security cases.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from tests.test_api import client, create_second_organizer, login, make_platform_admin, new_event
from tests.test_teams import confirm_registration

__all__ = ["client"]


def make_completed_match_with_participants(client: TestClient, headers: dict):
    """A fully set-up event: two confirmed players, two teams, one
    completed match, both players recorded as participants. Returns
    (event, fixture, team_a, team_b, reg1, reg2)."""
    event = new_event(client, headers)
    team_a = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    team_b = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Bravo"}, headers=headers).json()
    reg1, _ = confirm_registration(client, event["public_id"], "9400000001", "Player One")
    reg2, _ = confirm_registration(client, event["public_id"], "9400000002", "Player Two")
    client.post(f"/api/admin/teams/{team_a['id']}/members", json={"registration_id": reg1["id"]}, headers=headers)
    client.post(f"/api/admin/teams/{team_b['id']}/members", json={"registration_id": reg2["id"]}, headers=headers)
    fixture = client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=headers,
    ).json()
    client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"status": "IN_PROGRESS"}, headers=headers)
    client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"status": "COMPLETED"}, headers=headers)
    client.put(
        f"/api/admin/fixtures/{fixture['id']}/participants",
        json=[{"registration_id": reg1["id"], "team_id": team_a["id"]}, {"registration_id": reg2["id"], "team_id": team_b["id"]}],
        headers=headers,
    )
    return event, fixture, team_a, team_b, reg1, reg2


def test_result_rejected_for_scheduled_fixture(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    team_b = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Bravo"}, headers=headers).json()
    fixture = client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=headers,
    ).json()
    response = client.post(f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "DRAW"}, headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FIXTURE_NOT_COMPLETED"


def test_result_rejected_for_in_progress_fixture(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    team_b = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Bravo"}, headers=headers).json()
    fixture = client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=headers,
    ).json()
    client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"status": "IN_PROGRESS"}, headers=headers)
    response = client.post(f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "DRAW"}, headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FIXTURE_NOT_COMPLETED"


def test_result_rejected_for_cancelled_fixture(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    team_b = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Bravo"}, headers=headers).json()
    fixture = client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=headers,
    ).json()
    client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"status": "CANCELLED"}, headers=headers)
    response = client.post(f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "NO_RESULT"}, headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FIXTURE_NOT_COMPLETED"


def test_team_a_win_requires_team_a_as_winner(client: TestClient):
    headers = login(client)
    _, fixture, team_a, team_b, _, _ = make_completed_match_with_participants(client, headers)
    wrong = client.post(
        f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "TEAM_A_WIN", "winning_team_id": team_b["id"]}, headers=headers,
    )
    assert wrong.status_code == 422
    right = client.post(
        f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "TEAM_A_WIN", "winning_team_id": team_a["id"]}, headers=headers,
    )
    assert right.status_code == 201
    assert right.json()["winning_team"]["id"] == team_a["id"]


def test_win_result_requires_a_winner(client: TestClient):
    headers = login(client)
    _, fixture, team_a, _, _, _ = make_completed_match_with_participants(client, headers)
    missing_winner = client.post(f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "TEAM_A_WIN"}, headers=headers)
    assert missing_winner.status_code == 422


def test_draw_and_no_result_have_null_winner(client: TestClient):
    headers = login(client)
    _, fixture, team_a, _, _, _ = make_completed_match_with_participants(client, headers)
    draw_with_winner = client.post(
        f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "DRAW", "winning_team_id": team_a["id"]}, headers=headers,
    )
    assert draw_with_winner.status_code == 422

    draw = client.post(f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "DRAW"}, headers=headers)
    assert draw.status_code == 201
    assert draw.json()["winning_team"] is None


def test_one_result_per_fixture(client: TestClient):
    headers = login(client)
    _, fixture, team_a, _, _, _ = make_completed_match_with_participants(client, headers)
    first = client.post(f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "TEAM_A_WIN", "winning_team_id": team_a["id"]}, headers=headers)
    assert first.status_code == 201
    second = client.post(f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "DRAW"}, headers=headers)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "RESULT_ALREADY_EXISTS"


def test_result_update_allows_correction(client: TestClient):
    headers = login(client)
    _, fixture, team_a, team_b, reg1, reg2 = make_completed_match_with_participants(client, headers)
    client.post(f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "TEAM_A_WIN", "winning_team_id": team_a["id"], "player_of_match_registration_id": reg1["id"]}, headers=headers)

    updated = client.patch(
        f"/api/admin/fixtures/{fixture['id']}/result",
        json={"result_type": "TEAM_B_WIN", "winning_team_id": team_b["id"], "player_of_match_registration_id": reg2["id"], "notes": "Corrected after review"},
        headers=headers,
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["result_type"] == "TEAM_B_WIN"
    assert body["winning_team"]["id"] == team_b["id"]
    assert body["player_of_match"]["registration_id"] == reg2["id"]
    assert body["notes"] == "Corrected after review"

    fetched = client.get(f"/api/admin/fixtures/{fixture['id']}/result", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["result_type"] == "TEAM_B_WIN"


def test_get_result_404_before_any_created(client: TestClient):
    headers = login(client)
    _, fixture, _, _, _, _ = make_completed_match_with_participants(client, headers)
    response = client.get(f"/api/admin/fixtures/{fixture['id']}/result", headers=headers)
    assert response.status_code == 404


def test_organizer_cannot_touch_another_organizers_result(client: TestClient):
    headers = login(client)
    _, fixture, team_a, _, _, _ = make_completed_match_with_participants(client, headers)
    client.post(f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "TEAM_A_WIN", "winning_team_id": team_a["id"]}, headers=headers)

    other_client, other_headers = create_second_organizer(client)
    forbidden_get = other_client.get(f"/api/admin/fixtures/{fixture['id']}/result", headers=other_headers)
    assert forbidden_get.status_code == 404
    forbidden_update = other_client.patch(f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "DRAW"}, headers=other_headers)
    assert forbidden_update.status_code == 404
    forbidden_create_on_other_fixture = other_client.post(f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "DRAW"}, headers=other_headers)
    assert forbidden_create_on_other_fixture.status_code == 404


def test_platform_admin_can_manage_any_organizers_result(client: TestClient):
    headers = login(client)
    _, fixture, team_a, _, _, _ = make_completed_match_with_participants(client, headers)

    other_client, other_headers = create_second_organizer(client, username="platform_admin_results", password="correct-horse-9")
    make_platform_admin(client, username="platform_admin_results")
    allowed = other_client.post(f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "TEAM_A_WIN", "winning_team_id": team_a["id"]}, headers=other_headers)
    assert allowed.status_code == 201, allowed.json()


def test_player_cannot_create_or_update_result(client: TestClient):
    headers = login(client)
    _, fixture, team_a, _, _, _ = make_completed_match_with_participants(client, headers)

    player_client = TestClient(client.app)
    forbidden = player_client.post(f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "TEAM_A_WIN", "winning_team_id": team_a["id"]})
    assert forbidden.status_code == 401


def test_award_must_be_a_participant_of_this_fixture(client: TestClient):
    headers = login(client)
    event, fixture, team_a, team_b, reg1, reg2 = make_completed_match_with_participants(client, headers)
    # a third confirmed-but-not-participating player cannot be awarded
    reg3, _ = confirm_registration(client, event["public_id"], "9400000003", "Player Three")
    client.post(f"/api/admin/teams/{team_a['id']}/members", json={"registration_id": reg3["id"]}, headers=headers)

    response = client.post(
        f"/api/admin/fixtures/{fixture['id']}/result",
        json={"result_type": "TEAM_A_WIN", "winning_team_id": team_a["id"], "best_batter_registration_id": reg3["id"]},
        headers=headers,
    )
    assert response.status_code == 422


@pytest.mark.skipif("SC_TEST_DATABASE_URL" not in os.environ, reason="requires a real PostgreSQL instance (set SC_TEST_DATABASE_URL) to prove DB-level enforcement, not just SQLite")
def test_database_itself_rejects_a_non_participant_award(client: TestClient):
    """Bypasses services.create_match_result entirely and inserts a raw
    MatchResult row awarding MVP to a registration with no MatchParticipant
    row for this fixture — proving the composite FK (not just the Python
    check above) makes this impossible."""
    from sqlalchemy.exc import IntegrityError

    from backend.app.models import MatchResult

    headers = login(client)
    event, fixture, team_a, team_b, reg1, reg2 = make_completed_match_with_participants(client, headers)
    reg3, _ = confirm_registration(client, event["public_id"], "9400000004", "Player Four")

    with client.app.state.session_factory() as session:
        session.add(MatchResult(
            fixture_id=fixture["id"], event_id=event["id"], team_a_id=team_a["id"], team_b_id=team_b["id"],
            winning_team_id=team_a["id"], result_type="TEAM_A_WIN", player_of_match_registration_id=reg3["id"],
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_concurrent_result_creation_produces_exactly_one_result(client: TestClient):
    """Two organizer requests racing to create a result for the same
    fixture must never create two rows — the DB's own UniqueConstraint
    (fixture_id) is the backstop, same pattern as create_team's name race."""
    headers = login(client)
    _, fixture, team_a, team_b, _, _ = make_completed_match_with_participants(client, headers)

    def create(winner_id):
        with client.app.state.session_factory() as session:
            from backend.app.deps import token_hash
            from backend.app.models import Fixture, Organizer, OrganizerSession
            from backend.app.schemas import MatchResultCreate
            from backend.app.services import create_match_result
            organizer = session.query(Organizer).filter_by(username="organizer").one()
            fx = session.get(Fixture, fixture["id"])
            payload = MatchResultCreate(result_type="TEAM_A_WIN", winning_team_id=winner_id)
            try:
                create_match_result(session, fx, payload, organizer)
                return "created"
            except Exception as e:
                return f"error:{type(e).__name__}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, [team_a["id"], team_a["id"]]))

    assert results.count("created") == 1, results
    check = client.get(f"/api/admin/fixtures/{fixture['id']}/result", headers=headers)
    assert check.status_code == 200


def test_concurrent_update_and_participant_removal_never_leaks_a_raw_error(client: TestClient):
    """A racing set_match_participants that removes the very registration an
    in-flight update_match_result is about to award must never surface as an
    unhandled IntegrityError (500) — it must resolve to a clean, expected
    application error (or a clean success, if the update wins the race),
    regardless of which side the Fixture-row lock lets through first.
    Reproduces the exact interleaving update_match_result's lock now
    prevents from ever reaching the database in a broken state."""
    import threading

    from backend.app.models import Fixture, MatchResult, Organizer
    from backend.app.schemas import MatchParticipantEntry, MatchResultUpdate
    from backend.app.services import set_match_participants, update_match_result

    headers = login(client)
    _, fixture, team_a, team_b, reg1, reg2 = make_completed_match_with_participants(client, headers)
    client.post(
        f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "TEAM_A_WIN", "winning_team_id": team_a["id"], "player_of_match_registration_id": reg1["id"]}, headers=headers,
    )

    barrier = threading.Barrier(2)
    outcomes = []

    def award_reg2_as_mvp():
        barrier.wait()
        with client.app.state.session_factory() as session:
            organizer = session.query(Organizer).filter_by(username="organizer").one()
            fx = session.get(Fixture, fixture["id"])
            result = session.query(MatchResult).filter_by(fixture_id=fixture["id"]).one()
            payload = MatchResultUpdate(result_type="TEAM_A_WIN", winning_team_id=team_a["id"], player_of_match_registration_id=reg2["id"])
            try:
                update_match_result(session, result, fx, payload, organizer)
                outcomes.append(("update", "success"))
            except Exception as e:
                outcomes.append(("update", type(e).__name__, getattr(e, "status_code", None)))

    def remove_reg2_from_participants():
        barrier.wait()
        with client.app.state.session_factory() as session:
            organizer = session.query(Organizer).filter_by(username="organizer").one()
            fx = session.get(Fixture, fixture["id"])
            try:
                set_match_participants(session, fx, [MatchParticipantEntry(registration_id=reg1["id"], team_id=team_a["id"])], organizer)
                outcomes.append(("remove", "success"))
            except Exception as e:
                outcomes.append(("remove", type(e).__name__, getattr(e, "status_code", None)))

    threads = [threading.Thread(target=award_reg2_as_mvp), threading.Thread(target=remove_reg2_from_participants)]
    for t in threads: t.start()
    for t in threads: t.join()

    for outcome in outcomes:
        if len(outcome) == 3:
            # Any raised error must be the application's own clean HTTPException
            # (422/409), never a bare, unhandled IntegrityError.
            assert outcome[1] == "HTTPException", f"leaked a raw error instead of a clean one: {outcome}"
            assert outcome[2] in (409, 422), f"unexpected status code: {outcome}"

    # Whichever order won, the database must be left in a self-consistent
    # state: refetching the result must not raise (the CHECK/FK constraints
    # were never violated in what actually got committed).
    final = client.get(f"/api/admin/fixtures/{fixture['id']}/result", headers=headers)
    assert final.status_code == 200
