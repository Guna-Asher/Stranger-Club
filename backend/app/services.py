from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time
from pathlib import Path

from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from .models import AuditLog, Match, Payment, Player, Registration, now_ist
from .schemas import EventUpdate, MatchCreate, RegistrationCreate

logger = logging.getLogger("stranger_club")

CONFIRMED = "CONFIRMED"
PENDING = "PENDING"
PAYMENT_SUBMITTED = "SUBMITTED"
PAYMENT_VERIFIED = "VERIFIED"
PAYMENT_REJECTED = "REJECTED"
REJECTED = "REJECTED"
WAITLISTED = "WAITLISTED"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_FORMATS = {"JPEG": (".jpg", "image/jpeg"), "PNG": (".png", "image/png"), "WEBP": (".webp", "image/webp")}
VALID_EVENT_TRANSITIONS = {
    "DRAFT": {"OPEN", "CANCELLED"}, "OPEN": {"FULL", "ONGOING", "CANCELLED"},
    "FULL": {"OPEN", "ONGOING", "CANCELLED"}, "ONGOING": {"COMPLETED", "CANCELLED"},
    "COMPLETED": set(), "CANCELLED": set(),
}


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def audit(session: Session, event_type: str, entity_type: str, entity_id: int | str, metadata: dict | None = None) -> None:
    session.add(AuditLog(event_type=event_type, entity_type=entity_type, entity_id=str(entity_id), metadata_json=metadata))


def match_counts(session: Session, match_id: int) -> dict[str, int]:
    registration_rows = session.execute(
        select(Registration.status, func.count(Registration.id)).where(Registration.match_id == match_id).group_by(Registration.status)
    ).all()
    payment_rows = session.execute(
        select(Payment.status, func.count(Payment.id)).join(Registration).where(Registration.match_id == match_id).group_by(Payment.status)
    ).all()
    registrations = dict(registration_rows)
    payments = dict(payment_rows)
    return {
        "confirmed": registrations.get(CONFIRMED, 0), "pending": registrations.get(PENDING, 0),
        "waitlisted": registrations.get(WAITLISTED, 0), "payment_submitted": payments.get(PAYMENT_SUBMITTED, 0),
        "collected_amount": session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).join(Registration).where(Registration.match_id == match_id, Payment.status == PAYMENT_VERIFIED)) or 0,
    }


def event_summary(session: Session, match: Match) -> dict:
    counts = match_counts(session, match.id)
    return {"event_id": match.public_id, "capacity": match.capacity, **counts, "available": max(0, match.capacity - counts["confirmed"])}


def match_to_response(session: Session, match: Match) -> dict:
    summary = event_summary(session, match)
    return {
        "id": match.id, "public_id": match.public_id, "name": match.name, "date": match.date,
        "start_time": match.start_time, "end_time": match.end_time, "venue": match.venue,
        "capacity": match.capacity, "fee": match.fee, "registration_deadline": match.registration_deadline,
        "upi_id": match.upi_id, "status": match.status, "confirmed_count": summary["confirmed"],
        "pending_count": summary["pending"], "payment_submitted_count": summary["payment_submitted"],
        "waitlist_count": summary["waitlisted"], "available_slots": summary["available"],
        "collected_amount": summary["collected_amount"],
    }


def payment_to_response(payment: Payment, include_proof: bool = False) -> dict:
    return {"id": payment.id, "amount": payment.amount, "status": payment.status, "submitted_at": payment.submitted_at,
            "verified_at": payment.verified_at, "rejection_reason": payment.rejection_reason,
            "screenshot_url": f"/api/payment-proofs/{payment.screenshot_token}" if include_proof else None}


def registration_to_response(registration: Registration, include_proof: bool = False) -> dict:
    return {"id": registration.id, "public_id": registration.public_id, "match_id": registration.match_id,
            "name": registration.name, "phone": registration.phone, "email": registration.email, "status": registration.status,
            "preferred_position": registration.preferred_position or "NO_PREFERENCE", "assigned_position": registration.assigned_position,
            "created_at": registration.created_at, "updated_at": registration.updated_at,
            "payment": payment_to_response(registration.payment, include_proof) if registration.payment else None}


def get_public_match(session: Session, public_id: str) -> Match:
    match = session.scalar(select(Match).where(Match.public_id == public_id, Match.status.in_(("OPEN", "FULL", "ONGOING"))))
    if not match:
        raise api_error(404, "EVENT_NOT_FOUND", "Event not found")
    return match


# Compatibility alias used by old route handlers.
get_active_match = get_public_match


def create_match(session: Session, payload: MatchCreate) -> Match:
    if payload.registration_deadline > datetime.combine(payload.date, payload.start_time):
        raise api_error(422, "VALIDATION_ERROR", "Registration deadline must be before event start")
    match = Match(public_id=uuid.uuid4().hex[:16], **payload.model_dump())
    session.add(match)
    session.flush()
    audit(session, "EVENT_CREATED", "event", match.id, {"public_id": match.public_id})
    session.commit(); session.refresh(match)
    logger.info("event_created event_id=%s", match.id)
    return match


def update_match(session: Session, match: Match, payload: EventUpdate) -> Match:
    changes = payload.model_dump(exclude_unset=True)
    requested_status = changes.pop("status", None)
    if requested_status and requested_status != match.status:
        if requested_status not in VALID_EVENT_TRANSITIONS.get(match.status, set()):
            raise api_error(409, "INVALID_STATE_TRANSITION", f"Cannot move an event from {match.status} to {requested_status}")
        match.status = requested_status
    for field, value in changes.items():
        if isinstance(value, str): value = value.strip()
        setattr(match, field, value)
    if match.end_time <= match.start_time:
        raise api_error(422, "VALIDATION_ERROR", "End time must be after start time")
    if match.registration_deadline > datetime.combine(match.date, match.start_time):
        raise api_error(422, "VALIDATION_ERROR", "Registration deadline must be before event start")
    if match.capacity < match_counts(session, match.id)["confirmed"]:
        raise api_error(409, "INVALID_STATE_TRANSITION", "Capacity cannot be below confirmed players")
    audit(session, "EVENT_UPDATED", "event", match.id, {"fields": sorted(changes)})
    session.commit(); session.refresh(match)
    return match


def _refresh_full_status(session: Session, match: Match) -> None:
    if match.status in {"OPEN", "FULL"}:
        match.status = "FULL" if match_counts(session, match.id)["confirmed"] >= match.capacity else "OPEN"


def create_registration(session: Session, match: Match, payload: RegistrationCreate) -> Registration:
    if match.registration_deadline < now_ist() or match.status not in {"OPEN", "FULL"}:
        raise api_error(409, "REGISTRATION_CLOSED", "Registration for this event has closed")
    # SQLite BEGIN IMMEDIATE serialises the capacity decision. PostgreSQL can
    # replace this with SELECT ... FOR UPDATE without changing this service API.
    session.rollback(); session.execute(text("BEGIN IMMEDIATE"))
    match = session.scalar(select(Match).where(Match.id == match.id))
    player = session.scalar(select(Player).where(Player.phone == payload.phone))
    if not player:
        player = Player(phone=payload.phone, name=payload.name, email=str(payload.email) if payload.email else None, preferred_position=payload.preferred_position)
        session.add(player); session.flush()
    else:
        player.name = payload.name; player.email = str(payload.email) if payload.email else player.email
        player.preferred_position = payload.preferred_position
    confirmed = match_counts(session, match.id)["confirmed"]
    registration = Registration(public_id=uuid.uuid4().hex, match_id=match.id, player_id=player.id, name=payload.name,
        phone=payload.phone, email=str(payload.email) if payload.email else None, preferred_position=payload.preferred_position,
        status=WAITLISTED if confirmed >= match.capacity else PENDING)
    session.add(registration)
    try:
        session.flush()
        audit(session, "PLAYER_WAITLISTED" if registration.status == WAITLISTED else "REGISTRATION_CREATED", "registration", registration.id, {"event_id": match.id})
        session.commit()
    except IntegrityError:
        session.rollback(); raise api_error(409, "REGISTRATION_EXISTS", "This phone number is already registered for this event")
    session.refresh(registration)
    logger.info("registration_created registration_id=%s event_id=%s status=%s", registration.id, match.id, registration.status)
    return registration


async def store_payment_proof(upload: UploadFile, uploads_dir: Path) -> tuple[str, str]:
    if Path(upload.filename or "").suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"} or upload.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise api_error(400, "INVALID_UPLOAD", "Upload a JPEG, PNG, or WEBP image")
    content = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES: raise api_error(413, "UPLOAD_TOO_LARGE", "Payment screenshot must be 5 MB or smaller")
    try:
        from io import BytesIO
        image = Image.open(BytesIO(content)); image.verify()
        image = Image.open(BytesIO(content)); image_format = image.format
    except (UnidentifiedImageError, OSError):
        raise api_error(400, "INVALID_UPLOAD", "Invalid image")
    if image_format not in ALLOWED_FORMATS: raise api_error(400, "INVALID_UPLOAD", "Upload a JPEG, PNG, or WEBP image")
    filename = f"{uuid.uuid4().hex}{ALLOWED_FORMATS[image_format][0]}"
    uploads_dir.mkdir(parents=True, exist_ok=True); (uploads_dir / filename).write_bytes(content)
    return filename, uuid.uuid4().hex


async def submit_payment(session: Session, registration_key: str, upload: UploadFile, uploads_dir: Path) -> Registration:
    registration = session.scalar(select(Registration).options(joinedload(Registration.match), joinedload(Registration.payment)).where(Registration.public_id == registration_key))
    if not registration: raise api_error(404, "RESOURCE_NOT_FOUND", "Registration not found")
    if registration.status == WAITLISTED: raise api_error(409, "EVENT_FULL", "This registration is on the waitlist")
    if registration.status == CONFIRMED: raise api_error(409, "PAYMENT_ALREADY_VERIFIED", "This registration is already confirmed")
    if registration.payment and registration.payment.status == PAYMENT_SUBMITTED:
        raise api_error(409, "PAYMENT_IN_REVIEW", "A payment proof is already being reviewed")
    filename, token = await store_payment_proof(upload, uploads_dir)
    if registration.payment:
        old = uploads_dir / registration.payment.screenshot_path
        if old.is_file(): old.unlink()
        payment = registration.payment; payment.amount = registration.match.fee; payment.screenshot_path = filename; payment.screenshot_token = token
        payment.status = PAYMENT_SUBMITTED; payment.submitted_at = now_ist(); payment.verified_at = None; payment.rejection_reason = None
    else:
        payment = Payment(amount=registration.match.fee, screenshot_path=filename, screenshot_token=token, status=PAYMENT_SUBMITTED); registration.payment = payment
    session.flush()
    audit(session, "PAYMENT_SUBMITTED", "payment", payment.id, {"registration_id": registration.id})
    session.commit(); session.refresh(registration)
    logger.info("payment_submitted registration_id=%s payment_id=%s", registration.id, registration.payment.id)
    return registration


def verify_payment(session: Session, payment_id: int, approve: bool, reason: str | None = None) -> Registration:
    session.rollback(); session.execute(text("BEGIN IMMEDIATE"))
    payment = session.scalar(select(Payment).options(joinedload(Payment.registration).joinedload(Registration.match)).where(Payment.id == payment_id))
    if not payment: raise api_error(404, "RESOURCE_NOT_FOUND", "Payment not found")
    if payment.status != PAYMENT_SUBMITTED: raise api_error(409, "PAYMENT_ALREADY_REVIEWED", "Payment has already been reviewed")
    registration = payment.registration
    if approve:
        if match_counts(session, registration.match_id)["confirmed"] >= registration.match.capacity:
            registration.status = WAITLISTED; payment.status = PAYMENT_REJECTED; payment.rejection_reason = "Event filled before payment could be confirmed"
            audit(session, "PLAYER_WAITLISTED", "registration", registration.id, {"reason": "capacity"})
        else:
            registration.status = CONFIRMED; payment.status = PAYMENT_VERIFIED; payment.verified_at = now_ist()
            audit(session, "PAYMENT_VERIFIED", "payment", payment.id, {"registration_id": registration.id})
    else:
        registration.status = REJECTED; payment.status = PAYMENT_REJECTED; payment.rejection_reason = reason; payment.verified_at = now_ist()
        audit(session, "PAYMENT_REJECTED", "payment", payment.id, {"registration_id": registration.id})
    _refresh_full_status(session, registration.match)
    session.commit(); session.refresh(registration)
    logger.info("payment_reviewed payment_id=%s approved=%s registration_id=%s", payment.id, approve, registration.id)
    return registration


def promote_waitlisted(session: Session, registration_id: int) -> Registration:
    session.rollback(); session.execute(text("BEGIN IMMEDIATE"))
    registration = session.scalar(select(Registration).options(joinedload(Registration.match), joinedload(Registration.payment)).where(Registration.id == registration_id))
    if not registration: raise api_error(404, "RESOURCE_NOT_FOUND", "Registration not found")
    if registration.status != WAITLISTED: raise api_error(409, "INVALID_STATE_TRANSITION", "Only waitlisted players can be promoted")
    # Preserve FIFO: only the oldest eligible entry can be promoted.
    first = session.scalar(select(Registration).where(Registration.match_id == registration.match_id, Registration.status == WAITLISTED).order_by(Registration.created_at, Registration.id))
    if first.id != registration.id: raise api_error(409, "WAITLIST_ORDER", "Promote the next player on the waitlist first")
    if match_counts(session, registration.match_id)["confirmed"] >= registration.match.capacity: raise api_error(409, "EVENT_FULL", "No spots are available")
    registration.status = PENDING; _refresh_full_status(session, registration.match)
    audit(session, "PLAYER_PROMOTED", "registration", registration.id, {"event_id": registration.match_id})
    session.commit(); session.refresh(registration)
    return registration


def seed_database(session: Session) -> None:
    if session.scalar(select(func.count(Match.id))) > 0: return
    match = Match(public_id="sunday-cricket-2026", name="Sunday Cricket", date=date(2026, 8, 23), start_time=time(7), end_time=time(10), venue="PlayArena, Bellandur", capacity=22, fee=300, registration_deadline=datetime(2026, 8, 22, 20), upi_id="strangerclub@upi", status="OPEN")
    session.add(match); session.commit(); logger.info("seed_event_created public_id=%s", match.public_id)
