from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..deps import (
    PAYMENT_UPLOAD_RATE_LIMIT, PAYMENT_UPLOAD_RATE_WINDOW_SECONDS, get_session,
    publish_event_update, require_player, require_player_csrf, require_proof_access,
)
from ..models import PaymentProof, Registration, User
from ..rate_limit import enforce_rate_limit_db
from ..schemas import RegistrationResponse
from ..services import api_error, assert_registration_owner, registration_to_response, submit_payment_proof
from ..storage import Storage, StorageUnavailableError

router = APIRouter()

# Presigned URLs are minted fresh on every authorized request and expire
# quickly — the signature only ever grants time-boxed access to one object;
# ownership is decided by the authorization dependency on every single
# request, never by whether a URL happens to still be valid.
PRESIGNED_URL_TTL_SECONDS = 60


def _serve_object(storage: Storage, key: str) -> Response:
    """Authorization must already have happened before this is called. On an
    S3-compatible backend, redirects the client to a short-lived presigned
    URL so the app server never relays the binary payload itself. On local
    storage (dev/test), streams the bytes directly — there's no presigning
    concept to redirect to."""
    try:
        presigned = storage.presigned_url(key, expires_in=PRESIGNED_URL_TTL_SECONDS)
        if presigned:
            return RedirectResponse(presigned, status_code=302)
        found = storage.get(key)
    except StorageUnavailableError:
        raise api_error(503, "STORAGE_UNAVAILABLE", "This file is temporarily unavailable. Please try again.")
    if not found:
        raise api_error(404, "RESOURCE_NOT_FOUND", "File not found")
    content, content_type = found
    return Response(content=content, media_type=content_type)


@router.post("/api/registrations/{registration_key}/payment", response_model=RegistrationResponse)
async def upload_payment(
    request: Request, registration_key: str, screenshot: UploadFile = File(...), utr: str | None = Form(None),
    session: Session = Depends(get_session), player: User = Depends(require_player_csrf),
):
    client = request.client.host if request.client else "unknown"
    enforce_rate_limit_db(session, f"payment_upload:{client}", PAYMENT_UPLOAD_RATE_LIMIT, PAYMENT_UPLOAD_RATE_WINDOW_SECONDS)
    try:
        registration = await submit_payment_proof(session, registration_key, screenshot, utr, request.app.state.proof_storage, player.id)
    except StorageUnavailableError:
        raise api_error(503, "STORAGE_UNAVAILABLE", "Upload service is temporarily unavailable. Please try again.")
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
    return _serve_object(request.app.state.qr_storage, payment.qr_storage_key_snapshot)


@router.get("/api/payment-proofs/{proof_id}")
def payment_proof(proof_id: int, request: Request, proof: PaymentProof = Depends(require_proof_access)):
    return _serve_object(request.app.state.proof_storage, proof.storage_key)
