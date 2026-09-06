"""0010_player_profile_avatar's data backfill — existing PlayerProfiles
(created before this revision existed) must come out the other side with a
valid, mutually-distinct avatar_design_id, never NULL and never colliding.

Mirrors test_api.py's test_legacy_database_is_upgraded_without_losing_event_or_payment:
pre-create just the `users`/`player_profiles` tables in their pre-0010 shape
(no avatar_design_id column) with real rows, leave every other table for
Base.metadata.create_all() to build fresh from the current models, then let
create_app()'s normal SQLite bootstrap (create_all + the real Alembic
upgrade path, since the database file already exists) carry it to head —
exactly the path a real deployed database takes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.main import create_app


def test_existing_profiles_receive_distinct_avatars_on_upgrade(tmp_path: Path):
    data_dir = tmp_path / "pre_avatar"; data_dir.mkdir(); database = data_dir / "stranger_club.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, phone VARCHAR(16) UNIQUE, phone_verified_at DATETIME,
                email VARCHAR(254), is_active BOOLEAN, created_at DATETIME, updated_at DATETIME, last_login_at DATETIME);
            CREATE TABLE player_profiles (id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE, display_name VARCHAR(120),
                cricket_role VARCHAR(32), skill_rating INTEGER, photo_storage_key VARCHAR(255), bio TEXT,
                created_at DATETIME, updated_at DATETIME);
            INSERT INTO users VALUES (1, '9700000001', NULL, NULL, 1, '2026-08-01', '2026-08-01', NULL);
            INSERT INTO users VALUES (2, '9700000002', NULL, NULL, 1, '2026-08-01', '2026-08-01', NULL);
            INSERT INTO users VALUES (3, '9700000003', NULL, NULL, 1, '2026-08-01', '2026-08-01', NULL);
            INSERT INTO player_profiles VALUES (1, 1, 'Player One', 'NO_PREFERENCE', NULL, NULL, NULL, '2026-08-01', '2026-08-01');
            INSERT INTO player_profiles VALUES (2, 2, 'Player Two', 'NO_PREFERENCE', NULL, NULL, NULL, '2026-08-01', '2026-08-01');
            INSERT INTO player_profiles VALUES (3, 3, 'Player Three', 'NO_PREFERENCE', NULL, NULL, NULL, '2026-08-01', '2026-08-01');
        """)

    with TestClient(create_app(data_dir=data_dir, admin_password="correct-horse")) as legacy_client:
        with legacy_client.app.state.session_factory() as session:
            from backend.app.models import PlayerProfile
            avatar_ids = [p.avatar_design_id for p in session.query(PlayerProfile).order_by(PlayerProfile.id).all()]

    assert len(avatar_ids) == 3
    assert all(design_id is not None for design_id in avatar_ids)
    assert len(set(avatar_ids)) == 3, "existing profiles must not collide on the same avatar"
    assert all(0 <= design_id < 256 for design_id in avatar_ids)
