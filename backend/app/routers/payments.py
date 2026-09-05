from __future__ import annotations

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..deps import (
    PAYMENT_UPLOAD_RATE_LIMIT, PAYMENT_UPLOAD_RATE_WINDOW_SECONDS, enforce_rate_limit, get_session,
    organizer_owns_event, publish_event_update, require_admin, require_player_csrf,
)
from ..models import Organizer, Payment, Registration, User
from ..schemas import RegistrationResponse
from ..services import api_error, registration_to_response, submit_payment

router = APIRouter()


@router.post("/api/registrations/{registration_key}/payment", response_model=RegistrationResponse)
async def upload_payment(request: Request, registration_key: str, screenshot: UploadFile = File(...), session: Session = Depends(get_session), player: User = Depends(require_player_csrf)):
    client = request.client.host if request.client else "unknown"
    enforce_rate_limit(request.app.state.payment_upload_attempts, client, PAYMENT_UPLOAD_RATE_LIMIT, PAYMENT_UPLOAD_RATE_WINDOW_SECONDS)
    registration = await submit_payment(session, registration_key, screenshot, request.app.state.uploads_dir, player.id); publish_event_update(request, session, registration.match_id, "PAYMENT_SUBMITTED"); return registration_to_response(registration)


@router.get("/api/payment-proofs/{token}")
def payment_proof(token: str, request: Request, organizer: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
    payment = session.scalar(select(Payment).options(joinedload(Payment.registration).joinedload(Registration.match)).where(Payment.screenshot_token == token))
    if not payment or not organizer_owns_event(organizer, payment.registration.match):
        raise api_error(404, "RESOURCE_NOT_FOUND", "Payment proof not found")
    file_path = request.app.state.uploads_dir / payment.screenshot_path
    if not file_path.is_file(): raise api_error(404, "RESOURCE_NOT_FOUND", "Payment proof not found")
    return FileResponse(file_path)
