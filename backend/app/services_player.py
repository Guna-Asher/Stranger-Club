from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .deps import token_hash
from .models import OtpChallenge, PlayerProfile, User, now_ist
from .services import api_error

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
    # the attempt-count check.
    session.rollback(); session.execute(text("BEGIN IMMEDIATE"))
    challenge = session.scalar(
        select(OtpChallenge)
        .where(OtpChallenge.phone == phone, OtpChallenge.consumed_at.is_(None))
        .order_by(OtpChallenge.created_at.desc())
    )
    if not challenge or challenge.expires_at <= now_ist():
        session.commit()
        raise api_error(401, "INVALID_OTP", "That code is invalid or has expired.")
    if challenge.attempts >= OTP_MAX_ATTEMPTS:
        session.commit()
        raise api_error(429, "OTP_LOCKED", "Too many incorrect attempts. Request a new code.")
    challenge.attempts += 1
    if not secrets.compare_digest(challenge.code_hash, token_hash(code)):
        session.commit()
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
