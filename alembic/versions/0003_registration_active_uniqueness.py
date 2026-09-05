"""registration active-uniqueness invariant

Replaces the blanket UNIQUE(match_id, phone) with a partial unique index
scoped to ACTIVE registration statuses only (PENDING, WAITLISTED, CONFIRMED,
REJECTED — CANCELLED is excluded), keyed on the authenticated user rather
than the phone string. This is what makes cancel-then-register-again produce
a genuinely new, independent historical row instead of requiring row reuse:
a CANCELLED registration simply falls outside the index and no longer blocks
a new one.

Also adds registrations.cancelled_at and a CHECK constraint on status.

The same predicate is used for both sqlite_where and postgresql_where, so
this migration's *meaning* does not change when the project moves to
PostgreSQL later.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACTIVE_PREDICATE = "status IN ('PENDING', 'WAITLISTED', 'CONFIRMED', 'REJECTED') AND user_id IS NOT NULL"
ALL_STATUSES_CHECK = "status IN ('PENDING', 'WAITLISTED', 'CONFIRMED', 'REJECTED', 'CANCELLED')"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # Same reasoning as 0002: a database missing `registrations` entirely
    # would have it created fresh (by create_all(), from the current model —
    # which already has the new index/constraint/column) before this
    # migration runs, so every operation here is guarded to be a no-op
    # against a table that's already at the target shape.
    has_old_unique_constraint = any(
        uc["name"] == "uq_registration_match_phone" for uc in inspector.get_unique_constraints("registrations")
    )
    has_cancelled_at = "cancelled_at" in {c["name"] for c in inspector.get_columns("registrations")}
    has_status_check = any(
        cc["name"] == "ck_registration_status_valid" for cc in inspector.get_check_constraints("registrations")
    )
    has_active_index = any(ix["name"] == "uq_registration_active_per_user" for ix in inspector.get_indexes("registrations"))

    if has_old_unique_constraint or not has_cancelled_at or not has_status_check:
        with op.batch_alter_table("registrations") as batch_op:
            if has_old_unique_constraint:
                batch_op.drop_constraint("uq_registration_match_phone", type_="unique")
            if not has_cancelled_at:
                batch_op.add_column(sa.Column("cancelled_at", sa.DateTime(), nullable=True))
            if not has_status_check:
                batch_op.create_check_constraint("ck_registration_status_valid", ALL_STATUSES_CHECK)

    if not has_active_index:
        op.create_index(
            "uq_registration_active_per_user", "registrations", ["match_id", "user_id"],
            unique=True,
            sqlite_where=sa.text(ACTIVE_PREDICATE),
            postgresql_where=sa.text(ACTIVE_PREDICATE),
        )


def downgrade() -> None:
    op.drop_index("uq_registration_active_per_user", table_name="registrations")
    with op.batch_alter_table("registrations") as batch_op:
        batch_op.drop_constraint("ck_registration_status_valid", type_="check")
        batch_op.drop_column("cancelled_at")
        batch_op.create_unique_constraint("uq_registration_match_phone", ["match_id", "phone"])
