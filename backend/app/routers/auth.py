from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import PASSWORD_HASHER, SESSION_COOKIE, SESSION_HOURS, auth_context, get_session, require_csrf, token_hash
from ..models import Organizer, OrganizerSession, now_ist
from ..rate_limit import RateLimitBackendError, peek_rate_limit_db, record_rate_limit_attempt_db, reset_rate_limit_db
from ..schemas import AuthResponse, LoginRequest
from ..services import api_error

logger = logging.getLogger("stranger_club")
router = APIRouter()

LOGIN_FAILURE_LIMIT = 8
LOGIN_FAILURE_WINDOW_SECONDS = 900

# A precomputed Argon2 hash of an arbitrary value, verified against (and
# always failing) whenever the submitted username doesn't resolve to an
# active organizer. Argon2 is deliberately slow/memory-hard; skipping that
# work entirely for a nonexistent username would make the login endpoint's
# response time a timing side-channel an attacker could use to enumerate
# valid organizer usernames without ever needing a correct password.
_DUMMY_PASSWORD_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


@router.post("/api/auth/login", response_model=AuthResponse)
def login(payload: LoginRequest, request: Request, response: Response, session: Session = Depends(get_session)):
    client = request.client.host if request.client else "unknown"
    key = f"login:{client}"
    # Only failed attempts count toward the limit, and a successful login
    # resets it — a legitimate organizer logging in repeatedly is never
    # penalised, only sustained brute-forcing is. Fails CLOSED: if the
    # limiter backend itself is unreachable, reject rather than silently
    # allow unlimited attempts during an outage.
    try:
        if peek_rate_limit_db(session, key, LOGIN_FAILURE_WINDOW_SECONDS) >= LOGIN_FAILURE_LIMIT:
            raise api_error(429, "RATE_LIMITED", "Too many login attempts. Please try again later.")
    except RateLimitBackendError:
        logger.error("rate_limit_backend_unavailable key=%s", key)
        raise api_error(429, "RATE_LIMITED", "Too many login attempts. Please try again later.")

    organizer = session.scalar(select(Organizer).where(Organizer.username == payload.username))
    valid = False
    if organizer and organizer.is_active:
        try: valid = PASSWORD_HASHER.verify(organizer.password_hash, payload.password)
        except VerifyMismatchError: valid = False
    else:
        # Burn the same Argon2 cost a real verify would take, so a
        # nonexistent/inactive username can't be distinguished from a wrong
        # password by response time alone.
        try: PASSWORD_HASHER.verify(_DUMMY_PASSWORD_HASH, payload.password)
        except VerifyMismatchError: pass
    if not valid:
        try:
            record_rate_limit_attempt_db(session, key, LOGIN_FAILURE_WINDOW_SECONDS)
        except RateLimitBackendError:
            logger.error("rate_limit_backend_unavailable key=%s", key)
        logger.warning("login_failed remote=%s", client)
        raise api_error(401, "INVALID_CREDENTIALS", "Invalid username or password.")

    raw = secrets.token_urlsafe(32); csrf = secrets.token_urlsafe(24)
    session.add(OrganizerSession(token_hash=token_hash(raw), csrf_token=csrf, organizer_id=organizer.id, expires_at=now_ist() + timedelta(hours=SESSION_HOURS)))
    session.commit()
    try:
        reset_rate_limit_db(session, key)
    except RateLimitBackendError:
        pass  # best-effort reset; a stale counter just means slightly stricter limiting next time, not a security issue
    response.set_cookie(SESSION_COOKIE, raw, httponly=True, secure=request.app.state.config.secure_cookies, samesite="lax", max_age=SESSION_HOURS * 3600, path="/")
    logger.info("login_succeeded organizer_id=%s", organizer.id)
    return {"organizer": {"username": organizer.username, "role": organizer.role}, "csrf_token": csrf}


@router.get("/api/auth/me", response_model=AuthResponse)
def me(context: tuple[Organizer, OrganizerSession] = Depends(auth_context)):
    return {"organizer": {"username": context[0].username, "role": context[0].role}, "csrf_token": context[1].csrf_token}


@router.post("/api/auth/logout", status_code=204)
def logout(response: Response, _: Organizer = Depends(require_csrf), context: tuple[Organizer, OrganizerSession] = Depends(auth_context), session: Session = Depends(get_session)):
    session.delete(context[1]); session.commit(); response.delete_cookie(SESSION_COOKIE, path="/")
    logger.info("logout organizer_id=%s", context[0].id)
