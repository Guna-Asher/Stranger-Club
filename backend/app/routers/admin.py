from __future__ import annotations

from fastapi import APIRouter, Depends, File, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..deps import (
    get_session, organizer_events_filter, publish_event_update, require_admin, require_csrf,
    require_event_access, require_payment_access, require_registration_access,
)
from ..models import Match, Organizer, Payment, Registration
from ..schemas import (
    AdminMatchDetail, EventSummary, EventUpdate, MatchCreate, MatchResponse, PaymentConfigurationResponse,
    PaymentConfigurationUpdate, RegistrationResponse, RejectRequest,
)
from ..services import (
    PAYMENT_SUBMITTED, api_error, cancel_registration, create_match, event_summary, match_to_response,
    payment_configuration_to_response, promote_waitlisted, registration_to_response, review_payment,
    set_payment_configuration_qr, update_match, update_payment_configuration,
)
from ..storage import StorageUnavailableError

router = APIRouter()


@router.get("/api/admin/events", response_model=list[MatchResponse])
@router.get("/api/admin/matches", response_model=list[MatchResponse])
def admin_events(organizer: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
    records = session.scalars(select(Match).options(joinedload(Match.payment_configuration)).where(organizer_events_filter(organizer)).order_by(Match.date.desc(), Match.start_time.desc())).all()
    return [match_to_response(session, item) for item in records]


@router.post("/api/admin/events", response_model=MatchResponse, status_code=201)
@router.post("/api/admin/matches", response_model=MatchResponse, status_code=201)
def admin_create_event(payload: MatchCreate, request: Request, organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    match = create_match(session, payload, organizer); publish_event_update(request, session, match.id, "EVENT_CREATED"); return match_to_response(session, match)


@router.get("/api/admin/events/{match_id}", response_model=AdminMatchDetail)
@router.get("/api/admin/matches/{match_id}", response_model=AdminMatchDetail)
def admin_event_detail(match: Match = Depends(require_event_access), session: Session = Depends(get_session)):
    match = session.scalar(select(Match).options(joinedload(Match.registrations).joinedload(Registration.payment).joinedload(Payment.proofs)).where(Match.id == match.id))
    data = match_to_response(session, match); data["registrations"] = [registration_to_response(item, viewer="organizer", session=session) for item in sorted(match.registrations, key=lambda item: item.created_at, reverse=True)]; return data


@router.patch("/api/admin/events/{match_id}", response_model=MatchResponse)
def admin_update_event(payload: EventUpdate, request: Request, match: Match = Depends(require_event_access), organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    match = update_match(session, match, payload, organizer); publish_event_update(request, session, match.id, "EVENT_UPDATED"); return match_to_response(session, match)


@router.get("/api/admin/events/{match_id}/summary", response_model=EventSummary)
def admin_summary(match: Match = Depends(require_event_access), session: Session = Depends(get_session)):
    return event_summary(session, match)


@router.patch("/api/admin/events/{match_id}/payment-configuration", response_model=PaymentConfigurationResponse)
def admin_update_payment_configuration(
    payload: PaymentConfigurationUpdate, match: Match = Depends(require_event_access),
    organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session),
):
    config = update_payment_configuration(session, match, payload, organizer)
    return payment_configuration_to_response(config)


@router.post("/api/admin/events/{match_id}/payment-configuration/qr", response_model=PaymentConfigurationResponse)
async def admin_upload_payment_qr(
    request: Request, qr_image: UploadFile = File(...), match: Match = Depends(require_event_access),
    organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session),
):
    try:
        config = await set_payment_configuration_qr(session, match, qr_image, request.app.state.qr_storage, organizer)
    except StorageUnavailableError:
        raise api_error(503, "STORAGE_UNAVAILABLE", "Upload service is temporarily unavailable. Please try again.")
    return payment_configuration_to_response(config)


@router.get("/api/admin/events/{match_id}/registrations", response_model=list[RegistrationResponse])
@router.get("/api/admin/matches/{match_id}/registrations", response_model=list[RegistrationResponse])
def admin_registrations(match: Match = Depends(require_event_access), session: Session = Depends(get_session)):
    records = session.scalars(select(Registration).options(joinedload(Registration.payment).joinedload(Payment.proofs)).where(Registration.match_id == match.id).order_by(Registration.created_at.desc())).unique().all()
    return [registration_to_response(item, viewer="organizer", session=session) for item in records]


@router.get("/api/admin/payments/pending", response_model=list[RegistrationResponse])
def pending_payments(organizer: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
    records = session.scalars(
        select(Registration).options(joinedload(Registration.payment).joinedload(Payment.proofs)).join(Payment).join(Match)
        .where(Payment.status == PAYMENT_SUBMITTED, organizer_events_filter(organizer))
        .order_by(Payment.submitted_at)
    ).unique().all()
    return [registration_to_response(item, viewer="organizer", session=session) for item in records]


@router.post("/api/admin/payments/{payment_id}/confirm", response_model=RegistrationResponse)
def confirm_payment(request: Request, payment: Payment = Depends(require_payment_access), organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    registration = review_payment(session, payment.id, approve=True, actor_id=organizer.id); publish_event_update(request, session, registration.match_id, "PAYMENT_VERIFIED"); return registration_to_response(registration, viewer="organizer", session=session)


@router.post("/api/admin/payments/{payment_id}/reject", response_model=RegistrationResponse)
def reject_payment(payload: RejectRequest, request: Request, payment: Payment = Depends(require_payment_access), organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    registration = review_payment(session, payment.id, approve=False, actor_id=organizer.id, reason=payload.reason.strip()); publish_event_update(request, session, registration.match_id, "PAYMENT_REJECTED"); return registration_to_response(registration, viewer="organizer", session=session)


@router.post("/api/admin/registrations/{registration_id}/promote", response_model=RegistrationResponse)
def promote(request: Request, registration: Registration = Depends(require_registration_access), organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    registration = promote_waitlisted(session, registration.id, actor_id=organizer.id); publish_event_update(request, session, registration.match_id, "WAITLIST_UPDATED"); return registration_to_response(registration, viewer="organizer", session=session)


@router.post("/api/admin/registrations/{registration_id}/cancel", response_model=RegistrationResponse)
def admin_cancel_registration(request: Request, registration: Registration = Depends(require_registration_access), organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    updated = cancel_registration(session, registration.id, actor_type="ORGANIZER", actor_id=organizer.id)
    publish_event_update(request, session, updated.match_id, "REGISTRATION_CANCELLED")
    return registration_to_response(updated, viewer="organizer", session=session)
