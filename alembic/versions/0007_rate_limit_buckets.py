"""rate_limit_buckets table

Backs the distributed, PostgreSQL-based rate limiter (backend/app/rate_limit.py)
that replaces the old in-process dict buckets, which only worked correctly
with exactly one application process. A fixed-window counter per limiter key
(e.g. "login:203.0.113.5", "otp_verify_phone:9876543210"), updated with a
single atomic INSERT ... ON CONFLICT DO UPDATE so concurrent requests across
instances never race on a read-then-write.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-19

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "rate_limit_buckets" in inspect(bind).get_table_names():
        return
    op.create_table(
        "rate_limit_buckets",
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_table("rate_limit_buckets")
