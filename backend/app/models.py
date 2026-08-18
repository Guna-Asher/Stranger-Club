from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, JSON, String, Text, Time, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def now_ist() -> datetime:
    """Store local wall-clock datetimes consistently for this India-only MVP."""
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)


class Match(Base):
    __tablename__ = "matches"

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist, onupdate=now_ist)

    registrations: Mapped[list[Registration]] = relationship(back_populates="match", cascade="all, delete-orphan")


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    preferred_position: Mapped[str] = mapped_column(String(32), default="NO_PREFERENCE")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist, onupdate=now_ist)

    registrations: Mapped[list[Registration]] = relationship(back_populates="player")


class Registration(Base):
    __tablename__ = "registrations"
    __table_args__ = (UniqueConstraint("match_id", "phone", name="uq_registration_match_phone"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(16))
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    # Registration and payment each own a separate, intentionally small state machine.
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    preferred_position: Mapped[str] = mapped_column(String(32), default="NO_PREFERENCE")
    assigned_position: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist, onupdate=now_ist)

    match: Mapped[Match] = relationship(back_populates="registrations")
    player: Mapped[Player | None] = relationship(back_populates="registrations")
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
