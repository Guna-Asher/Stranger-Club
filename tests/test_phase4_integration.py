"""Phase 4 — end-to-end integration: Event -> confirmed registrations ->
teams -> team membership -> matches, plus realtime notifications for a
representative mutation. Runs against SQLite by default; set
SC_TEST_DATABASE_URL to also run the full path against real PostgreSQL, same
convention as test_api.py.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_api import client, login, new_event
from tests.test_teams import confirm_registration

__all__ = ["client"]


def test_full_event_to_teams_to_matches_journey(client: TestClient):
    headers = login(client)
    event = new_event(client, headers, capacity=4, name="Sunday Stranger Cricket")

    # Existing Phase 1-3 journey, untouched: two players register, pay, and
    # get confirmed by the organizer. Each player gets its own TestClient
    # (same app/DB, separate cookie jar) — a single shared cookie jar can
    # only hold one player session at a time, and this test needs to check
    # each player's own view independently.
    player_one_client, player_two_client = TestClient(client.app), TestClient(client.app)
    player_one, player_one_headers = confirm_registration(player_one_client, event["public_id"], "9222222221", "Asha")
    player_two, player_two_headers = confirm_registration(player_two_client, event["public_id"], "9222222222", "Bilal")
    assert player_one["status"] == "CONFIRMED"
    assert player_two["status"] == "CONFIRMED"

    # Organizer creates teams and assigns confirmed players.
    team_a = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    team_b = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Bravo"}, headers=headers).json()
    assign_one = client.post(f"/api/admin/teams/{team_a['id']}/members", json={"registration_id": player_one["id"]}, headers=headers)
    assign_two = client.post(f"/api/admin/teams/{team_b['id']}/members", json={"registration_id": player_two["id"]}, headers=headers)
    assert assign_one.status_code == 201
    assert assign_two.status_code == 201

    # Organizer schedules a match between the two teams.
    fixture = client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00", "sequence": 1},
        headers=headers,
    )
    assert fixture.status_code == 201, fixture.json()
    fixture_id = fixture.json()["id"]

    # Player one sees their team and their upcoming match.
    my_team = player_one_client.get(f"/api/events/{event['public_id']}/my-team", headers=player_one_headers)
    assert my_team.status_code == 200
    assert my_team.json()["team"]["id"] == team_a["id"]
    assert my_team.json()["teammates"] == []  # sole member of Team Alpha

    my_fixtures = player_one_client.get(f"/api/events/{event['public_id']}/fixtures", headers=player_one_headers)
    assert my_fixtures.status_code == 200
    assert len(my_fixtures.json()) == 1
    assert my_fixtures.json()[0]["my_team_id"] == team_a["id"]
    assert my_fixtures.json()[0]["team_b"]["id"] == team_b["id"]

    # A player never assigned to any team sees no team, but still sees the
    # event's match schedule (with no highlighted "my team").
    player_three_client = TestClient(client.app)
    player_three, player_three_headers = confirm_registration(player_three_client, event["public_id"], "9222222223", "Chetan")
    unassigned_team = player_three_client.get(f"/api/events/{event['public_id']}/my-team", headers=player_three_headers)
    assert unassigned_team.json() == {"team": None, "teammates": []}
    unassigned_fixtures = player_three_client.get(f"/api/events/{event['public_id']}/fixtures", headers=player_three_headers)
    assert unassigned_fixtures.json()[0]["my_team_id"] is None

    # Organizer runs the match through to completion.
    started = client.patch(f"/api/admin/fixtures/{fixture_id}", json={"status": "IN_PROGRESS"}, headers=headers)
    assert started.status_code == 200
    completed = client.patch(f"/api/admin/fixtures/{fixture_id}", json={"status": "COMPLETED"}, headers=headers)
    assert completed.status_code == 200

    # The played teams' rosters are now frozen — a durable historical fact,
    # not overbuilt into a snapshot table (see Fixture's docstring).
    locked = client.delete(f"/api/admin/team-members/{assign_one.json()['members'][0]['id']}", headers=headers)
    assert locked.status_code == 409
    assert locked.json()["error"]["code"] == "TEAM_ROSTER_LOCKED"

    # The original registration/payment domain is unaffected by any of this.
    status = player_one_client.get(f"/api/registrations/{player_one['public_id']}", headers=player_one_headers)
    assert status.json()["status"] == "CONFIRMED"
    assert status.json()["payment"]["status"] == "VERIFIED"


def test_team_and_fixture_mutations_publish_realtime_notifications(client: TestClient):
    """Same style as the existing realtime tests: subscribe to the
    broadcaster directly and confirm a representative team/match mutation
    actually publishes — not a screenshot-only claim. Reconnect/RESYNC
    safety itself is Phase 3's concern and isn't re-derived here."""
    headers = login(client)
    event = new_event(client, headers)
    broadcaster = client.app.state.broadcaster
    channel = broadcaster.subscribe(event["public_id"])
    try:
        team = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
        created_message = channel.get(timeout=2)
        assert created_message["type"] == "TEAM_CREATED"

        team_b = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Bravo"}, headers=headers).json()
        channel.get(timeout=2)  # drain TEAM_CREATED for team_b

        fixture = client.post(
            f"/api/admin/events/{event['id']}/fixtures",
            json={"team_a_id": team["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00"},
            headers=headers,
        ).json()
        fixture_message = channel.get(timeout=2)
        assert fixture_message["type"] == "FIXTURE_CREATED"

        client.patch(f"/api/admin/fixtures/{fixture['id']}", json={"status": "IN_PROGRESS"}, headers=headers)
        status_message = channel.get(timeout=2)
        assert status_message["type"] == "FIXTURE_STATUS_CHANGED"
    finally:
        broadcaster.unsubscribe(event["public_id"], channel)
