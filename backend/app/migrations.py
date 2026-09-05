"""Small, append-only SQLite migrations for deployments that predate Alembic.

The app began as a single SQLite MVP.  These migrations deliberately only add
tables/columns/indexes and transform values; they never drop user data.
"""
from sqlalchemy import Connection, inspect, text


def _has_column(connection: Connection, table: str, column: str) -> bool:
    return column in {item["name"] for item in inspect(connection).get_columns(table)}


def upgrade(connection: Connection) -> None:
    connection.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"))
    applied = {row[0] for row in connection.execute(text("SELECT version FROM schema_migrations"))}
    # Base.metadata creates new tables; legacy installations need only columns
    # added to their existing registrations table.
    if "20260818_domain_foundation" not in applied and _has_column(connection, "registrations", "id"):
        additions = {
            "public_id": "VARCHAR(64)",
            "player_id": "INTEGER",
            "preferred_position": "VARCHAR(32)",
            "assigned_position": "VARCHAR(32)",
        }
        for name, definition in additions.items():
            if not _has_column(connection, "registrations", name):
                connection.execute(text(f"ALTER TABLE registrations ADD COLUMN {name} {definition}"))
        connection.execute(text("UPDATE registrations SET public_id = lower(hex(randomblob(16))) WHERE public_id IS NULL"))
        # Old versions mixed payment state into registration.status. Keep their
        # payment record as source of truth and normalise the registration side.
        connection.execute(text("UPDATE registrations SET status = 'PENDING' WHERE status IN ('PENDING_PAYMENT', 'PAYMENT_SUBMITTED')"))
        connection.execute(text("UPDATE matches SET status = 'OPEN' WHERE status = 'ACTIVE'"))
        connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_registrations_public_id ON registrations(public_id)"))
        connection.execute(text("INSERT INTO schema_migrations(version) VALUES ('20260818_domain_foundation')"))

    # Kept separately so installations that ran the first foundation migration
    # still receive the state split/backfill on their next deployment.
    if "20260818_payment_state_cleanup" not in applied and _has_column(connection, "payments", "status"):
        connection.execute(text("UPDATE payments SET status = 'SUBMITTED' WHERE status = 'PAYMENT_SUBMITTED'"))
        connection.execute(text("UPDATE payments SET status = 'VERIFIED' WHERE status = 'CONFIRMED'"))
        if _has_column(connection, "registrations", "player_id"):
            connection.execute(text("""
                INSERT OR IGNORE INTO players (phone, name, email, preferred_position, created_at, updated_at)
                SELECT phone, max(name), max(email), 'NO_PREFERENCE', min(created_at), max(updated_at)
                FROM registrations GROUP BY phone
            """))
            connection.execute(text("""
                UPDATE registrations SET player_id = (
                    SELECT players.id FROM players WHERE players.phone = registrations.phone
                ) WHERE player_id IS NULL
            """))
        connection.execute(text("INSERT INTO schema_migrations(version) VALUES ('20260818_payment_state_cleanup')"))

    if "20260818_registration_defaults" not in applied and _has_column(connection, "registrations", "preferred_position"):
        connection.execute(text("UPDATE registrations SET preferred_position = 'NO_PREFERENCE' WHERE preferred_position IS NULL"))
        connection.execute(text("INSERT INTO schema_migrations(version) VALUES ('20260818_registration_defaults')"))

    # Player identity (users/player_profiles/player_sessions/otp_challenges) are
    # brand-new tables, created automatically by Base.metadata.create_all for
    # every installation. Only the new column on the pre-existing registrations
    # table needs an explicit migration. Rows created before this version keep
    # user_id = NULL and are intentionally never matched by the ownership check
    # (NULL never equals an authenticated user's id) — no legacy access path.
    if "20260906_player_identity" not in applied and _has_column(connection, "registrations", "id"):
        if not _has_column(connection, "registrations", "user_id"):
            connection.execute(text("ALTER TABLE registrations ADD COLUMN user_id INTEGER"))
        connection.execute(text("INSERT INTO schema_migrations(version) VALUES ('20260906_player_identity')"))
