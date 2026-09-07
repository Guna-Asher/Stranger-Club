from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .deps import PASSWORD_HASHER, token_hash
from .models import OtpChallenge, PlayerProfile, User, now_ist
from .schemas import normalize_email
from .services import api_error, assign_new_avatar, begin_serialized_write

logger = logging.getLogger("stranger_club")

OTP_CODE_LENGTH = 6
OTP_EXPIRY_MINUTES = 5
OTP_MAX_ATTEMPTS = 5


def _generate_code() -> str:
    return f"{secrets.randbelow(10 ** OTP_CODE_LENGTH):0{OTP_CODE_LENGTH}d}"


def request_otp(session: Session, phone: str, otp_provider) -> None:
    # Single-active-challenge policy: requesting a new code invalidates
    # whatever challenge existed before, so only the latest one can ever verify.
    session.execute(
        text("UPDATE otp_challenges SET consumed_at = :now WHERE phone = :phone AND consumed_at IS NULL"),
        {"now": now_ist(), "phone": phone},
    )
    code = _generate_code()
    session.add(OtpChallenge(phone=phone, code_hash=token_hash(code), expires_at=now_ist() + timedelta(minutes=OTP_EXPIRY_MINUTES)))
    session.commit()
    otp_provider.send(phone, code)
    logger.info("otp_requested")


def verify_otp(session: Session, phone: str, code: str) -> User:
    # Serialised like the other capacity/state-sensitive writes in services.py,
    # so concurrent verify attempts against the same challenge can't race past
    # the attempt-count check. No row id is known ahead of time here (the
    # target row is found by phone, not by primary key), so — unlike
    # services.py's lock_row() — PostgreSQL locks it directly via
    # with_for_update() on this select. Safe to do here (unlike several
    # services.py queries) because this select has no joinedload of a
    # collection relationship: FOR UPDATE cannot be combined with the outer
    # joins joinedload() produces for those.
    begin_serialized_write(session)
    challenge_query = select(OtpChallenge).where(OtpChallenge.phone == phone, OtpChallenge.consumed_at.is_(None)).order_by(OtpChallenge.created_at.desc())
    if session.bind.dialect.name == "postgresql":
        challenge_query = challenge_query.with_for_update()
    challenge = session.scalar(challenge_query)
    if not challenge or challenge.expires_at <= now_ist():
        session.commit()
        raise api_error(401, "INVALID_OTP", "That code is invalid or has expired.")
    if challenge.attempts >= OTP_MAX_ATTEMPTS:
        session.commit()
        logger.warning("otp_locked challenge_id=%s", challenge.id)
        raise api_error(429, "OTP_LOCKED", "Too many incorrect attempts. Request a new code.")
    challenge.attempts += 1
    if not secrets.compare_digest(challenge.code_hash, token_hash(code)):
        session.commit()
        # Never logs the phone number or either code (attempted or correct)
        # — same minimalism as otp_requested() above — only that an
        # attempt against this challenge failed, and which challenge.
        logger.warning("otp_verify_failed challenge_id=%s attempts=%s", challenge.id, challenge.attempts)
        raise api_error(401, "INVALID_OTP", "That code is invalid or has expired.")
    challenge.consumed_at = now_ist()
    user = session.scalar(select(User).where(User.phone == phone))
    if not user:
        user = User(phone=phone, phone_verified_at=now_ist())
        session.add(user); session.flush()
        session.add(PlayerProfile(user_id=user.id))
    else:
        user.phone_verified_at = now_ist(); user.last_login_at = now_ist()
    session.commit()
    session.refresh(user)
    logger.info("player_verified user_id=%s", user.id)
    return user


# ---------------------------------------------------------------------------
# Email + password: the current player onboarding/login path (this file's
# OTP functions above are untouched and remain fully functional — they're
# simply no longer wired into the active signup/login UI; see
# routers/player_auth.py). A precomputed Argon2 hash, verified against (and
# always failing) whenever a submitted login email doesn't resolve to an
# active, password-having account — the exact same timing-side-channel
# mitigation routers/auth.py already uses for organizer login, reused here
# rather than reinvented.
# ---------------------------------------------------------------------------

_DUMMY_PLAYER_PASSWORD_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


def create_player_account(session: Session, *, name: str, email: str, phone: str, password: str) -> User:
    """First-time player account creation. Never touches OTP: users.phone is
    left NULL (this account has no OTP-verified identity yet), and the raw
    phone number the player gave goes only onto PlayerProfile.phone as
    editable, unverified profile information. Avatar assignment happens
    after the account is safely committed (see the assign_new_avatar call
    below) so a rare catalog-exhaustion failure there can never roll back
    account creation itself — the same reasoning documented on
    assign_new_avatar's own IntegrityError-retry loop."""
    normalized_email = normalize_email(email)
    normalized_phone = phone  # already normalized by PlayerSignupRequest's own validator
    if session.scalar(select(User).where(func.lower(User.email) == normalized_email)):
        raise api_error(409, "EMAIL_TAKEN", "An account with this email already exists.")

    user = User(email=normalized_email, password_hash=PASSWORD_HASHER.hash(password), is_active=True)
    session.add(user)
    try:
        session.flush()
        session.add(PlayerProfile(user_id=user.id, display_name=name, phone=normalized_phone))
        session.commit()
    except IntegrityError:
        session.rollback()
        raise api_error(409, "EMAIL_TAKEN", "An account with this email already exists.")
    session.refresh(user)
    logger.info("player_account_created user_id=%s", user.id)

    try:
        assign_new_avatar(session, user.profile)
    except HTTPException:
        # Rare (a near-exhausted 256-design catalog): the account still
        # exists without an avatar yet — the player can generate one from
        # Edit Profile exactly as any existing avatar-less profile already
        # can. Never worth losing the account creation over.
        logger.warning("player_signup_avatar_assignment_failed user_id=%s", user.id)
    return user


def authenticate_player(session: Session, email: str, password: str) -> User | None:
    """Verifies email+password credentials, always doing the same Argon2
    work regardless of whether the email resolves to a real, password-having
    account — see _DUMMY_PLAYER_PASSWORD_HASH above. Returns None (never
    raises) on any failure: the caller (routers/player_auth.py) owns turning
    that into the generic 401 and recording the rate-limit attempt, the same
    split routers/auth.py's organizer login already uses."""
    from argon2.exceptions import VerifyMismatchError

    normalized_email = normalize_email(email)
    user = session.scalar(select(User).where(func.lower(User.email) == normalized_email))
    if user and user.is_active and user.password_hash:
        try:
            if PASSWORD_HASHER.verify(user.password_hash, password):
                return user
        except VerifyMismatchError:
            pass
        return None
    try:
        PASSWORD_HASHER.verify(_DUMMY_PLAYER_PASSWORD_HASH, password)
    except VerifyMismatchError:
        pass
    return None
