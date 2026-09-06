"""Player avatars — a finite, code-defined pixel/identicon catalog with a
real database-level ownership guarantee (see services.py's
assign_new_avatar/choose_avatar and PlayerProfile.avatar_design_id): one
design is never the *current* avatar of two players at once, enforced by the
column's own UNIQUE constraint, not merely a low-collision-probability hash.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from tests.test_api import client
from tests.test_player_dashboard import player_client_for

__all__ = ["client"]


def test_new_player_has_no_avatar_until_they_pick_one(client: TestClient):
    player, headers = player_client_for(client, "9600000001")
    profile = player.get("/api/player/profile", headers=headers).json()
    assert profile["avatar_design_id"] is None


def test_avatar_options_requires_authentication(client: TestClient):
    assert client.get("/api/player/avatar/options").status_code == 401


def test_generate_new_avatar_assigns_a_valid_design_and_persists(client: TestClient):
    player, headers = player_client_for(client, "9600000002")
    generated = player.post("/api/player/avatar/generate", headers=headers)
    assert generated.status_code == 200, generated.json()
    design_id = generated.json()["avatar_design_id"]
    assert design_id is not None and 0 <= design_id < 256

    # Persists: a fresh GET (simulating a page reload) returns the same one.
    reloaded = player.get("/api/player/profile", headers=headers).json()
    assert reloaded["avatar_design_id"] == design_id


def test_player_can_choose_a_specific_avatar(client: TestClient):
    player, headers = player_client_for(client, "9600000003")
    chosen = player.put("/api/player/avatar", json={"design_id": 42}, headers=headers)
    assert chosen.status_code == 200, chosen.json()
    assert chosen.json()["avatar_design_id"] == 42


def test_choose_avatar_rejects_out_of_range_design(client: TestClient):
    player, headers = player_client_for(client, "9600000004")
    assert player.put("/api/player/avatar", json={"design_id": -1}, headers=headers).status_code == 422
    assert player.put("/api/player/avatar", json={"design_id": 256}, headers=headers).status_code == 422


def test_player_can_change_avatar_and_old_one_becomes_available(client: TestClient):
    asher, asher_headers = player_client_for(client, "9600000005")
    asher.put("/api/player/avatar", json={"design_id": 17}, headers=asher_headers)

    # While Asher holds design 17, nobody else can claim it.
    guna, guna_headers = player_client_for(client, "9600000006")
    blocked = guna.put("/api/player/avatar", json={"design_id": 17}, headers=guna_headers)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "AVATAR_TAKEN"

    # Asher moves to a different design ...
    switched = asher.put("/api/player/avatar", json={"design_id": 42}, headers=asher_headers)
    assert switched.status_code == 200
    assert switched.json()["avatar_design_id"] == 42

    # ... and design 17 is immediately available again for someone else.
    now_allowed = guna.put("/api/player/avatar", json={"design_id": 17}, headers=guna_headers)
    assert now_allowed.status_code == 200
    assert now_allowed.json()["avatar_design_id"] == 17


def test_avatar_options_lists_taken_designs_excluding_own(client: TestClient):
    asher, asher_headers = player_client_for(client, "9600000007")
    guna, guna_headers = player_client_for(client, "9600000008")
    asher.put("/api/player/avatar", json={"design_id": 5}, headers=asher_headers)
    guna.put("/api/player/avatar", json={"design_id": 9}, headers=guna_headers)

    options = asher.get("/api/player/avatar/options", headers=asher_headers).json()
    assert options["catalog_size"] == 256
    assert options["current"] == 5
    assert 9 in options["taken"]
    assert 5 not in options["taken"]  # a player's own current design is never listed as "taken" against them


def test_changing_name_role_or_bio_does_not_alter_avatar(client: TestClient):
    player, headers = player_client_for(client, "9600000009")
    player.put("/api/player/avatar", json={"design_id": 100}, headers=headers)
    player.patch(
        "/api/player/profile", headers=headers,
        json={"display_name": "New Name", "cricket_role": "BOWLER", "bio": "Loves cricket"},
    )
    profile = player.get("/api/player/profile", headers=headers).json()
    assert profile["avatar_design_id"] == 100
    assert profile["display_name"] == "New Name"


def test_player_cannot_see_or_modify_another_players_avatar_via_their_own_session(client: TestClient):
    """There is no request field naming a target user anywhere in the avatar
    API — every avatar endpoint resolves "whose profile" purely from the
    caller's own session. This proves player B's actions never touch player
    A's assignment, the only way "self-only" could actually break."""
    victim, victim_headers = player_client_for(client, "9600000010")
    victim.put("/api/player/avatar", json={"design_id": 200}, headers=victim_headers)

    attacker, attacker_headers = player_client_for(client, "9600000011")
    attacker.put("/api/player/avatar", json={"design_id": 201}, headers=attacker_headers)
    # Nothing in this payload can name the victim — confirm the victim's
    # avatar is untouched no matter what the attacker does with their own session.
    attacker.post("/api/player/avatar/generate", headers=attacker_headers)

    victim_profile = victim.get("/api/player/profile", headers=victim_headers).json()
    assert victim_profile["avatar_design_id"] == 200


def test_concurrent_selection_of_the_same_design_one_succeeds_one_conflicts(client: TestClient):
    player_a, headers_a = player_client_for(client, "9600000012")
    player_b, headers_b = player_client_for(client, "9600000013")

    def claim(pair):
        player, headers = pair
        return player.put("/api/player/avatar", json={"design_id": 77}, headers=headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [(player_a, headers_a), (player_b, headers_b)]))

    assert sorted(r.status_code for r in results) == [200, 409]

    profile_a = player_a.get("/api/player/profile", headers=headers_a).json()
    profile_b = player_b.get("/api/player/profile", headers=headers_b).json()
    holders_of_77 = [p for p in (profile_a, profile_b) if p["avatar_design_id"] == 77]
    assert len(holders_of_77) == 1


@pytest.mark.skipif("SC_TEST_DATABASE_URL" not in __import__("os").environ, reason="requires a real PostgreSQL instance (set SC_TEST_DATABASE_URL) to prove DB-level enforcement, not just SQLite")
def test_database_itself_rejects_duplicate_avatar_assignment(client: TestClient):
    """Bypasses services.choose_avatar and writes a raw duplicate
    avatar_design_id directly, proving the UNIQUE constraint itself — not
    just the Python-level check — is what makes ownership exclusive."""
    from sqlalchemy.exc import IntegrityError

    from backend.app.models import PlayerProfile

    player_a, headers_a = player_client_for(client, "9600000014")
    player_b, headers_b = player_client_for(client, "9600000015")
    player_a.put("/api/player/avatar", json={"design_id": 60}, headers=headers_a)
    player_b.put("/api/player/avatar", json={"design_id": 61}, headers=headers_b)

    with client.app.state.session_factory() as session:
        profile_b = session.query(PlayerProfile).filter_by(avatar_design_id=61).one()
        profile_b.avatar_design_id = 60
        with pytest.raises(IntegrityError):
            session.commit()
