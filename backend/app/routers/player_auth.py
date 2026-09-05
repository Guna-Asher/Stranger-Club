from __future__ import annotations

import logging
import os
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..deps import (
    OTP_REQUEST_IP_LIMIT, OTP_REQUEST_IP_WINDOW_SECONDS, OTP_REQUEST_PHONE_LIMIT,
    OTP_REQUEST_PHONE_WINDOW_SECONDS, OTP_VERIFY_IP_LIMIT, OTP_VERIFY_IP_WINDOW_SECONDS,
    PLAYER_SESSION_COOKIE, PLAYER_SESSION_DAYS, enforce_rate_limit, get_session,
    player_auth_context, require_player_csrf, token_hash,
)
from ..models import PlayerSession, User, now_ist
from ..schemas import OtpRequest, OtpVerify, PlayerAuthResponse
from ..services_player import request_otp, verify_otp

logger = logging.getLogger("stranger_club")
router = APIRouter()

# Deliberately identical regardless of whether the phone is new or already
# registered, so this endpoint can't be used to enumerate accounts.
GENERIC_OTP_RESPONSE = {"status": "sent"}


@router.post("/api/player/otp/request")
def otp_request(payload: OtpRequest, request: Request, session: Session = Depends(get_session)):
    client = request.client.host if request.client else "unknown"
    enforce_rate_limit(request.app.state.otp_request_ip_attempts, client, OTP_REQUEST_IP_LIMIT, OTP_REQUEST_IP_WINDOW_SECONDS)
    enforce_rate_limit(request.app.state.otp_request_phone_attempts, payload.phone, OTP_REQUEST_PHONE_LIMIT, OTP_REQUEST_PHONE_WINDOW_SECONDS)
    request_otp(session, payload.phone, request.app.state.otp_provider)
    return GENERIC_OTP_RESPONSE


@router.post("/api/player/otp/verify", response_model=PlayerAuthResponse)
def otp_verify(payload: OtpVerify, request: Request, response: Response, session: Session = Depends(get_session)):
    client = request.client.host if request.client else "unknown"
    enforce_rate_limit(request.app.state.otp_verify_ip_attempts, client, OTP_VERIFY_IP_LIMIT, OTP_VERIFY_IP_WINDOW_SECONDS)
    user = verify_otp(session, payload.phone, payload.code)

    # Session rotation: replace whatever session cookie the browser already
    # carried (if any) rather than layering a new one on top of it.
    existing_raw = request.cookies.get(PLAYER_SESSION_COOKIE)
    if existing_raw:
        session.execute(text("DELETE FROM player_sessions WHERE token_hash = :token_hash"), {"token_hash": token_hash(existing_raw)})

    raw = secrets.token_urlsafe(32); csrf = secrets.token_urlsafe(24)
    session.add(PlayerSession(token_hash=token_hash(raw), csrf_token=csrf, user_id=user.id, expires_at=now_ist() + timedelta(days=PLAYER_SESSION_DAYS)))
    session.commit()
    response.set_cookie(
        PLAYER_SESSION_COOKIE, raw, httponly=True,
        secure=os.getenv("SC_COOKIE_SECURE", "false").lower() == "true",
        samesite="lax", max_age=PLAYER_SESSION_DAYS * 86400, path="/",
    )
    return {"phone": user.phone, "csrf_token": csrf}


@router.get("/api/player/me", response_model=PlayerAuthResponse)
def me(context: tuple[User, PlayerSession] = Depends(player_auth_context)):
    return {"phone": context[0].phone, "csrf_token": context[1].csrf_token}


@router.post("/api/player/logout", status_code=204)
def logout(response: Response, _: User = Depends(require_player_csrf), context: tuple[User, PlayerSession] = Depends(player_auth_context), session: Session = Depends(get_session)):
    session.delete(context[1]); session.commit(); response.delete_cookie(PLAYER_SESSION_COOKIE, path="/")
