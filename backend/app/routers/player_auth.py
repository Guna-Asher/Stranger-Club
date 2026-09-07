from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sqlalchemy import select

from ..deps import (
    AVATAR_CHANGE_LIMIT, AVATAR_CHANGE_WINDOW_SECONDS, OTP_REQUEST_IP_LIMIT, OTP_REQUEST_IP_WINDOW_SECONDS,
    OTP_REQUEST_PHONE_LIMIT, OTP_REQUEST_PHONE_WINDOW_SECONDS, OTP_VERIFY_IP_LIMIT, OTP_VERIFY_IP_WINDOW_SECONDS,
    PLAYER_LOGIN_FAILURE_LIMIT, PLAYER_LOGIN_FAILURE_WINDOW_SECONDS, PLAYER_SESSION_COOKIE, PLAYER_SESSION_DAYS,
    PLAYER_SIGNUP_IP_LIMIT, PLAYER_SIGNUP_IP_WINDOW_SECONDS, get_session,
    player_auth_context, require_player, require_player_csrf, token_hash,
)
from ..models import PlayerProfile, PlayerSession, User, now_ist
from ..rate_limit import RateLimitBackendError, enforce_rate_limit_db, peek_rate_limit_db, record_rate_limit_attempt_db, reset_rate_limit_db
from ..schemas import (
    AvatarCatalogStatus, AvatarChoose, OtpRequest, OtpVerify, PlayerAuthResponse, PlayerDashboardResponse,
    PlayerLoginRequest, PlayerProfileResponse, PlayerProfileUpdate, PlayerSignupRequest,
)
from ..services import api_error, assign_new_avatar, avatar_catalog_status, choose_avatar, player_dashboard
from ..services_player import authenticate_player, create_player_account, request_otp, verify_otp

logger = logging.getLogger("stranger_club")
router = APIRouter()

# Deliberately identical regardless of whether the phone is new or already
# registered, so this endpoint can't be used to enumerate accounts.
GENERIC_OTP_RESPONSE = {"status": "sent"}


def _profile_response(user: User, profile: PlayerProfile) -> dict:
    """email/phone are merged in from User/PlayerProfile respectively —
    email lives on User (the login identifier), phone is profile-only
    information (see PlayerProfile.phone's docstring) — into the single
    shape every profile-returning endpoint below sends back."""
    return {
        "display_name": profile.display_name,
        "cricket_role": profile.cricket_role,
        "skill_rating": profile.skill_rating,
        "bio": profile.bio,
        "avatar_design_id": profile.avatar_design_id,
        "email": user.email,
        "phone": profile.phone,
    }


def _start_player_session(user: User, request: Request, response: Response, session: Session) -> dict:
    """Shared by every path that ends in an authenticated player session —
    OTP verify, email+password signup, and email+password login all rotate
    the session cookie the exact same way, so there is only ever one place
    a player session token gets minted."""
    existing_raw = request.cookies.get(PLAYER_SESSION_COOKIE)
    if existing_raw:
        session.execute(text("DELETE FROM player_sessions WHERE token_hash = :token_hash"), {"token_hash": token_hash(existing_raw)})

    raw = secrets.token_urlsafe(32); csrf = secrets.token_urlsafe(24)
    session.add(PlayerSession(token_hash=token_hash(raw), csrf_token=csrf, user_id=user.id, expires_at=now_ist() + timedelta(days=PLAYER_SESSION_DAYS)))
    session.commit()
    response.set_cookie(
        PLAYER_SESSION_COOKIE, raw, httponly=True,
        secure=request.app.state.config.secure_cookies,
        samesite="lax", max_age=PLAYER_SESSION_DAYS * 86400, path="/",
    )
    return {"email": user.email, "phone": user.phone, "csrf_token": csrf}


@router.post("/api/player/otp/request")
def otp_request(payload: OtpRequest, request: Request, session: Session = Depends(get_session)):
    client = request.client.host if request.client else "unknown"
    enforce_rate_limit_db(session, f"otp_request_ip:{client}", OTP_REQUEST_IP_LIMIT, OTP_REQUEST_IP_WINDOW_SECONDS)
    enforce_rate_limit_db(session, f"otp_request_phone:{payload.phone}", OTP_REQUEST_PHONE_LIMIT, OTP_REQUEST_PHONE_WINDOW_SECONDS)
    request_otp(session, payload.phone, request.app.state.otp_provider)
    return GENERIC_OTP_RESPONSE


@router.post("/api/player/otp/verify", response_model=PlayerAuthResponse)
def otp_verify(payload: OtpVerify, request: Request, response: Response, session: Session = Depends(get_session)):
    client = request.client.host if request.client else "unknown"
    enforce_rate_limit_db(session, f"otp_verify_ip:{client}", OTP_VERIFY_IP_LIMIT, OTP_VERIFY_IP_WINDOW_SECONDS)
    user = verify_otp(session, payload.phone, payload.code)
    return _start_player_session(user, request, response, session)


# ---------------------------------------------------------------------------
# Email + password: the current player onboarding/login path. The OTP
# endpoints above remain fully functional (see services_player.py) — this is
# additive, not a replacement of that architecture.
# ---------------------------------------------------------------------------

@router.post("/api/player/signup", response_model=PlayerAuthResponse)
def player_signup(payload: PlayerSignupRequest, request: Request, response: Response, session: Session = Depends(get_session)):
    client = request.client.host if request.client else "unknown"
    enforce_rate_limit_db(session, f"player_signup_ip:{client}", PLAYER_SIGNUP_IP_LIMIT, PLAYER_SIGNUP_IP_WINDOW_SECONDS)
    user = create_player_account(session, name=payload.name, email=payload.email, phone=payload.phone, password=payload.password)
    logger.info("player_signup_succeeded user_id=%s", user.id)
    return _start_player_session(user, request, response, session)


@router.post("/api/player/login", response_model=PlayerAuthResponse)
def player_login(payload: PlayerLoginRequest, request: Request, response: Response, session: Session = Depends(get_session)):
    client = request.client.host if request.client else "unknown"
    key = f"player_login:{client}"
    # Same shape as routers/auth.py's organizer login: only failed attempts
    # count, a success resets the counter, and the limiter fails CLOSED if
    # its own backend is unreachable.
    try:
        if peek_rate_limit_db(session, key, PLAYER_LOGIN_FAILURE_WINDOW_SECONDS) >= PLAYER_LOGIN_FAILURE_LIMIT:
            raise api_error(429, "RATE_LIMITED", "Too many login attempts. Please try again later.")
    except RateLimitBackendError:
        logger.error("rate_limit_backend_unavailable key=%s", key)
        raise api_error(429, "RATE_LIMITED", "Too many login attempts. Please try again later.")

    user = authenticate_player(session, payload.email, payload.password)
    if not user:
        try:
            record_rate_limit_attempt_db(session, key, PLAYER_LOGIN_FAILURE_WINDOW_SECONDS)
        except RateLimitBackendError:
            logger.error("rate_limit_backend_unavailable key=%s", key)
        logger.warning("player_login_failed remote=%s", client)
        # Deliberately generic — never reveals whether the email exists.
        raise api_error(401, "INVALID_CREDENTIALS", "Invalid email or password.")

    try:
        reset_rate_limit_db(session, key)
    except RateLimitBackendError:
        pass  # best-effort reset; a stale counter just means slightly stricter limiting next time
    user.last_login_at = now_ist()
    session.commit()
    logger.info("player_login_succeeded user_id=%s", user.id)
    return _start_player_session(user, request, response, session)


@router.get("/api/player/me", response_model=PlayerAuthResponse)
def me(context: tuple[User, PlayerSession] = Depends(player_auth_context)):
    return {"phone": context[0].phone, "email": context[0].email, "csrf_token": context[1].csrf_token}


@router.post("/api/player/logout", status_code=204)
def logout(response: Response, _: User = Depends(require_player_csrf), context: tuple[User, PlayerSession] = Depends(player_auth_context), session: Session = Depends(get_session)):
    session.delete(context[1]); session.commit(); response.delete_cookie(PLAYER_SESSION_COOKIE, path="/")


@router.get("/api/player/profile", response_model=PlayerProfileResponse)
def get_profile(player: User = Depends(require_player), session: Session = Depends(get_session)):
    profile = session.scalar(select(PlayerProfile).where(PlayerProfile.user_id == player.id))
    return _profile_response(player, profile)


@router.patch("/api/player/profile", response_model=PlayerProfileResponse)
def update_profile(payload: PlayerProfileUpdate, player: User = Depends(require_player_csrf), session: Session = Depends(get_session)):
    # The authenticated identity comes only from the session (`player`,
    # resolved server-side by require_player_csrf) — there is no request
    # field a client could supply to target another player's account, which
    # is what actually makes "a player can only edit their own email/phone/
    # profile" true, not just documented.
    profile = session.scalar(select(PlayerProfile).where(PlayerProfile.user_id == player.id))
    data = payload.model_dump(exclude_unset=True)
    new_email = data.pop("email", None)
    if new_email is not None and new_email != player.email:
        if session.scalar(select(User).where(func.lower(User.email) == new_email, User.id != player.id)):
            raise api_error(409, "EMAIL_TAKEN", "An account with this email already exists.")
        player.email = new_email
    for field, value in data.items():
        setattr(profile, field, value)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise api_error(409, "EMAIL_TAKEN", "An account with this email already exists.")
    session.refresh(profile); session.refresh(player)
    return _profile_response(player, profile)


@router.get("/api/player/matches", response_model=PlayerDashboardResponse)
def player_matches(player: User = Depends(require_player), session: Session = Depends(get_session)):
    return player_dashboard(session, player)


# ---------------------------------------------------------------------------
# Avatars. Every endpoint below resolves "which profile" purely from the
# session-authenticated player (require_player / require_player_csrf) — there
# is no request field a client could supply to target another player's
# profile, which is what actually makes "a player can only change their own
# avatar" true, not just documented.
# ---------------------------------------------------------------------------

@router.get("/api/player/avatar/options", response_model=AvatarCatalogStatus)
def get_avatar_options(player: User = Depends(require_player), session: Session = Depends(get_session)):
    profile = session.scalar(select(PlayerProfile).where(PlayerProfile.user_id == player.id))
    return avatar_catalog_status(session, profile)


@router.post("/api/player/avatar/generate", response_model=PlayerProfileResponse)
def generate_avatar(player: User = Depends(require_player_csrf), session: Session = Depends(get_session)):
    enforce_rate_limit_db(session, f"avatar_change:{player.id}", AVATAR_CHANGE_LIMIT, AVATAR_CHANGE_WINDOW_SECONDS)
    profile = session.scalar(select(PlayerProfile).where(PlayerProfile.user_id == player.id))
    return _profile_response(player, assign_new_avatar(session, profile))


@router.put("/api/player/avatar", response_model=PlayerProfileResponse)
def put_avatar(payload: AvatarChoose, player: User = Depends(require_player_csrf), session: Session = Depends(get_session)):
    enforce_rate_limit_db(session, f"avatar_change:{player.id}", AVATAR_CHANGE_LIMIT, AVATAR_CHANGE_WINDOW_SECONDS)
    profile = session.scalar(select(PlayerProfile).where(PlayerProfile.user_id == player.id))
    return _profile_response(player, choose_avatar(session, profile, payload.design_id))
