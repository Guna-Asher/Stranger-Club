"""player_profiles.avatar_design_id (pixel/identicon avatar ownership)

Adds the single column that backs the player avatar system: an index into a
fixed, frontend-rendered pixel/identicon catalog (see models.AVATAR_CATALOG_SIZE
and services.py's assign_new_avatar/choose_avatar). The column's own UNIQUE
constraint is the real "one design, one current owner" guarantee — NULLs are
excluded from uniqueness on both SQLite and PostgreSQL, so any number of
profiles may still have no avatar yet.

Data backfill: every existing PlayerProfile is assigned a distinct design ID
in `id` order (0, 1, 2, ...) so no existing player is left without a valid
avatar. This assumes the number of existing profiles does not exceed
AVATAR_CATALOG_SIZE — true for this club today; if it's ever not, this
migration fails loudly (a UNIQUE-constraint violation) rather than silently
handing two players the same avatar.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-20

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def _has_unique_constraint(bind, table: str, name: str) -> bool:
    return name in {uc["name"] for uc in inspect(bind).get_unique_constraints(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # Same existence-guard reasoning as 0002: a database old enough to
    # predate this revision may still be missing player_profiles entirely
    # until Base.metadata.create_all() (which runs before Alembic on a fresh
    # SQLite bootstrap — see database.py) builds it fresh from the *current*
    # model, avatar_design_id column included. Adding it again here would
    # otherwise collide with a column create_all() already built.
    if not _has_column(bind, "player_profiles", "avatar_design_id"):
        with op.batch_alter_table("player_profiles") as batch_op:
            batch_op.add_column(sa.Column("avatar_design_id", sa.Integer(), nullable=True))

    if not _has_unique_constraint(bind, "player_profiles", "uq_player_profiles_avatar_design_id"):
        # A separate batch op, not merged with the add_column above: SQLite's
        # batch mode recreates the whole table to apply a change, and
        # combining "add this column" with "add a unique constraint on it" in
        # the same recreate confuses its column reordering (a
        # CircularDependencyError from SQLAlchemy's own topological sort) —
        # trivially avoided by letting the column exist as a normal,
        # already-reflected column before the constraint is added on its own.
        with op.batch_alter_table("player_profiles") as batch_op:
            batch_op.create_unique_constraint("uq_player_profiles_avatar_design_id", ["avatar_design_id"])

    # Frozen here rather than imported from backend.app.models.
    # AVATAR_CATALOG_SIZE — like every other migration in this directory,
    # this file is a fixed historical snapshot, not coupled to wherever that
    # constant's value goes next.
    AVATAR_CATALOG_SIZE = 256

    existing_profile_ids = [
        row[0] for row in bind.execute(sa.text("SELECT id FROM player_profiles WHERE avatar_design_id IS NULL ORDER BY id"))
    ]
    taken = set(bind.execute(sa.text("SELECT avatar_design_id FROM player_profiles WHERE avatar_design_id IS NOT NULL")).scalars())
    # Precondition checked before any UPDATE runs (same "validate first, then
    # mutate" shape as 0002's capacity/fee check) — SQLite's DDL above is
    # non-transactional, so failing loudly here, before touching a single
    # data row, is what keeps a too-small catalog from ever leaving a
    # partially-backfilled table; failing partway through the loop below
    # instead would.
    if len(existing_profile_ids) + len(taken) > AVATAR_CATALOG_SIZE:
        raise RuntimeError(
            f"Cannot backfill avatar_design_id: {len(existing_profile_ids)} profile(s) need one and "
            f"{len(taken)} design(s) are already taken, exceeding the {AVATAR_CATALOG_SIZE}-design catalog. "
            "Raise AVATAR_CATALOG_SIZE (and this migration's own frozen copy of it) before retrying."
        )
    available = (design_id for design_id in range(AVATAR_CATALOG_SIZE) if design_id not in taken)
    for profile_id in existing_profile_ids:
        bind.execute(
            sa.text("UPDATE player_profiles SET avatar_design_id = :design_id WHERE id = :id"),
            {"design_id": next(available), "id": profile_id},
        )
    print(f"[0010_player_profile_avatar] backfilled avatar_design_id for {len(existing_profile_ids)} existing profile(s)")


def downgrade() -> None:
    with op.batch_alter_table("player_profiles") as batch_op:
        batch_op.drop_constraint("uq_player_profiles_avatar_design_id", type_="unique")
        batch_op.drop_column("avatar_design_id")
