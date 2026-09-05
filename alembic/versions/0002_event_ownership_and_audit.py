"""event ownership + audit attribution

Adds matches.owner_organizer_id (schema only — see NOTE), CHECK(capacity >= 2)
/ CHECK(fee >= 1) on matches, and actor/before/after columns on audit_logs.

NOTE on ownership backfill: it deliberately does NOT happen here. Alembic
migrations run inside make_session_factory(), which executes before the
application's lifespan hook has ever created its bootstrap organizer — so at
migration time, a genuinely fresh/legacy database may still have zero
organizers, and "backfill to the only organizer" could never apply where it's
needed most. The backfill (same fail-safe logic: unambiguous single-organizer
only, loud failure on ambiguity) instead runs from the app's startup lifespan
in main.py, after the organizer is guaranteed to exist. This migration just
adds the (nullable) column that backfill later fills in.

NOTE on existence guards: a database old enough to predate this revision can
still, in principle, be missing a table this migration touches (e.g. an
installation that never had audit_logs at all). Base.metadata.create_all()
creates any such missing table fresh, using the *current* model — which
already includes every column this migration would add. Every add_column /
create_check_constraint below is therefore guarded so it's a no-op against a
table that create_all() just built complete, rather than colliding with it.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def _has_check_constraint(bind, table: str, name: str) -> bool:
    return name in {c["name"] for c in inspect(bind).get_check_constraints(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # SQLite DDL in this setup is non-transactional (Alembic logs "will
    # assume non-transactional DDL" for it): a raised exception partway
    # through this function does NOT roll back DDL that already executed.
    # This precondition check therefore runs before any schema-mutating
    # statement, not interleaved with them.
    bad_capacity = bind.execute(sa.text("SELECT COUNT(*) FROM matches WHERE capacity < 2")).scalar_one()
    bad_fee = bind.execute(sa.text("SELECT COUNT(*) FROM matches WHERE fee < 1")).scalar_one()
    if bad_capacity or bad_fee:
        raise RuntimeError(
            f"Cannot add capacity/fee CHECK constraints: {bad_capacity} event(s) have capacity < 2 and "
            f"{bad_fee} have fee < 1. These violate the application's own long-standing validation rules "
            "and must be corrected manually before this migration can proceed."
        )

    needs_owner_column = not _has_column(bind, "matches", "owner_organizer_id")
    needs_capacity_check = not _has_check_constraint(bind, "matches", "ck_match_capacity_positive")
    needs_fee_check = not _has_check_constraint(bind, "matches", "ck_match_fee_nonnegative")
    if needs_owner_column or needs_capacity_check or needs_fee_check:
        with op.batch_alter_table("matches") as batch_op:
            if needs_owner_column:
                batch_op.add_column(sa.Column(
                    "owner_organizer_id", sa.Integer(),
                    sa.ForeignKey("organizers.id", name="fk_matches_owner_organizer_id"), nullable=True,
                ))
            if needs_capacity_check:
                batch_op.create_check_constraint("ck_match_capacity_positive", "capacity >= 2")
            if needs_fee_check:
                batch_op.create_check_constraint("ck_match_fee_nonnegative", "fee >= 1")

    audit_columns = {"actor_type": sa.String(length=32), "actor_id": sa.Integer(), "before_json": sa.JSON(), "after_json": sa.JSON()}
    missing_audit_columns = {name: type_ for name, type_ in audit_columns.items() if not _has_column(bind, "audit_logs", name)}
    if missing_audit_columns:
        with op.batch_alter_table("audit_logs") as batch_op:
            for name, type_ in missing_audit_columns.items():
                batch_op.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.drop_column("after_json")
        batch_op.drop_column("before_json")
        batch_op.drop_column("actor_id")
        batch_op.drop_column("actor_type")

    with op.batch_alter_table("matches") as batch_op:
        batch_op.drop_constraint("ck_match_fee_nonnegative", type_="check")
        batch_op.drop_constraint("ck_match_capacity_positive", type_="check")
        batch_op.drop_column("owner_organizer_id")
