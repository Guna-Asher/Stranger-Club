"""baseline (Phase 2A schema, no-op)

This revision intentionally does nothing. It exists so Alembic has a fixed
starting point: any database that already has the Phase 2A schema (built via
Base.metadata.create_all() plus the frozen hand-rolled migrations in
backend/app/migrations.py) is stamped here without re-running anything, and
every real schema change from Phase 2B onward is a revision *after* this one.

Revision ID: 0001
Revises: 0000
Create Date: 2026-09-13

"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = "0001"
down_revision: Union[str, None] = "0000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
