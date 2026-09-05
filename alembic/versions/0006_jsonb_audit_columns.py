"""audit_logs JSON columns -> JSONB on PostgreSQL

PostgreSQL's JSONB is indexable and queryable in ways plain JSON is not —
worth having now that audit_logs is a real production table. SQLite has no
JSONB concept and keeps its existing JSON (TEXT-backed) representation
unchanged; this migration is a no-op there.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-19

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = ("metadata_json", "before_json", "after_json")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.batch_alter_table("audit_logs") as batch_op:
        for column in COLUMNS:
            batch_op.alter_column(column, type_=postgresql.JSONB(), postgresql_using=f"{column}::jsonb")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.batch_alter_table("audit_logs") as batch_op:
        for column in COLUMNS:
            batch_op.alter_column(column, type_=sa.JSON())
