"""Phase 4 — Team domain: CRUD, ownership isolation, and safe deletion.

Reuses the shared client/login/registration/payment helpers from test_api.py
rather than re-declaring ~100 lines of fixture boilerplate a third time.
Set SC_TEST_DATABASE_URL to also run this file against PostgreSQL, exactly
like test_api.py.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_api import client, create_second_organizer, image_file, login, make_platform_admin, new_event
from backend.app.services import review_payment

__all__ = ["client"]


def confirm_registration(client: TestClient, event_public_id: str, phone: str, name: str) -> dict:
    requested = client.post("/api/player/otp/request", json={"phone": phone})
    assert requested.status_code == 200, requested.json()
    code = client.app.state.otp_provider.last_code_for(phone)
    verified = client.post("/api/player/otp/verify", json={"phone": phone, "code": code})
    assert verified.status_code == 200, verified.json()
    player_headers = {"X-CSRF-Token": verified.json()["csrf_token"]}
    response = client.post(f"/api/events/{event_public_id}/registrations", json={"name": name}, headers=player_headers)
    assert response.status_code == 201, response.json()
    registration = response.json()
    upload = client.post(f"/api/registrations/{registration['public_id']}/payment", files=image_file(), headers=player_headers)
    assert upload.status_code == 200, upload.json()
    with client.app.state.session_factory() as session:
        review_payment(session, upload.json()["payment"]["id"], approve=True, actor_id=1)
    status = client.get(f"/api/registrations/{registration['public_id']}", headers=player_headers)
    return status.json(), player_headers


def test_create_and_list_teams(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    created = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha", "short_code": "A"}, headers=headers)
    assert created.status_code == 201, created.json()
    body = created.json()
    assert body["name"] == "Team Alpha"
    assert body["member_count"] == 0

    listed = client.get(f"/api/admin/events/{event['id']}/teams", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["members"] == []


def test_duplicate_team_name_rejected(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    first = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers)
    assert first.status_code == 201
    second = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "TEAM_NAME_TAKEN"


def test_rename_team(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    renamed = client.patch(f"/api/admin/teams/{team['id']}", json={"name": "Team Bravo"}, headers=headers)
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Team Bravo"


def test_organizer_cannot_see_or_touch_another_organizers_team(client: TestClient):
    """IDOR: organizer B must get 404, not 403 — and never learn the team exists."""
    headers = login(client)
    event = new_event(client, headers)
    team = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()

    other_client, other_headers = create_second_organizer(client)
    forbidden_list = other_client.get(f"/api/admin/events/{event['id']}/teams", headers=other_headers)
    assert forbidden_list.status_code == 404
    forbidden_get = other_client.patch(f"/api/admin/teams/{team['id']}", json={"name": "Hijacked"}, headers=other_headers)
    assert forbidden_get.status_code == 404
    forbidden_create = other_client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team X"}, headers=other_headers)
    assert forbidden_create.status_code == 404


def test_platform_admin_can_manage_any_organizers_team(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()

    other_client, other_headers = create_second_organizer(client, username="platform_admin_user", password="correct-horse-3")
    make_platform_admin(client, username="platform_admin_user")
    allowed = other_client.patch(f"/api/admin/teams/{team['id']}", json={"name": "Renamed By Admin"}, headers=other_headers)
    assert allowed.status_code == 200, allowed.json()
    assert allowed.json()["name"] == "Renamed By Admin"


def test_delete_team_without_matches_succeeds(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    deleted = client.delete(f"/api/admin/teams/{team['id']}", headers=headers)
    assert deleted.status_code == 204
    listed = client.get(f"/api/admin/events/{event['id']}/teams", headers=headers)
    assert listed.json() == []


def test_delete_team_with_match_history_is_blocked(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    team_a = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers).json()
    team_b = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Bravo"}, headers=headers).json()
    client.post(
        f"/api/admin/events/{event['id']}/fixtures",
        json={"team_a_id": team_a["id"], "team_b_id": team_b["id"], "scheduled_at": "2026-10-02T08:00:00"},
        headers=headers,
    )
    deleted = client.delete(f"/api/admin/teams/{team_a['id']}", headers=headers)
    assert deleted.status_code == 409
    assert deleted.json()["error"]["code"] == "TEAM_HAS_MATCHES"


def test_creating_team_for_cancelled_event_is_rejected(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    cancelled = client.patch(f"/api/admin/events/{event['id']}", json={"status": "CANCELLED"}, headers=headers)
    assert cancelled.status_code == 200
    created = client.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"}, headers=headers)
    assert created.status_code == 409
    assert created.json()["error"]["code"] == "EVENT_CANCELLED"


def test_unauthenticated_caller_cannot_call_organizer_team_endpoints(client: TestClient):
    headers = login(client)
    event = new_event(client, headers)
    anonymous = TestClient(client.app)  # fresh cookie jar, same app/DB — no session at all
    response = anonymous.post(f"/api/admin/events/{event['id']}/teams", json={"name": "Team Alpha"})
    assert response.status_code == 401
