"""teams, team_members, fixtures (Phase 4: Events -> Teams -> Matches)

Adds the Team domain: a Team belongs to exactly one Event ("matches" table);
TeamMember links an event-scoped Registration to a Team; Fixture (internal
name only — the product UI always says "Match"/"Matches", see models.py's
docstring) is a scheduled game between two Teams of the same event.

Cross-event integrity is enforced by composite foreign keys, not just Python
checks: team_members.(team_id, event_id) -> teams.(id, event_id) and
team_members.(registration_id, event_id) -> registrations.(id, match_id);
fixtures.(team_a_id, event_id) and (team_b_id, event_id) -> teams.(id,
event_id). This requires a new UniqueConstraint(id, match_id) on
registrations (id is already the PK; this just adds match_id alongside it as
the composite-FK target) and UniqueConstraint(id, event_id) on the new teams
table.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-05

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FIXTURE_STATUS_CHECK = "status IN ('SCHEDULED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED')"
FIXTURE_SEQUENCE_PREDICATE = "sequence IS NOT NULL"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # A genuinely fresh database (Postgres bootstrap, or a fresh SQLite
    # create_all()) already has every table below at its current model
    # shape — same guard pattern as 0007 for rate_limit_buckets.
    has_registration_composite_unique = any(
        uc["name"] == "uq_registration_id_match_id" for uc in inspector.get_unique_constraints("registrations")
    )
    if not has_registration_composite_unique:
        with op.batch_alter_table("registrations") as batch_op:
            batch_op.create_unique_constraint("uq_registration_id_match_id", ["id", "match_id"])

    if "teams" not in existing_tables:
        op.create_table(
            "teams",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("event_id", sa.Integer(), sa.ForeignKey("matches.id"), nullable=False),
            sa.Column("name", sa.String(60), nullable=False),
            sa.Column("short_code", sa.String(10), nullable=True),
            sa.Column("max_size", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("id", "event_id", name="uq_team_id_event_id"),
            sa.UniqueConstraint("event_id", "name", name="uq_team_name_per_event"),
        )
        op.create_index("ix_teams_event_id", "teams", ["event_id"])

    if "team_members" not in existing_tables:
        op.create_table(
            "team_members",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("team_id", sa.Integer(), nullable=False),
            sa.Column("registration_id", sa.Integer(), nullable=False),
            sa.Column("event_id", sa.Integer(), nullable=False),
            sa.Column("assigned_by_organizer_id", sa.Integer(), sa.ForeignKey("organizers.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("registration_id", name="uq_team_member_registration"),
            sa.ForeignKeyConstraint(["team_id", "event_id"], ["teams.id", "teams.event_id"], name="fk_team_member_team_event"),
            sa.ForeignKeyConstraint(
                ["registration_id", "event_id"], ["registrations.id", "registrations.match_id"],
                name="fk_team_member_registration_event",
            ),
        )
        op.create_index("ix_team_members_team_id", "team_members", ["team_id"])
        op.create_index("ix_team_members_event_id", "team_members", ["event_id"])

    if "fixtures" not in existing_tables:
        op.create_table(
            "fixtures",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("event_id", sa.Integer(), sa.ForeignKey("matches.id"), nullable=False),
            sa.Column("team_a_id", sa.Integer(), nullable=False),
            sa.Column("team_b_id", sa.Integer(), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=True),
            sa.Column("scheduled_at", sa.DateTime(), nullable=False),
            sa.Column("venue_override", sa.String(200), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="SCHEDULED"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint("team_a_id != team_b_id", name="ck_fixture_teams_distinct"),
            sa.CheckConstraint(FIXTURE_STATUS_CHECK, name="ck_fixture_status_valid"),
            sa.ForeignKeyConstraint(["team_a_id", "event_id"], ["teams.id", "teams.event_id"], name="fk_fixture_team_a_event"),
            sa.ForeignKeyConstraint(["team_b_id", "event_id"], ["teams.id", "teams.event_id"], name="fk_fixture_team_b_event"),
        )
        op.create_index("ix_fixtures_event_id", "fixtures", ["event_id"])
        op.create_index("ix_fixtures_scheduled_at", "fixtures", ["scheduled_at"])
        op.create_index("ix_fixtures_event_status", "fixtures", ["event_id", "status"])
        op.create_index(
            "uq_fixture_sequence_per_event", "fixtures", ["event_id", "sequence"],
            unique=True,
            sqlite_where=sa.text(FIXTURE_SEQUENCE_PREDICATE),
            postgresql_where=sa.text(FIXTURE_SEQUENCE_PREDICATE),
        )


def downgrade() -> None:
    op.drop_table("fixtures")
    op.drop_table("team_members")
    op.drop_table("teams")
    with op.batch_alter_table("registrations") as batch_op:
        batch_op.drop_constraint("uq_registration_id_match_id", type_="unique")
