from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..deps import (
    get_session, organizer_events_filter, publish_event_update, require_admin, require_csrf,
    require_event_access, require_payment_access, require_registration_access,
)
from ..models import Match, Organizer, Payment, Registration
from ..schemas import AdminMatchDetail, EventSummary, EventUpdate, MatchCreate, MatchResponse, RegistrationResponse, RejectRequest
from ..services import (
    PAYMENT_SUBMITTED, cancel_registration, create_match, event_summary, match_to_response, promote_waitlisted,
    registration_to_response, update_match, verify_payment,
)

router = APIRouter()


@router.get("/api/admin/events", response_model=list[MatchResponse])
@router.get("/api/admin/matches", response_model=list[MatchResponse])
def admin_events(organizer: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
    records = session.scalars(select(Match).where(organizer_events_filter(organizer)).order_by(Match.date.desc(), Match.start_time.desc())).all()
    return [match_to_response(session, item) for item in records]


@router.post("/api/admin/events", response_model=MatchResponse, status_code=201)
@router.post("/api/admin/matches", response_model=MatchResponse, status_code=201)
def admin_create_event(payload: MatchCreate, request: Request, organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    match = create_match(session, payload, organizer); publish_event_update(request, session, match.id, "EVENT_CREATED"); return match_to_response(session, match)


@router.get("/api/admin/events/{match_id}", response_model=AdminMatchDetail)
@router.get("/api/admin/matches/{match_id}", response_model=AdminMatchDetail)
def admin_event_detail(match: Match = Depends(require_event_access), session: Session = Depends(get_session)):
    match = session.scalar(select(Match).options(joinedload(Match.registrations).joinedload(Registration.payment)).where(Match.id == match.id))
    data = match_to_response(session, match); data["registrations"] = [registration_to_response(item, include_proof=True) for item in sorted(match.registrations, key=lambda item: item.created_at, reverse=True)]; return data


@router.patch("/api/admin/events/{match_id}", response_model=MatchResponse)
def admin_update_event(payload: EventUpdate, request: Request, match: Match = Depends(require_event_access), organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    match = update_match(session, match, payload, organizer); publish_event_update(request, session, match.id, "EVENT_UPDATED"); return match_to_response(session, match)


@router.get("/api/admin/events/{match_id}/summary", response_model=EventSummary)
def admin_summary(match: Match = Depends(require_event_access), session: Session = Depends(get_session)):
    return event_summary(session, match)


@router.get("/api/admin/events/{match_id}/registrations", response_model=list[RegistrationResponse])
@router.get("/api/admin/matches/{match_id}/registrations", response_model=list[RegistrationResponse])
def admin_registrations(match: Match = Depends(require_event_access), session: Session = Depends(get_session)):
    records = session.scalars(select(Registration).options(joinedload(Registration.payment)).where(Registration.match_id == match.id).order_by(Registration.created_at.desc())).all()
    return [registration_to_response(item, include_proof=True) for item in records]


@router.get("/api/admin/payments/pending", response_model=list[RegistrationResponse])
def pending_payments(organizer: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
    records = session.scalars(
        select(Registration).options(joinedload(Registration.payment)).join(Payment).join(Match)
        .where(Payment.status == PAYMENT_SUBMITTED, organizer_events_filter(organizer))
        .order_by(Payment.submitted_at)
    ).all()
    return [registration_to_response(item, include_proof=True) for item in records]


@router.post("/api/admin/payments/{payment_id}/confirm", response_model=RegistrationResponse)
def confirm_payment(request: Request, payment: Payment = Depends(require_payment_access), organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    registration = verify_payment(session, payment.id, approve=True, actor_id=organizer.id); publish_event_update(request, session, registration.match_id, "PAYMENT_VERIFIED"); return registration_to_response(registration, include_proof=True)


@router.post("/api/admin/payments/{payment_id}/reject", response_model=RegistrationResponse)
def reject_payment(payload: RejectRequest, request: Request, payment: Payment = Depends(require_payment_access), organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    registration = verify_payment(session, payment.id, approve=False, actor_id=organizer.id, reason=payload.reason.strip()); publish_event_update(request, session, registration.match_id, "PAYMENT_REJECTED"); return registration_to_response(registration, include_proof=True)


@router.post("/api/admin/registrations/{registration_id}/promote", response_model=RegistrationResponse)
def promote(request: Request, registration: Registration = Depends(require_registration_access), organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    registration = promote_waitlisted(session, registration.id, actor_id=organizer.id); publish_event_update(request, session, registration.match_id, "WAITLIST_UPDATED"); return registration_to_response(registration, include_proof=True)


@router.post("/api/admin/registrations/{registration_id}/cancel", response_model=RegistrationResponse)
def admin_cancel_registration(request: Request, registration: Registration = Depends(require_registration_access), organizer: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    updated = cancel_registration(session, registration.id, actor_type="ORGANIZER", actor_id=organizer.id)
    publish_event_update(request, session, updated.match_id, "REGISTRATION_CANCELLED")
    return registration_to_response(updated, include_proof=True)
