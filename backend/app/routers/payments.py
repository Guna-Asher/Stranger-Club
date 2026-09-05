from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..deps import (
    PAYMENT_UPLOAD_RATE_LIMIT, PAYMENT_UPLOAD_RATE_WINDOW_SECONDS, enforce_rate_limit, get_session,
    publish_event_update, require_player, require_player_csrf, require_proof_access,
)
from ..models import PaymentProof, Registration, User
from ..schemas import RegistrationResponse
from ..services import api_error, assert_registration_owner, registration_to_response, submit_payment_proof
from ..storage import Storage

router = APIRouter()


@router.post("/api/registrations/{registration_key}/payment", response_model=RegistrationResponse)
async def upload_payment(
    request: Request, registration_key: str, screenshot: UploadFile = File(...), utr: str | None = Form(None),
    session: Session = Depends(get_session), player: User = Depends(require_player_csrf),
):
    client = request.client.host if request.client else "unknown"
    enforce_rate_limit(request.app.state.payment_upload_attempts, client, PAYMENT_UPLOAD_RATE_LIMIT, PAYMENT_UPLOAD_RATE_WINDOW_SECONDS)
    registration = await submit_payment_proof(session, registration_key, screenshot, utr, request.app.state.proof_storage, player.id)
    publish_event_update(request, session, registration.match_id, "PAYMENT_PROOF_SUBMITTED")
    return registration_to_response(registration)


@router.get("/api/registrations/{registration_key}/payment-qr")
def payment_qr(registration_key: str, request: Request, session: Session = Depends(get_session), player: User = Depends(require_player)):
    """Serves the QR image identity frozen onto this specific Payment at
    creation time — never the event's current live configuration, and never
    a client-supplied storage key. Only reachable by the owning player."""
    registration = session.scalar(select(Registration).options(joinedload(Registration.payment)).where(Registration.public_id == registration_key))
    if not registration or not registration.payment:
        raise api_error(404, "RESOURCE_NOT_FOUND", "Payment not found")
    assert_registration_owner(registration, player.id)
    payment = registration.payment
    if payment.qr_source_snapshot != "UPLOADED" or not payment.qr_storage_key_snapshot:
        raise api_error(404, "RESOURCE_NOT_FOUND", "No custom QR configured for this payment")
    storage: Storage = request.app.state.qr_storage
    path = storage.path_for(payment.qr_storage_key_snapshot)
    if not path:
        raise api_error(404, "RESOURCE_NOT_FOUND", "QR asset not found")
    return FileResponse(path)


@router.get("/api/payment-proofs/{proof_id}")
def payment_proof(proof_id: int, request: Request, proof: PaymentProof = Depends(require_proof_access)):
    storage: Storage = request.app.state.proof_storage
    path = storage.path_for(proof.storage_key)
    if not path:
        raise api_error(404, "RESOURCE_NOT_FOUND", "Payment proof not found")
    return FileResponse(path)
