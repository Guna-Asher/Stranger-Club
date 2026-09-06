"""Event (Match) upcoming/past classification is driven solely by
Match.status == 'COMPLETED' — never by an individual Fixture completing, and
never by date. See services.VALID_EVENT_TRANSITIONS: nothing in the
Fixture/MatchResult write path ever touches Match.status, and the public
/api/events listing (the landing page's only data source, per Landing.jsx)
already filters by Match.status alone.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_api import client, login, new_event
from tests.test_match_results import make_completed_match_with_participants

__all__ = ["client"]


def test_completing_one_fixture_does_not_complete_the_event(client: TestClient):
    """Event -> Match 1 COMPLETED, Match 2 SCHEDULED: the event itself must
    remain whatever status it already had — never auto-promoted to COMPLETED
    just because one of its fixtures finished."""
    headers = login(client)
    event, fixture, team_a, team_b, reg1, reg2 = make_completed_match_with_participants(client, headers)
    # A second, still-SCHEDULED fixture on the same event.
    client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-03T08:00:00"},
        headers=headers,
    )
    client.post(
        f"/api/admin/fixtures/{fixture['id']}/result", json={"result_type": "TEAM_A_WIN", "winning_team_id": team_a["id"]}, headers=headers,
    )
    reloaded = client.get(f"/api/admin/events/{event['id']}", headers=headers).json()
    assert reloaded["status"] not in ("COMPLETED", "CANCELLED")


def test_event_moves_to_history_only_once_its_own_status_is_completed(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    # OPEN -> ONGOING -> COMPLETED, the event's own lifecycle, independent of any fixture.
    client.patch(f"/api/admin/events/{event['id']}", json={"status": "ONGOING"}, headers=headers)
    still_current = client.get(f"/api/admin/events/{event['id']}", headers=headers).json()
    assert still_current["status"] == "ONGOING"

    client.patch(f"/api/admin/events/{event['id']}", json={"status": "COMPLETED"}, headers=headers)
    completed = client.get(f"/api/admin/events/{event['id']}", headers=headers).json()
    assert completed["status"] == "COMPLETED"


def test_public_events_excludes_draft_completed_and_cancelled(client: TestClient):
    """The landing page's only data source (GET /api/events) must never
    surface a non-joinable event — no separate frontend filtering should be
    needed for this."""
    headers = login(client)
    open_event = new_event(client, headers, name="Open For Landing")

    draft_event = client.post("/api/admin/events", headers=headers, json={
        "name": "Still Draft", "date": "2026-10-02", "start_time": "07:00:00", "end_time": "10:00:00",
        "venue": "Ground", "capacity": 10, "fee": 100,
        "registration_deadline": "2026-10-01T20:00:00", "upi_id": "strangerclub@upi", "status": "DRAFT",
    }).json()

    cancelled_event = new_event(client, headers, name="Later Cancelled")
    client.patch(f"/api/admin/events/{cancelled_event['id']}", json={"status": "CANCELLED"}, headers=headers)

    completed_event = new_event(client, headers, name="Later Completed")
    client.patch(f"/api/admin/events/{completed_event['id']}", json={"status": "ONGOING"}, headers=headers)
    client.patch(f"/api/admin/events/{completed_event['id']}", json={"status": "COMPLETED"}, headers=headers)

    public_names = {item["name"] for item in client.get("/api/events").json()}
    assert "Open For Landing" in public_names
    assert "Still Draft" not in public_names
    assert "Later Cancelled" not in public_names
    assert "Later Completed" not in public_names
