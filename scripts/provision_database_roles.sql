-- Least-privilege PostgreSQL role provisioning for Stranger Club.
-- Run ONCE, by a superuser/admin, against a fresh database. See docs/database.md.
--
-- Three roles, not a complicated RBAC hierarchy:
--   stranger_club_migrator — DDL, schema owner. Used only by
--       `python -m backend.app.migrate` and by restoring a backup into a
--       fresh scratch database (scripts/restore_test.py). Never embedded in
--       the running application's DATABASE_URL.
--   stranger_club_app      — DML only (SELECT/INSERT/UPDATE/DELETE), no
--       CREATE/DROP/ALTER of any kind. This is the credential the running
--       application uses.
--   stranger_club_backup   — read-only (via the built-in pg_read_all_data
--       role), used only to run `pg_dump`. Cannot write, cannot see or
--       change any role's password, cannot DROP anything.
--
-- Replace the placeholder passwords before running. Requires PostgreSQL 14+
-- for pg_read_all_data (this project targets 16).

CREATE ROLE stranger_club_migrator LOGIN PASSWORD 'CHANGE_ME_MIGRATOR';
CREATE ROLE stranger_club_app LOGIN PASSWORD 'CHANGE_ME_APP';
CREATE ROLE stranger_club_backup LOGIN PASSWORD 'CHANGE_ME_BACKUP';

CREATE DATABASE stranger_club OWNER stranger_club_migrator;

\c stranger_club

GRANT USAGE, CREATE ON SCHEMA public TO stranger_club_migrator;
GRANT USAGE ON SCHEMA public TO stranger_club_app;
GRANT USAGE ON SCHEMA public TO stranger_club_backup;

-- Run `python -m backend.app.migrate` as stranger_club_migrator now, THEN
-- run the grants below — they need the tables to already exist, and the
-- ALTER DEFAULT PRIVILEGES lines make every *future* migration's new
-- tables inherit the same grants automatically, so this is a one-time step.

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO stranger_club_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO stranger_club_app;
GRANT pg_read_all_data TO stranger_club_backup;

ALTER DEFAULT PRIVILEGES FOR ROLE stranger_club_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO stranger_club_app;
ALTER DEFAULT PRIVILEGES FOR ROLE stranger_club_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO stranger_club_app;

-- Explicitly confirm stranger_club_app has no DDL rights (documentation of
-- intent — REVOKE on something never granted is a harmless no-op, but
-- makes the boundary explicit for the next person reading this file):
REVOKE CREATE ON SCHEMA public FROM stranger_club_app;
REVOKE ALL ON SCHEMA public FROM stranger_club_backup;
GRANT USAGE ON SCHEMA public TO stranger_club_backup;
