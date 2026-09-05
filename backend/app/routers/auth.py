from __future__ import annotations

import logging
import os
import secrets
import time
from datetime import timedelta

from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import PASSWORD_HASHER, SESSION_COOKIE, SESSION_HOURS, auth_context, get_session, require_csrf, token_hash
from ..models import Organizer, OrganizerSession, now_ist
from ..schemas import AuthResponse, LoginRequest
from ..services import api_error

logger = logging.getLogger("stranger_club")
router = APIRouter()


@router.post("/api/auth/login", response_model=AuthResponse)
def login(payload: LoginRequest, request: Request, response: Response, session: Session = Depends(get_session)):
    client = request.client.host if request.client else "unknown"; attempts = [value for value in request.app.state.login_attempts.get(client, []) if time.time() - value < 900]
    request.app.state.login_attempts[client] = attempts
    if len(attempts) >= 8: raise api_error(429, "RATE_LIMITED", "Too many login attempts. Please try again later.")
    organizer = session.scalar(select(Organizer).where(Organizer.username == payload.username))
    valid = False
    if organizer and organizer.is_active:
        try: valid = PASSWORD_HASHER.verify(organizer.password_hash, payload.password)
        except VerifyMismatchError: valid = False
    if not valid:
        attempts.append(time.time()); request.app.state.login_attempts[client] = attempts
        logger.warning("login_failed remote=%s", client)
        raise api_error(401, "INVALID_CREDENTIALS", "Invalid username or password.")
    raw = secrets.token_urlsafe(32); csrf = secrets.token_urlsafe(24)
    session.add(OrganizerSession(token_hash=token_hash(raw), csrf_token=csrf, organizer_id=organizer.id, expires_at=now_ist() + timedelta(hours=SESSION_HOURS)))
    session.commit(); request.app.state.login_attempts.pop(client, None)
    response.set_cookie(SESSION_COOKIE, raw, httponly=True, secure=os.getenv("SC_COOKIE_SECURE", "false").lower() == "true", samesite="lax", max_age=SESSION_HOURS * 3600, path="/")
    logger.info("login_succeeded organizer_id=%s", organizer.id)
    return {"organizer": {"username": organizer.username, "role": organizer.role}, "csrf_token": csrf}


@router.get("/api/auth/me", response_model=AuthResponse)
def me(context: tuple[Organizer, OrganizerSession] = Depends(auth_context)):
    return {"organizer": {"username": context[0].username, "role": context[0].role}, "csrf_token": context[1].csrf_token}


@router.post("/api/auth/logout", status_code=204)
def logout(response: Response, _: Organizer = Depends(require_csrf), context: tuple[Organizer, OrganizerSession] = Depends(auth_context), session: Session = Depends(get_session)):
    session.delete(context[1]); session.commit(); response.delete_cookie(SESSION_COOKIE, path="/")
    logger.info("logout organizer_id=%s", context[0].id)
