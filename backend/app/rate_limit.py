from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import DateTime, Integer, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .models import now_ist

logger = logging.getLogger("stranger_club.rate_limit")

# A single atomic fixed-window upsert: if the caller's stored window has
# already expired, reset it (count=1, window_start=now); otherwise increment
# the existing counter. ON CONFLICT ... DO UPDATE makes this one round trip
# with no read-then-write race between concurrent requests — including
# concurrent requests landing on *different* application instances, since
# the counter lives in PostgreSQL, not in any one process's memory.
_UPSERT_SQL = text("""
    INSERT INTO rate_limit_buckets (key, window_start, count)
    VALUES (:key, :now, 1)
    ON CONFLICT (key) DO UPDATE SET
        count = CASE WHEN rate_limit_buckets.window_start <= :window_expiry THEN 1 ELSE rate_limit_buckets.count + 1 END,
        window_start = CASE WHEN rate_limit_buckets.window_start <= :window_expiry THEN :now ELSE rate_limit_buckets.window_start END
    RETURNING count
""")


class RateLimitBackendError(RuntimeError):
    """Raised when the rate-limit table itself can't be read/written (e.g.
    the database is unavailable)."""


def _increment(session: Session, key: str, window_seconds: int) -> int:
    now = now_ist()
    window_expiry = now - timedelta(seconds=window_seconds)
    try:
        count = session.execute(_UPSERT_SQL, {"key": key, "now": now, "window_expiry": window_expiry}).scalar_one()
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        raise RateLimitBackendError(f"rate limit backend unavailable for key={key}") from exc
    return count


def enforce_rate_limit_db(session: Session, key: str, limit: int, window_seconds: int) -> None:
    """Raises HTTPException(429) if `key` has exceeded `limit` requests
    within the last `window_seconds` — counting this call. Used for limiters
    where every request counts regardless of outcome (registration, payment
    upload, OTP request/verify).

    Fails CLOSED if the backend itself is unavailable: a client cannot
    bypass rate limiting by waiting for a database blip. Applied uniformly
    (not just to login/OTP) — simpler to reason about, and the database
    being unreachable already fails the underlying request for every other
    limiter anyway."""
    from .services import api_error  # local import: avoids a circular import at module load time

    try:
        count = _increment(session, key, window_seconds)
    except RateLimitBackendError:
        logger.error("rate_limit_backend_unavailable key=%s", key)
        raise api_error(429, "RATE_LIMITED", "Too many requests. Please try again later.")
    if count > limit:
        raise api_error(429, "RATE_LIMITED", "Too many requests. Please try again later.")


def peek_rate_limit_db(session: Session, key: str, window_seconds: int) -> int:
    """Current count for `key` without recording a new attempt — 0 if the
    window has expired or no bucket exists yet. Used by callers (login) that
    need to reject *before* knowing whether this attempt should itself count
    as a failure."""
    # .columns(...) declares result types explicitly: a raw text() query
    # otherwise skips SQLAlchemy's normal column type decoding, and SQLite
    # returns window_start as a plain string rather than a datetime.
    select_stmt = text("SELECT window_start, count FROM rate_limit_buckets WHERE key = :key").columns(
        window_start=DateTime(), count=Integer(),
    )
    try:
        row = session.execute(select_stmt, {"key": key}).first()
    except SQLAlchemyError as exc:
        session.rollback()
        raise RateLimitBackendError(f"rate limit backend unavailable for key={key}") from exc
    if not row:
        return 0
    window_start, count = row
    if window_start <= now_ist() - timedelta(seconds=window_seconds):
        return 0
    return count


def record_rate_limit_attempt_db(session: Session, key: str, window_seconds: int) -> None:
    """Records one attempt against `key` without raising — the caller has
    already decided this attempt should count (e.g. a failed login)."""
    _increment(session, key, window_seconds)


def reset_rate_limit_db(session: Session, key: str) -> None:
    """Clears `key`'s bucket entirely — e.g. a successful login resets the
    failed-attempt counter, matching the pre-Phase-3 in-memory behaviour."""
    session.execute(text("DELETE FROM rate_limit_buckets WHERE key = :key"), {"key": key})
    session.commit()
