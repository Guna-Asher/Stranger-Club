from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, JSON, String, Text, Time, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Registration statuses considered "active" for the purposes of the one-active-
# registration-per-user-per-event invariant. REJECTED is deliberately included:
# the product lets a rejected player resubmit payment proof on the SAME
# registration (see services.submit_payment / Pay.jsx "UPLOAD NEW PROOF"), so a
# rejected registration must keep occupying its uniqueness slot rather than
# allowing a second, parallel registration to be created alongside it.
# CANCELLED is the only terminal, non-blocking status.
ACTIVE_REGISTRATION_STATUSES = ("PENDING", "WAITLISTED", "CONFIRMED", "REJECTED")
ALL_REGISTRATION_STATUSES = ACTIVE_REGISTRATION_STATUSES + ("CANCELLED",)


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
    upi_id: Mapped[str] = mapped_column(String(120))
    qr_code_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    # The organizer who owns this event. NULL only for rows that predate event
    # ownership; backfilled where unambiguous during the Phase 2B migration
    # (see alembic/versions/0002_...). Every new event sets this server-side.
    owner_organizer_id: Mapped[int | None] = mapped_column(ForeignKey("organizers.id"), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist, onupdate=now_ist)

    registrations: Mapped[list[Registration]] = relationship(back_populates="match", cascade="all, delete-orphan")


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
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    registration_id: Mapped[int] = mapped_column(ForeignKey("registrations.id"), unique=True, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    screenshot_path: Mapped[str] = mapped_column(String(255))
    screenshot_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="PAYMENT_SUBMITTED", index=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    registration: Mapped[Registration] = relationship(back_populates="payment")


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
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Attribution, added in Phase 2B. Nullable because historical rows (and a
    # small number of system-initiated events) may not have an actor.
    actor_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)


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
