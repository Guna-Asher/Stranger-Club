"""match_participants, match_results (Phase 5: post-match Results/Awards/Participation)

Adds a manually-entered MatchResult (winner/draw/no-result plus optional
MVP/Best Batter/Best Bowler) and a lightweight MatchParticipant ("did this
registration actually play this fixture, for which team") — see
architecture.md for the full Event -> Registration -> TeamMember -> Fixture
-> MatchParticipant -> MatchResult chain.

Every cross-table reference here is a composite foreign key, the same
technique 0008 introduced for Team/TeamMember/Fixture: award fields
reference (registration_id, fixture_id) in match_participants rather than
registrations directly, so an award can only ever name someone who has a
real participation row for this exact fixture. Two new composite-FK targets
are required on existing tables: UNIQUE(id, event_id) on fixtures and
UNIQUE(registration_id, team_id) on team_members (the latter proves a
participant's team_id is a team the player was actually assigned to, not
just any team in the right event).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-05

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RESULT_TYPE_CHECK = "result_type IN ('TEAM_A_WIN', 'TEAM_B_WIN', 'DRAW', 'NO_RESULT')"
WINNER_CONSISTENCY_CHECK = (
    "(result_type = 'TEAM_A_WIN' AND winning_team_id = team_a_id) OR "
    "(result_type = 'TEAM_B_WIN' AND winning_team_id = team_b_id) OR "
    "(result_type IN ('DRAW', 'NO_RESULT') AND winning_team_id IS NULL)"
)
PARTICIPATION_STATUS_CHECK = "participation_status IN ('PLAYED')"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    has_fixture_composite_unique = any(
        uc["name"] == "uq_fixture_id_event_id" for uc in inspector.get_unique_constraints("fixtures")
    )
    if not has_fixture_composite_unique:
        with op.batch_alter_table("fixtures") as batch_op:
            batch_op.create_unique_constraint("uq_fixture_id_event_id", ["id", "event_id"])

    has_team_member_composite_unique = any(
        uc["name"] == "uq_team_member_registration_team" for uc in inspector.get_unique_constraints("team_members")
    )
    if not has_team_member_composite_unique:
        with op.batch_alter_table("team_members") as batch_op:
            batch_op.create_unique_constraint("uq_team_member_registration_team", ["registration_id", "team_id"])

    if "match_participants" not in existing_tables:
        op.create_table(
            "match_participants",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("fixture_id", sa.Integer(), nullable=False),
            sa.Column("registration_id", sa.Integer(), nullable=False),
            sa.Column("team_id", sa.Integer(), nullable=False),
            sa.Column("event_id", sa.Integer(), nullable=False),
            sa.Column("participation_status", sa.String(20), nullable=False, server_default="PLAYED"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("fixture_id", "registration_id", name="uq_match_participant_fixture_registration"),
            sa.CheckConstraint(PARTICIPATION_STATUS_CHECK, name="ck_participant_status_valid"),
            sa.ForeignKeyConstraint(["team_id", "event_id"], ["teams.id", "teams.event_id"], name="fk_participant_team_event"),
            sa.ForeignKeyConstraint(
                ["registration_id", "event_id"], ["registrations.id", "registrations.match_id"],
                name="fk_participant_registration_event",
            ),
            sa.ForeignKeyConstraint(
                ["fixture_id", "event_id"], ["fixtures.id", "fixtures.event_id"], name="fk_participant_fixture_event",
            ),
            sa.ForeignKeyConstraint(
                ["registration_id", "team_id"], ["team_members.registration_id", "team_members.team_id"],
                name="fk_participant_registration_team_member",
            ),
        )
        op.create_index("ix_match_participants_fixture_id", "match_participants", ["fixture_id"])
        op.create_index("ix_match_participants_event_id", "match_participants", ["event_id"])

    if "match_results" not in existing_tables:
        op.create_table(
            "match_results",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("fixture_id", sa.Integer(), nullable=False),
            sa.Column("event_id", sa.Integer(), nullable=False),
            sa.Column("team_a_id", sa.Integer(), nullable=False),
            sa.Column("team_b_id", sa.Integer(), nullable=False),
            sa.Column("winning_team_id", sa.Integer(), nullable=True),
            sa.Column("result_type", sa.String(20), nullable=False),
            sa.Column("player_of_match_registration_id", sa.Integer(), nullable=True),
            sa.Column("best_batter_registration_id", sa.Integer(), nullable=True),
            sa.Column("best_bowler_registration_id", sa.Integer(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("finalized_at", sa.DateTime(), nullable=True),
            sa.Column("finalized_by_organizer_id", sa.Integer(), sa.ForeignKey("organizers.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("fixture_id", name="uq_match_result_fixture"),
            sa.CheckConstraint(RESULT_TYPE_CHECK, name="ck_result_type_valid"),
            sa.CheckConstraint(WINNER_CONSISTENCY_CHECK, name="ck_result_winner_consistent"),
            sa.ForeignKeyConstraint(["fixture_id", "event_id"], ["fixtures.id", "fixtures.event_id"], name="fk_result_fixture_event"),
            sa.ForeignKeyConstraint(
                ["player_of_match_registration_id", "fixture_id"],
                ["match_participants.registration_id", "match_participants.fixture_id"],
                name="fk_result_mvp_participant",
            ),
            sa.ForeignKeyConstraint(
                ["best_batter_registration_id", "fixture_id"],
                ["match_participants.registration_id", "match_participants.fixture_id"],
                name="fk_result_best_batter_participant",
            ),
            sa.ForeignKeyConstraint(
                ["best_bowler_registration_id", "fixture_id"],
                ["match_participants.registration_id", "match_participants.fixture_id"],
                name="fk_result_best_bowler_participant",
            ),
        )


def downgrade() -> None:
    op.drop_table("match_results")
    op.drop_table("match_participants")
    with op.batch_alter_table("team_members") as batch_op:
        batch_op.drop_constraint("uq_team_member_registration_team", type_="unique")
    with op.batch_alter_table("fixtures") as batch_op:
        batch_op.drop_constraint("uq_fixture_id_event_id", type_="unique")
