from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..deps import get_session, publish_event_update, require_admin, require_csrf
from ..models import Match, Organizer, Payment, Registration
from ..schemas import AdminMatchDetail, EventSummary, EventUpdate, MatchCreate, MatchResponse, RegistrationResponse, RejectRequest
from ..services import (
    PAYMENT_SUBMITTED, api_error, create_match, event_summary, match_to_response, promote_waitlisted,
    registration_to_response, update_match, verify_payment,
)

router = APIRouter()


@router.get("/api/admin/events", response_model=list[MatchResponse])
@router.get("/api/admin/matches", response_model=list[MatchResponse])
def admin_events(_: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
    return [match_to_response(session, item) for item in session.scalars(select(Match).order_by(Match.date.desc(), Match.start_time.desc())).all()]


@router.post("/api/admin/events", response_model=MatchResponse, status_code=201)
@router.post("/api/admin/matches", response_model=MatchResponse, status_code=201)
def admin_create_event(payload: MatchCreate, request: Request, _: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    match = create_match(session, payload); publish_event_update(request, session, match.id, "EVENT_CREATED"); return match_to_response(session, match)


@router.get("/api/admin/events/{match_id}", response_model=AdminMatchDetail)
@router.get("/api/admin/matches/{match_id}", response_model=AdminMatchDetail)
def admin_event_detail(match_id: int, _: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
    match = session.scalar(select(Match).options(joinedload(Match.registrations).joinedload(Registration.payment)).where(Match.id == match_id))
    if not match: raise api_error(404, "EVENT_NOT_FOUND", "Event not found")
    data = match_to_response(session, match); data["registrations"] = [registration_to_response(item, include_proof=True) for item in sorted(match.registrations, key=lambda item: item.created_at, reverse=True)]; return data


@router.patch("/api/admin/events/{match_id}", response_model=MatchResponse)
def admin_update_event(match_id: int, payload: EventUpdate, request: Request, _: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    match = session.get(Match, match_id)
    if not match: raise api_error(404, "EVENT_NOT_FOUND", "Event not found")
    match = update_match(session, match, payload); publish_event_update(request, session, match.id, "EVENT_UPDATED"); return match_to_response(session, match)


@router.get("/api/admin/events/{match_id}/summary", response_model=EventSummary)
def admin_summary(match_id: int, _: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
    match = session.get(Match, match_id)
    if not match: raise api_error(404, "EVENT_NOT_FOUND", "Event not found")
    return event_summary(session, match)


@router.get("/api/admin/events/{match_id}/registrations", response_model=list[RegistrationResponse])
@router.get("/api/admin/matches/{match_id}/registrations", response_model=list[RegistrationResponse])
def admin_registrations(match_id: int, _: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
    if not session.get(Match, match_id): raise api_error(404, "EVENT_NOT_FOUND", "Event not found")
    records = session.scalars(select(Registration).options(joinedload(Registration.payment)).where(Registration.match_id == match_id).order_by(Registration.created_at.desc())).all(); return [registration_to_response(item, include_proof=True) for item in records]


@router.get("/api/admin/payments/pending", response_model=list[RegistrationResponse])
def pending_payments(_: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
    records = session.scalars(select(Registration).options(joinedload(Registration.payment)).join(Payment).where(Payment.status == PAYMENT_SUBMITTED).order_by(Payment.submitted_at)).all(); return [registration_to_response(item, include_proof=True) for item in records]


@router.post("/api/admin/payments/{payment_id}/confirm", response_model=RegistrationResponse)
def confirm_payment(payment_id: int, request: Request, _: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    registration = verify_payment(session, payment_id, approve=True); publish_event_update(request, session, registration.match_id, "PAYMENT_VERIFIED"); return registration_to_response(registration, include_proof=True)


@router.post("/api/admin/payments/{payment_id}/reject", response_model=RegistrationResponse)
def reject_payment(payment_id: int, payload: RejectRequest, request: Request, _: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    registration = verify_payment(session, payment_id, approve=False, reason=payload.reason.strip()); publish_event_update(request, session, registration.match_id, "PAYMENT_REJECTED"); return registration_to_response(registration, include_proof=True)


@router.post("/api/admin/registrations/{registration_id}/promote", response_model=RegistrationResponse)
def promote(registration_id: int, request: Request, _: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
    registration = promote_waitlisted(session, registration_id); publish_event_update(request, session, registration.match_id, "WAITLIST_UPDATED"); return registration_to_response(registration, include_proof=True)
