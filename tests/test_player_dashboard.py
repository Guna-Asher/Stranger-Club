"""GET /api/player/matches — the persistent player Profile page's cross-event
summary (see services.player_dashboard): active registrations, upcoming
fixtures, and completed fixtures with results, batched across every event
the player has ever registered for.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_api import client, login, new_event
from tests.test_match_results import make_completed_match_with_participants
from tests.test_teams import confirm_registration

__all__ = ["client"]


def player_client_for(client: TestClient, phone: str) -> tuple[TestClient, dict]:
    """A fresh cookie jar authenticated as an *already-registered* phone
    number — mirrors a player revisiting the site later, independent of
    whatever session the shared `client` currently holds (see
    make_completed_match_with_participants, which logs two different
    players in sequence on the same shared client)."""
    player = TestClient(client.app)
    requested = player.post("/api/player/otp/request", json={"phone": phone})
    assert requested.status_code == 200, requested.json()
    code = player.app.state.otp_provider.last_code_for(phone)
    verified = player.post("/api/player/otp/verify", json={"phone": phone, "code": code})
    assert verified.status_code == 200, verified.json()
    return player, {"X-CSRF-Token": verified.json()["csrf_token"]}


def test_player_matches_requires_authentication(client: TestClient):
    assert client.get("/api/player/matches").status_code == 401


def test_player_matches_lists_own_registration_and_team(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    reg, player_headers = confirm_registration(client, event["public_id"], "9455500001", "Asher")
    team = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    client.post(f"/api/admin/teams/{team['id']}/members", json={"registration_id": reg["id"]}, headers=headers)

    player, player_headers = player_client_for(client, "9455500001")
    body = player.get("/api/player/matches", headers=player_headers).json()
    assert len(body["registrations"]) == 1
    entry = body["registrations"][0]
    assert entry["public_id"] == reg["public_id"]
    assert entry["status"] == "CONFIRMED"
    assert entry["event"]["public_id"] == event["public_id"]
    assert entry["team"]["id"] == team["id"]


def test_player_matches_shows_upcoming_fixture(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    reg1, _ = confirm_registration(client, event["public_id"], "9455500011", "Player One")
    reg2, _ = confirm_registration(client, event["public_id"], "9455500012", "Player Two")
    team_a = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    team_b = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Bravo"}, headers=headers).json()
    client.post(f"/api/admin/teams/{team_a['id']}/members", json={"registration_id": reg1["id"]}, headers=headers)
    client.post(f"/api/admin/teams/{team_b['id']}/members", json={"registration_id": reg2["id"]}, headers=headers)
    client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=headers,
    )

    player, player_headers = player_client_for(client, "9455500011")
    body = player.get("/api/player/matches", headers=player_headers).json()
    assert len(body["upcoming"]) == 1
    assert body["upcoming"][0]["status"] == "SCHEDULED"
    assert body["upcoming"][0]["opponent"]["id"] == team_b["id"]
    assert body["completed"] == []


def test_player_matches_shows_completed_result_from_each_players_perspective(client: TestClient):
    headers = login(client)
    event, fixture, team_a, team_b, reg1, reg2 = make_completed_match_with_participants(client, headers)
    client.post(
        f"/api/admin/fixtures/{fixture['id']}/result",
        json={"result_type": "TEAM_A_WIN", "winning_team_id": team_a["id"], "player_of_match_registration_id": reg1["id"]},
        headers=headers,
    )

    winner, winner_headers = player_client_for(client, "9400000001")
    winner_body = winner.get("/api/player/matches", headers=winner_headers).json()
    assert len(winner_body["completed"]) == 1
    win_entry = winner_body["completed"][0]
    assert win_entry["result_type"] == "TEAM_A_WIN"
    assert win_entry["winning_team"]["id"] == team_a["id"]
    assert win_entry["opponent"]["id"] == team_b["id"]
    assert win_entry["player_of_match_name"] == "Player One"
    assert win_entry["participated"] is True
    assert winner_body["upcoming"] == []

    loser, loser_headers = player_client_for(client, "9400000002")
    loser_body = loser.get("/api/player/matches", headers=loser_headers).json()
    assert len(loser_body["completed"]) == 1
    lose_entry = loser_body["completed"][0]
    assert lose_entry["winning_team"]["id"] == team_a["id"]
    assert lose_entry["opponent"]["id"] == team_a["id"]
    assert lose_entry["participated"] is True


def test_player_matches_never_leaks_another_players_data(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    reg1, _ = confirm_registration(client, event["public_id"], "9455500021", "Player One")
    confirm_registration(client, event["public_id"], "9455500022", "Player Two")

    player2, player2_headers = player_client_for(client, "9455500022")
    body = player2.get("/api/player/matches", headers=player2_headers).json()
    assert [r["public_id"] for r in body["registrations"]] != [reg1["public_id"]]
    assert reg1["public_id"] not in [r["public_id"] for r in body["registrations"]]
