"""PostgreSQL schema bootstrap

SQLite bootstraps via Base.metadata.create_all() plus the frozen hand-rolled
migrations in backend/app/migrations.py, both run before Alembic ever starts
(see backend/app/database.py) — that pre-Alembic history is what 0001's
no-op assumes exists.

PostgreSQL has no such pre-Alembic history: production Postgres databases
are always brand new to this project. This is the ONLY place a PostgreSQL
schema is ever created, and it is created directly from the CURRENT models
via Base.metadata.create_all(checkfirst=True) — not silently, not as an
application-startup side effect, but as one reviewable, versioned migration
step, invoked only through `alembic upgrade head`.

Every revision from 0001 onward is then a genuine no-op against a database
bootstrapped this way, because each of those revisions is already guarded
(see their own docstrings/_has_column-style checks) to skip its DDL when the
target shape already exists — exactly the same guarantee that already makes
them safe against a SQLite database built by create_all().

On SQLite this revision does nothing: create_all() + migrations.py already
ran first, as they always have.

Revision ID: 0000
Revises:
Create Date: 2026-09-19

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0000"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    from backend.app.models import Base
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    # Dropping the entire schema is never an automated downgrade path.
    pass
