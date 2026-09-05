from __future__ import annotations

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import (
    PAYMENT_UPLOAD_RATE_LIMIT, PAYMENT_UPLOAD_RATE_WINDOW_SECONDS, enforce_rate_limit, get_session,
    publish_event_update, require_admin,
)
from ..models import Organizer, Payment
from ..schemas import RegistrationResponse
from ..services import api_error, registration_to_response, submit_payment

router = APIRouter()


@router.post("/api/registrations/{registration_key}/payment", response_model=RegistrationResponse)
async def upload_payment(request: Request, registration_key: str, screenshot: UploadFile = File(...), session: Session = Depends(get_session)):
    client = request.client.host if request.client else "unknown"
    enforce_rate_limit(request.app.state.payment_upload_attempts, client, PAYMENT_UPLOAD_RATE_LIMIT, PAYMENT_UPLOAD_RATE_WINDOW_SECONDS)
    registration = await submit_payment(session, registration_key, screenshot, request.app.state.uploads_dir); publish_event_update(request, session, registration.match_id, "PAYMENT_SUBMITTED"); return registration_to_response(registration)


@router.get("/api/payment-proofs/{token}")
def payment_proof(token: str, request: Request, _: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
    payment = session.scalar(select(Payment).where(Payment.screenshot_token == token))
    if not payment: raise api_error(404, "RESOURCE_NOT_FOUND", "Payment proof not found")
    file_path = request.app.state.uploads_dir / payment.screenshot_path
    if not file_path.is_file(): raise api_error(404, "RESOURCE_NOT_FOUND", "Payment proof not found")
    return FileResponse(file_path)
