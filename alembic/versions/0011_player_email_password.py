"""player email+password authentication

Adds the columns needed for email+password player signup/login, without
touching the existing OTP architecture (OtpChallenge, and the phone-based
half of User's identity) at all — see backend/app/services_player.py and
backend/app/routers/player_auth.py for where the new path lives alongside
the untouched OTP one.

- users.password_hash: nullable Argon2 hash (deps.PASSWORD_HASHER — the same
  instance/convention the organizer login already uses). NULL for any
  account that has never set a password, i.e. every pre-existing OTP-only
  account, until it completes a password-setup path later.
- users.phone becomes nullable: an email+password signup never invents or
  verifies a phone number, so it must be able to leave this column unset.
- player_profiles.phone: the profile-facing, editable, unverified phone
  number collected at signup and editable from Edit Profile — deliberately
  separate from users.phone, which stays reserved for an actual OTP-verified
  identity.
- A case-insensitive unique index on users.email (functional index on
  lower(email), partial WHERE email IS NOT NULL) — enforced at the database
  level so uniqueness can never be bypassed by a code path that forgets to
  normalize case first. Portable to both PostgreSQL and SQLite (3.9+, which
  supports expression + partial indexes).

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-07

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def _is_nullable(bind, table: str, column: str) -> bool:
    for c in inspect(bind).get_columns(table):
        if c["name"] == column:
            return bool(c["nullable"])
    return True


def _has_index(bind, table: str, name: str) -> bool:
    # inspect(bind).get_indexes() cannot be trusted here: SQLAlchemy's SQLite
    # reflection explicitly skips expression-based indexes ("Skipped
    # unsupported reflection of expression-based index ..."), which would
    # make this guard always report "missing" and re-run CREATE INDEX into
    # an OperationalError on any database that already has it. Query each
    # dialect's own catalog directly instead.
    if bind.dialect.name == "sqlite":
        row = bind.execute(sa.text("SELECT name FROM sqlite_master WHERE type = 'index' AND name = :name"), {"name": name}).first()
        return row is not None
    return name in {ix["name"] for ix in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # Same existence-guard reasoning as every prior revision in this
    # directory: a database old enough to predate this revision may still be
    # missing these columns entirely until Base.metadata.create_all() (which
    # runs before Alembic on a fresh SQLite bootstrap, and IS how a fresh
    # PostgreSQL database is built — see 0000's docstring) builds them fresh
    # from the *current* model. Every change below is guarded so it's a
    # no-op against a table create_all() already built complete.
    if not _has_column(bind, "users", "password_hash"):
        with op.batch_alter_table("users") as batch_op:
            batch_op.add_column(sa.Column("password_hash", sa.String(length=255), nullable=True))

    if not _is_nullable(bind, "users", "phone"):
        with op.batch_alter_table("users") as batch_op:
            batch_op.alter_column("phone", existing_type=sa.String(length=16), nullable=True)

    if not _has_column(bind, "player_profiles", "phone"):
        with op.batch_alter_table("player_profiles") as batch_op:
            batch_op.add_column(sa.Column("phone", sa.String(length=16), nullable=True))

    # Precondition, checked before the index DDL below — SQLite DDL here is
    # non-transactional (same reasoning as 0002's own precondition check), so
    # this runs before any schema-mutating statement, not interleaved with
    # them. In practice no application code has ever written to users.email
    # before this revision, so this is expected to always be empty; it exists
    # only to fail loudly instead of silently choosing a winner if that
    # assumption is ever wrong for some installation.
    dupes = bind.execute(sa.text(
        "SELECT lower(email) FROM users WHERE email IS NOT NULL GROUP BY lower(email) HAVING COUNT(*) > 1"
    )).fetchall()
    if dupes:
        raise RuntimeError(
            f"Cannot add the case-insensitive unique index on users.email: {len(dupes)} email value(s) "
            "collide once lower-cased. Resolve these duplicates manually before retrying this migration."
        )
    bind.execute(sa.text("UPDATE users SET email = lower(email) WHERE email IS NOT NULL AND email != lower(email)"))

    if not _has_index(bind, "users", "uq_users_email_lower"):
        op.execute(text("CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email)) WHERE email IS NOT NULL"))


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS uq_users_email_lower"))
    with op.batch_alter_table("player_profiles") as batch_op:
        batch_op.drop_column("phone")
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("phone", existing_type=sa.String(length=16), nullable=False)
        batch_op.drop_column("password_hash")
