from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..deps import (
    REGISTRATION_RATE_LIMIT, REGISTRATION_RATE_WINDOW_SECONDS, enforce_rate_limit, get_session,
    publish_event_update, require_player, require_player_csrf,
)
from ..models import Registration, User
from ..schemas import RegistrationCreate, RegistrationResponse
from ..services import api_error, assert_registration_owner, cancel_registration, create_registration, get_public_match, registration_to_response

router = APIRouter()


@router.post("/api/events/{public_id}/registrations", response_model=RegistrationResponse, status_code=201)
@router.post("/api/matches/{public_id}/registrations", response_model=RegistrationResponse, status_code=201)
def register(public_id: str, payload: RegistrationCreate, request: Request, session: Session = Depends(get_session), player: User = Depends(require_player_csrf)):
    client = request.client.host if request.client else "unknown"
    enforce_rate_limit(request.app.state.registration_attempts, client, REGISTRATION_RATE_LIMIT, REGISTRATION_RATE_WINDOW_SECONDS)
    registration = create_registration(session, get_public_match(session, public_id), payload, player); publish_event_update(request, session, registration.match_id, "REGISTRATION_CREATED"); return registration_to_response(registration)


@router.get("/api/registrations/{registration_key}", response_model=RegistrationResponse)
def registration_status(registration_key: str, session: Session = Depends(get_session), player: User = Depends(require_player)):
    registration = session.scalar(select(Registration).options(joinedload(Registration.payment)).where(Registration.public_id == registration_key))
    if not registration: raise api_error(404, "RESOURCE_NOT_FOUND", "Registration not found")
    assert_registration_owner(registration, player.id)
    return registration_to_response(registration)


@router.post("/api/registrations/{registration_key}/cancel", response_model=RegistrationResponse)
def cancel_own_registration(registration_key: str, request: Request, session: Session = Depends(get_session), player: User = Depends(require_player_csrf)):
    registration = session.scalar(select(Registration).where(Registration.public_id == registration_key))
    if not registration: raise api_error(404, "RESOURCE_NOT_FOUND", "Registration not found")
    assert_registration_owner(registration, player.id)
    updated = cancel_registration(session, registration.id, actor_type="PLAYER", actor_id=player.id)
    publish_event_update(request, session, updated.match_id, "REGISTRATION_CANCELLED")
    return registration_to_response(updated)
