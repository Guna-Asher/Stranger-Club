from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String,
    Text, Time, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSONB on PostgreSQL (indexable/queryable), plain JSON elsewhere (SQLite).
# See alembic/versions/0006_jsonb_audit_columns.py for the migration that
# aligns an existing PostgreSQL database's column type with this.
JSONVariant = JSON().with_variant(JSONB(), "postgresql")

# Registration statuses considered "active" for the purposes of the one-active-
# registration-per-user-per-event invariant. REJECTED is deliberately included:
# the product lets a rejected player resubmit payment proof on the SAME
# registration (see services.submit_payment / Pay.jsx "UPLOAD NEW PROOF"), so a
# rejected registration must keep occupying its uniqueness slot rather than
# allowing a second, parallel registration to be created alongside it.
# CANCELLED is the only terminal, non-blocking status.
ACTIVE_REGISTRATION_STATUSES = ("PENDING", "WAITLISTED", "CONFIRMED", "REJECTED")
ALL_REGISTRATION_STATUSES = ACTIVE_REGISTRATION_STATUSES + ("CANCELLED",)

# Payment never observes the underlying bank/UPI transaction — these are the
# only states that can ever exist. AWAITING_PROOF is the state from the
# moment a Registration (and its Payment row) is created; there is no
# "player says they paid" state distinct from having submitted a proof.
PAYMENT_STATUSES = ("AWAITING_PROOF", "SUBMITTED", "VERIFIED", "REJECTED")
# A proof's own lifecycle, independent of its Payment's current status:
# PENDING -> ACCEPTED | REJECTED (organizer reviewed it), or PENDING ->
# SUPERSEDED (the player uploaded a replacement before it was reviewed).
# ACCEPTED/REJECTED/SUPERSEDED are all terminal for that proof row.
PROOF_STATUSES = ("PENDING", "ACCEPTED", "REJECTED", "SUPERSEDED")

# Phase 4: the scheduled game between two Teams. Internally named "Fixture"
# to avoid colliding with the Match class above, which is actually the Event
# entity — every user-facing string still says "Match"/"Matches" (see
# services.py / routers/fixtures.py). Forward-only, mirroring
# VALID_EVENT_TRANSITIONS's own reasoning: no COMPLETED -> SCHEDULED or other
# nonsensical reverse transition.
FIXTURE_STATUSES = ("SCHEDULED", "IN_PROGRESS", "COMPLETED", "CANCELLED")
# A team's roster is locked (see TeamMember below) once any of its fixtures
# reaches one of these statuses.
FIXTURE_ROSTER_LOCKING_STATUSES = ("IN_PROGRESS", "COMPLETED")

# Phase 5: the organizer's manually-entered outcome of a completed match. No
# automatic computation, ever — see MatchResult below.
RESULT_TYPES = ("TEAM_A_WIN", "TEAM_B_WIN", "DRAW", "NO_RESULT")
# Deliberately a single value for now (see MatchParticipant) — a real column
# so a future distinction (e.g. an unused substitute) doesn't require a
# migration, without inventing meaning that doesn't exist yet.
PARTICIPATION_STATUSES = ("PLAYED",)

# Size of the finite, code-defined avatar design catalog (see PlayerProfile.
# avatar_design_id). Each design is a deterministic pixel/identicon pattern
# rendered client-side from its integer ID alone — nothing about the design
# itself is stored server-side. Comfortably larger than this club's
# foreseeable player count while staying small enough that "list every taken
# ID" (see services.avatar_catalog_status) is always a single cheap query.
AVATAR_CATALOG_SIZE = 256


def _sql_in_tuple(values: tuple[str, ...]) -> str:
    """Python's tuple repr — used elsewhere in this file for CHECK
    constraints, e.g. f"status IN {ALL_REGISTRATION_STATUSES}" — renders a
    single-element tuple as ('X',), a trailing comma that is invalid SQL.
    This renders correctly for any length, including one."""
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


class Base(DeclarativeBase):
    pass


def now_ist() -> datetime:
    """Store local wall-clock datetimes consistently for this India-only MVP."""
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        CheckConstraint("capacity >= 2", name="ck_match_capacity_positive"),
        CheckConstraint("fee >= 1", name="ck_match_fee_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    date: Mapped[date] = mapped_column(Date)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    venue: Mapped[str] = mapped_column(String(200))
    capacity: Mapped[int] = mapped_column(Integer)
    fee: Mapped[int] = mapped_column(Integer)
    registration_deadline: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    # The organizer who owns this event. NULL only for rows that predate event
    # ownership; backfilled where unambiguous during the Phase 2B migration
    # (see alembic/versions/0002_...). Every new event sets this server-side.
    owner_organizer_id: Mapped[int | None] = mapped_column(ForeignKey("organizers.id"), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist, onupdate=now_ist)

    registrations: Mapped[list[Registration]] = relationship(back_populates="match", cascade="all, delete-orphan")
    payment_configuration: Mapped[EventPaymentConfiguration | None] = relationship(
        back_populates="match", uselist=False, cascade="all, delete-orphan"
    )
    teams: Mapped[list[Team]] = relationship(back_populates="event", cascade="all, delete-orphan")
    fixtures: Mapped[list[Fixture]] = relationship(back_populates="event", cascade="all, delete-orphan")


class EventPaymentConfiguration(Base):
    """The organizer's current, mutable payment settings for an event.

    Deliberately a separate 1:1 table rather than columns on Match: this is a
    payment-sensitive, organizer-editable surface, and every Payment freezes
    a snapshot of it at creation time (see Payment below) rather than ever
    reading it live again. Editing this row never rewrites history.
    """
    __tablename__ = "event_payment_configurations"
    __table_args__ = (
        CheckConstraint("qr_source IN ('GENERATED','UPLOADED')", name="ck_payment_config_qr_source_valid"),
        CheckConstraint(
            "(qr_source = 'UPLOADED' AND qr_storage_key IS NOT NULL) OR (qr_source = 'GENERATED' AND qr_storage_key IS NULL)",
            name="ck_payment_config_qr_storage_consistent",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), unique=True, index=True)
    payee_upi_id: Mapped[str] = mapped_column(String(120))
    payee_name: Mapped[str] = mapped_column(String(80), default="Stranger Club")
    qr_source: Mapped[str] = mapped_column(String(20), default="GENERATED")
    # Opaque storage key for an organizer-uploaded QR image. Never exposed to
    # a client directly — only ever resolved server-side. NULL when qr_source
    # is GENERATED (the client renders a QR from the server-authoritative
    # UPI URI itself; see Payment.qr_source_snapshot).
    qr_storage_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist, onupdate=now_ist)
    # Nullable for the same reason as Match.owner_organizer_id: a migration
    # backfilling this row for a pre-existing event can run before any
    # organizer account exists. Purely informational (never used for
    # authorization), so unlike owner_organizer_id it needs no startup
    # backfill of its own.
    updated_by_organizer_id: Mapped[int | None] = mapped_column(ForeignKey("organizers.id"), nullable=True)

    match: Mapped[Match] = relationship(back_populates="payment_configuration")


class Player(Base):
    """LEGACY, FROZEN. Superseded by User + PlayerProfile (Phase 2A/2B).

    No application code reads or writes this table anymore as of Phase 2B.
    The class/columns are kept declared only so that:
      (a) this table and registrations.player_id continue to exist for any
          database upgrading from a pre-Alembic legacy shape (the frozen
          migrations in migrations.py still reference them), and
      (b) SQLAlchemy's create_all() keeps producing a schema-compatible
          "no such table" is never raised for very old databases.
    Do not add new usage of this model. Do not dual-write to it.
    """
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    preferred_position: Mapped[str] = mapped_column(String(32), default="NO_PREFERENCE")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist, onupdate=now_ist)


class Registration(Base):
    __tablename__ = "registrations"
    __table_args__ = (
        CheckConstraint(f"status IN {ALL_REGISTRATION_STATUSES}", name="ck_registration_status_valid"),
        # Composite-FK target for TeamMember (see below): lets a TeamMember
        # row require, at the database level, that its registration's event
        # matches its team's event — id is already the PK, this just adds
        # match_id alongside it for the composite reference.
        UniqueConstraint("id", "match_id", name="uq_registration_id_match_id"),
        # One ACTIVE registration per (event, authenticated user). CANCELLED
        # rows are excluded from this index entirely, so a player can cancel
        # and register again as a brand-new, independent historical record —
        # never by reactivating or overwriting the cancelled one. Rows with
        # user_id IS NULL (pre-authentication legacy data) are also excluded:
        # they were never subject to this invariant and must not be
        # retroactively constrained by it.
        Index(
            "uq_registration_active_per_user",
            "match_id", "user_id",
            unique=True,
            sqlite_where=text(f"status IN {ACTIVE_REGISTRATION_STATUSES} AND user_id IS NOT NULL"),
            postgresql_where=text(f"status IN {ACTIVE_REGISTRATION_STATUSES} AND user_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    # LEGACY, unused — see Player docstring. Never written to by application
    # code as of Phase 2B; retained only for schema compatibility.
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), index=True, nullable=True)
    # The authenticated owner of this registration. NULL only for rows created
    # before player authentication existed; such rows are intentionally not
    # accessible through the owner-checked endpoints (no NULL == NULL fallback).
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(16))
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    # Registration and payment each own a separate, intentionally small state machine.
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    preferred_position: Mapped[str] = mapped_column(String(32), default="NO_PREFERENCE")
    assigned_position: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist, onupdate=now_ist)

    match: Mapped[Match] = relationship(back_populates="registrations")
    user: Mapped[User | None] = relationship()
    payment: Mapped[Payment | None] = relationship(back_populates="registration", uselist=False, cascade="all, delete-orphan")


class Payment(Base):
    """The player-specific payment expectation and current state for one
    Registration. Created alongside the Registration (status AWAITING_PROOF)
    and never re-reads EventPaymentConfiguration afterward: amount_due and
    the payee_*/qr_*_snapshot fields are frozen at creation time, so an
    organizer editing the event's live payment configuration later can never
    silently change what an existing Payment record represents.
    """
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(f"status IN {PAYMENT_STATUSES}", name="ck_payment_status_valid"),
        CheckConstraint(
            "(qr_source_snapshot = 'UPLOADED' AND qr_storage_key_snapshot IS NOT NULL) "
            "OR (qr_source_snapshot = 'GENERATED' AND qr_storage_key_snapshot IS NULL)",
            name="ck_payment_qr_snapshot_consistent",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    registration_id: Mapped[int] = mapped_column(ForeignKey("registrations.id"), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="AWAITING_PROOF", index=True)
    # --- Frozen at creation time from EventPaymentConfiguration. Never re-read. ---
    amount_due: Mapped[int] = mapped_column(Integer)
    payee_upi_id_snapshot: Mapped[str] = mapped_column(String(120))
    payee_name_snapshot: Mapped[str] = mapped_column(String(80))
    qr_source_snapshot: Mapped[str] = mapped_column(String(20))
    qr_storage_key_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # --- Current review state ---
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    registration: Mapped[Registration] = relationship(back_populates="payment")
    proofs: Mapped[list[PaymentProof]] = relationship(
        back_populates="payment", cascade="all, delete-orphan", order_by="PaymentProof.uploaded_at"
    )


class PaymentProof(Base):
    """One submitted screenshot (plus optional UTR). Append-only: rows are
    never deleted or overwritten to change their evidence — only `status`,
    `reviewed_by_organizer_id`, `reviewed_at`, `rejection_reason`, and
    `superseded_at` ever change after creation. A rejected or superseded
    proof stays in the table forever as the historical record it was."""
    __tablename__ = "payment_proofs"
    __table_args__ = (
        CheckConstraint(f"status IN {PROOF_STATUSES}", name="ck_payment_proof_status_valid"),
        Index(
            "uq_payment_proof_one_pending", "payment_id", unique=True,
            sqlite_where=text("status = 'PENDING'"),
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    screenshot_hash: Mapped[str] = mapped_column(String(64), index=True)
    file_size: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(String(40))
    utr_reference: Mapped[str | None] = mapped_column(String(30), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    submitted_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    reviewed_by_organizer_id: Mapped[int | None] = mapped_column(ForeignKey("organizers.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    payment: Mapped[Payment] = relationship(back_populates="proofs")


class Team(Base):
    """A competing side for exactly one Event. Deliberately minimal — no
    logos, sponsors, ranking points, or player ratings (see Phase 4 scope)."""
    __tablename__ = "teams"
    __table_args__ = (
        # Composite-FK target for TeamMember/Fixture below.
        UniqueConstraint("id", "event_id", name="uq_team_id_event_id"),
        UniqueConstraint("event_id", "name", name="uq_team_name_per_event"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    short_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Nullable = unlimited. Enforced in services.assign_team_member /
    # move_team_member under a row lock, not a DB CHECK — a "count of related
    # rows" constraint isn't portable across SQLite/PostgreSQL without
    # triggers, so the lock is the real guard; see services.py.
    max_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist, onupdate=now_ist)

    event: Mapped[Match] = relationship(back_populates="teams")
    members: Mapped[list[TeamMember]] = relationship(back_populates="team", cascade="all, delete-orphan")
    # No reverse fixtures_as_a/fixtures_as_b relationship here: with two
    # composite FKs both involving event_id (one per side), a bidirectional
    # relationship would need the same disambiguating primaryjoin as
    # Fixture.team_a/team_b below for no real benefit — services.py queries
    # Fixture directly (e.g. "does this team have any locking fixture")
    # instead.


class TeamMember(Base):
    """Links one Registration to one Team, both scoped to the same event.

    Keyed to Registration, not PlayerProfile: Registration is already
    event-scoped and stable (a cancelled registration is never reused; a new
    attempt is a brand-new row — see cancel_registration/create_registration
    in services.py), whereas PlayerProfile is a global, mutable record — a
    later profile edit must never be able to retroactively reinterpret who
    was on a team in a past event. registration_id is unique here: a
    registration is on at most one team, ever, at a time, which is also what
    makes "a player cannot belong to two teams in the same event" true for
    free (a player has at most one active registration per event already).

    The two ForeignKeyConstraints below are the actual cross-event integrity
    guarantee, enforced by the database itself, not just Python: team_id must
    belong to event_id, and registration_id must belong to event_id, so a
    membership row spanning two different events is structurally impossible
    to insert on either PostgreSQL or SQLite (with foreign_keys=ON — see
    database.py).
    """
    __tablename__ = "team_members"
    __table_args__ = (
        UniqueConstraint("registration_id", name="uq_team_member_registration"),
        # Redundant with the single-column unique above (registration_id is
        # already unique table-wide), but this composite is the Phase 5
        # composite-FK target that proves "this registration is genuinely a
        # member of this specific team" — see MatchParticipant.
        UniqueConstraint("registration_id", "team_id", name="uq_team_member_registration_team"),
        ForeignKeyConstraint(["team_id", "event_id"], ["teams.id", "teams.event_id"], name="fk_team_member_team_event"),
        ForeignKeyConstraint(
            ["registration_id", "event_id"], ["registrations.id", "registrations.match_id"],
            name="fk_team_member_registration_event",
        ),
        Index("ix_team_members_team_id", "team_id"),
        Index("ix_team_members_event_id", "event_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(Integer)
    registration_id: Mapped[int] = mapped_column(Integer)
    # Denormalized on purpose — required by the composite FKs above, which
    # are what make the cross-event guarantee a database-level fact.
    event_id: Mapped[int] = mapped_column(Integer)
    assigned_by_organizer_id: Mapped[int | None] = mapped_column(ForeignKey("organizers.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist, onupdate=now_ist)

    team: Mapped[Team] = relationship(back_populates="members")
    # overlaps: event_id is intentionally written explicitly by services.py
    # (never via relationship assignment), shared between this FK and
    # TeamMember.team's — silences SQLAlchemy's (accurate, harmless-here)
    # warning about the two relationships both touching that column.
    registration: Mapped[Registration] = relationship(overlaps="members,team")


class Fixture(Base):
    """A scheduled game between two Teams belonging to the same Event.

    Named "Fixture" only to avoid colliding with the Match class above
    (which is the Event entity) — the product UI always calls this "Match"/
    "Matches". No live scoring, innings, or result fields yet (see Phase 4
    scope) — Fixture.id is a stable, never-reused, never-deleted identity a
    later Results phase can attach to without any redesign here.
    """
    __tablename__ = "fixtures"
    __table_args__ = (
        # Composite-FK target for MatchResult/MatchParticipant (Phase 5).
        UniqueConstraint("id", "event_id", name="uq_fixture_id_event_id"),
        ForeignKeyConstraint(["team_a_id", "event_id"], ["teams.id", "teams.event_id"], name="fk_fixture_team_a_event"),
        ForeignKeyConstraint(["team_b_id", "event_id"], ["teams.id", "teams.event_id"], name="fk_fixture_team_b_event"),
        CheckConstraint("team_a_id != team_b_id", name="ck_fixture_teams_distinct"),
        CheckConstraint(f"status IN {FIXTURE_STATUSES}", name="ck_fixture_status_valid"),
        # Optional organizer-assigned ordering; unique within an event only
        # when actually set (same partial-unique-index pattern as
        # uq_registration_active_per_user).
        Index(
            "uq_fixture_sequence_per_event", "event_id", "sequence", unique=True,
            sqlite_where=text("sequence IS NOT NULL"),
            postgresql_where=text("sequence IS NOT NULL"),
        ),
        Index("ix_fixtures_event_status", "event_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    team_a_id: Mapped[int] = mapped_column(Integer)
    team_b_id: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    venue_override: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="SCHEDULED")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist, onupdate=now_ist)

    event: Mapped[Match] = relationship(back_populates="fixtures")
    # Explicit primaryjoin: event_id participates in *both* composite FKs
    # (team_a's and team_b's), so SQLAlchemy's automatic FK-constraint
    # detection is ambiguous here — spelling out the join removes any
    # guesswork about which constraint applies to which relationship.
    team_a: Mapped[Team] = relationship(
        foreign_keys=[team_a_id, event_id],
        primaryjoin="and_(Fixture.team_a_id == Team.id, Fixture.event_id == Team.event_id)",
        overlaps="event,fixtures",
    )
    team_b: Mapped[Team] = relationship(
        foreign_keys=[team_b_id, event_id],
        primaryjoin="and_(Fixture.team_b_id == Team.id, Fixture.event_id == Team.event_id)",
        overlaps="event,fixtures,team_a",
    )


class MatchParticipant(Base):
    """A lightweight record answering exactly one question: did this
    Registration actually play this Fixture, for which Team. Deliberately
    small — no innings, substitutions, bench, or per-player stats (see
    Phase 5 scope).

    The four ForeignKeyConstraints below chain together to make every wrong
    combination structurally impossible to insert: team_id and
    registration_id must each independently belong to event_id (same
    technique as TeamMember), fixture_id must also belong to event_id, and
    — the one that goes further than TeamMember needs to — registration_id
    and team_id together must correspond to a real, existing TeamMember row,
    so a participant can never be recorded against a team the player was
    never actually assigned to.
    """
    __tablename__ = "match_participants"
    __table_args__ = (
        # Also the composite-FK target MatchResult's award fields reference —
        # see MatchResult below.
        UniqueConstraint("fixture_id", "registration_id", name="uq_match_participant_fixture_registration"),
        ForeignKeyConstraint(["team_id", "event_id"], ["teams.id", "teams.event_id"], name="fk_participant_team_event"),
        ForeignKeyConstraint(
            ["registration_id", "event_id"], ["registrations.id", "registrations.match_id"],
            name="fk_participant_registration_event",
        ),
        ForeignKeyConstraint(["fixture_id", "event_id"], ["fixtures.id", "fixtures.event_id"], name="fk_participant_fixture_event"),
        ForeignKeyConstraint(
            ["registration_id", "team_id"], ["team_members.registration_id", "team_members.team_id"],
            name="fk_participant_registration_team_member",
        ),
        CheckConstraint(f"participation_status IN {_sql_in_tuple(PARTICIPATION_STATUSES)}", name="ck_participant_status_valid"),
        Index("ix_match_participants_fixture_id", "fixture_id"),
        Index("ix_match_participants_event_id", "event_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    fixture_id: Mapped[int] = mapped_column(Integer)
    registration_id: Mapped[int] = mapped_column(Integer)
    team_id: Mapped[int] = mapped_column(Integer)
    # Denormalized on purpose — required by the composite FKs above.
    event_id: Mapped[int] = mapped_column(Integer)
    participation_status: Mapped[str] = mapped_column(String(20), default="PLAYED")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)

    # Explicit primaryjoin: registration_id participates in two composite FKs
    # (the one to registrations, and the one to team_members) — spelling out
    # the join picks the registrations one unambiguously, same reasoning as
    # Fixture.team_a/team_b above.
    registration: Mapped[Registration] = relationship(
        foreign_keys=[registration_id, event_id],
        primaryjoin="and_(MatchParticipant.registration_id == Registration.id, MatchParticipant.event_id == Registration.match_id)",
    )


class MatchResult(Base):
    """The organizer's manually-entered outcome of exactly one completed
    Fixture. Never computed automatically — see services.create_match_result.

    team_a_id/team_b_id are a denormalized snapshot of the fixture's teams
    at the moment the result is first created, not a live reference — this
    is what turns "the winner must be team A or team B, and a draw/no-result
    must have no winner" into a single same-row CHECK constraint, rather
    than a Python-only rule. The three award fields are each a composite FK
    into MatchParticipant, not directly into Registration — an award can
    only ever reference someone who has an actual MatchParticipant row for
    this exact fixture; SQL's standard multi-column FK semantics mean a NULL
    award (nothing selected) simply isn't checked at all, only a set one.
    """
    __tablename__ = "match_results"
    __table_args__ = (
        UniqueConstraint("fixture_id", name="uq_match_result_fixture"),
        ForeignKeyConstraint(["fixture_id", "event_id"], ["fixtures.id", "fixtures.event_id"], name="fk_result_fixture_event"),
        ForeignKeyConstraint(
            ["player_of_match_registration_id", "fixture_id"],
            ["match_participants.registration_id", "match_participants.fixture_id"],
            name="fk_result_mvp_participant",
        ),
        ForeignKeyConstraint(
            ["best_batter_registration_id", "fixture_id"],
            ["match_participants.registration_id", "match_participants.fixture_id"],
            name="fk_result_best_batter_participant",
        ),
        ForeignKeyConstraint(
            ["best_bowler_registration_id", "fixture_id"],
            ["match_participants.registration_id", "match_participants.fixture_id"],
            name="fk_result_best_bowler_participant",
        ),
        CheckConstraint(f"result_type IN {RESULT_TYPES}", name="ck_result_type_valid"),
        CheckConstraint(
            "(result_type = 'TEAM_A_WIN' AND winning_team_id = team_a_id) OR "
            "(result_type = 'TEAM_B_WIN' AND winning_team_id = team_b_id) OR "
            "(result_type IN ('DRAW', 'NO_RESULT') AND winning_team_id IS NULL)",
            name="ck_result_winner_consistent",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    fixture_id: Mapped[int] = mapped_column(Integer)
    event_id: Mapped[int] = mapped_column(Integer)
    team_a_id: Mapped[int] = mapped_column(Integer)
    team_b_id: Mapped[int] = mapped_column(Integer)
    winning_team_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_type: Mapped[str] = mapped_column(String(20))
    player_of_match_registration_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_batter_registration_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_bowler_registration_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finalized_by_organizer_id: Mapped[int | None] = mapped_column(ForeignKey("organizers.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist, onupdate=now_ist)


class Organizer(Base):
    __tablename__ = "organizers"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="ORGANIZER")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist, onupdate=now_ist)


class OrganizerSession(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    organizer_id: Mapped[int] = mapped_column(ForeignKey("organizers.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)

    organizer: Mapped[Organizer] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String(80))
    metadata_json: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    # Attribution, added in Phase 2B. Nullable because historical rows (and a
    # small number of system-initiated events) may not have an actor.
    actor_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before_json: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)


class RateLimitBucket(Base):
    """Backs the distributed rate limiter (backend/app/rate_limit.py). One
    fixed-window counter per limiter key, shared across every application
    instance via PostgreSQL — see 0007_rate_limit_buckets.py."""
    __tablename__ = "rate_limit_buckets"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class User(Base):
    """Authenticated player identity. Kept separate from profile data (PlayerProfile)."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist, onupdate=now_ist)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    profile: Mapped[PlayerProfile | None] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")


class PlayerProfile(Base):
    """Editable profile data, separate from the auth identity in User."""
    __tablename__ = "player_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    cricket_role: Mapped[str] = mapped_column(String(32), default="NO_PREFERENCE")
    skill_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    photo_storage_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The player's current avatar: an index (0..AVATAR_CATALOG_SIZE-1) into a
    # fixed, frontend-rendered pixel/identicon catalog — never derived from
    # display_name/cricket_role/team/event, so editing any of those can never
    # change it (see services.assign_new_avatar / choose_avatar). NULL until a
    # player generates/chooses one. The column's own UNIQUE constraint (NULLs
    # excluded from uniqueness on both SQLite and PostgreSQL) is the actual
    # ownership guarantee — one design can never be the *current* avatar of
    # two profiles at once — not merely a low-collision-probability hash.
    # Reuse policy: releasing an avatar (by switching to another one) frees
    # it for reassignment immediately; no history of past avatars is kept.
    avatar_design_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist, onupdate=now_ist)

    user: Mapped[User] = relationship(back_populates="profile")


class PlayerSession(Base):
    __tablename__ = "player_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)

    user: Mapped[User] = relationship()


class OtpChallenge(Base):
    """A single OTP code issued for a phone number. Only one active (unconsumed,
    unexpired) challenge per phone is intended to exist at a time — requesting a
    new code invalidates any prior one (see services_player.request_otp)."""
    __tablename__ = "otp_challenges"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(16), index=True)
    code_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
