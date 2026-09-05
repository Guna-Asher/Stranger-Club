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

from .models import AuditLog, Match, Organizer, Payment, Registration, User, now_ist
from .schemas import EventUpdate, MatchCreate, RegistrationCreate

logger = logging.getLogger("stranger_club")

CONFIRMED = "CONFIRMED"
PENDING = "PENDING"
PAYMENT_SUBMITTED = "SUBMITTED"
PAYMENT_VERIFIED = "VERIFIED"
PAYMENT_REJECTED = "REJECTED"
REJECTED = "REJECTED"
WAITLISTED = "WAITLISTED"
CANCELLED = "CANCELLED"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_FORMATS = {"JPEG": (".jpg", "image/jpeg"), "PNG": (".png", "image/png"), "WEBP": (".webp", "image/webp")}
VALID_EVENT_TRANSITIONS = {
    "DRAFT": {"OPEN", "CANCELLED"}, "OPEN": {"FULL", "ONGOING", "CANCELLED"},
    "FULL": {"OPEN", "ONGOING", "CANCELLED"}, "ONGOING": {"COMPLETED", "CANCELLED"},
    "COMPLETED": set(), "CANCELLED": set(),
}
# Single authoritative registration state machine. CANCELLED is terminal — no
# reactivation, ever; a player who wants back in creates a brand-new
# registration row (see create_registration), which is what makes each of
# these historical records independently trustworthy. REJECTED is also
# terminal *for cancellation purposes* (deliberately, per product decision):
# a rejected player's only path forward is resubmitting proof on the same
# registration (see submit_payment), not cancelling it.
VALID_REGISTRATION_TRANSITIONS = {
    PENDING: {CONFIRMED, WAITLISTED, REJECTED, CANCELLED},
    WAITLISTED: {PENDING, CANCELLED},
    CONFIRMED: {CANCELLED},
    REJECTED: set(),
    CANCELLED: set(),
}
# Event statuses during which cancelling a registration is still meaningful.
# Once an event is ONGOING, COMPLETED, or itself CANCELLED, there is nothing
# for a registration-level cancellation to accomplish.
CANCELLABLE_EVENT_STATUSES = {"DRAFT", "OPEN", "FULL"}


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def audit(
    session: Session, event_type: str, entity_type: str, entity_id: int | str, metadata: dict | None = None,
    *, actor_type: str | None = None, actor_id: int | None = None, before: dict | None = None, after: dict | None = None,
) -> None:
    session.add(AuditLog(
        event_type=event_type, entity_type=entity_type, entity_id=str(entity_id), metadata_json=metadata,
        actor_type=actor_type, actor_id=actor_id, before_json=before, after_json=after,
    ))


def transition_registration(
    session: Session, registration: Registration, to_status: str, *, event_type: str,
    actor_type: str, actor_id: int | None, metadata: dict | None = None,
) -> None:
    """The single place a Registration's status is ever changed. Enforces
    VALID_REGISTRATION_TRANSITIONS and records an attributed, before/after
    audit entry for every transition — callers never assign `.status` directly."""
    from_status = registration.status
    if to_status not in VALID_REGISTRATION_TRANSITIONS.get(from_status, set()):
        raise api_error(409, "INVALID_STATE_TRANSITION", f"Cannot move a registration from {from_status} to {to_status}")
    registration.status = to_status
    audit(
        session, event_type, "registration", registration.id, metadata,
        actor_type=actor_type, actor_id=actor_id, before={"status": from_status}, after={"status": to_status},
    )
    # This session has autoflush disabled. Callers commonly re-derive counts
    # from the database immediately after a transition (e.g. _refresh_full_status
    # checking whether the event is now full) — without an explicit flush here,
    # that re-derivation would silently read the pre-transition row.
    session.flush()


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


def assert_event_owner(match: Match, organizer: Organizer) -> None:
    """A mismatch reports 404, not 403 — mirrors assert_registration_owner:
    an ownership check must not confirm to a non-owning organizer that an
    event exists at all."""
    if organizer.role != "PLATFORM_ADMIN" and match.owner_organizer_id != organizer.id:
        raise api_error(404, "EVENT_NOT_FOUND", "Event not found")


def create_match(session: Session, payload: MatchCreate, organizer: Organizer) -> Match:
    if payload.registration_deadline > datetime.combine(payload.date, payload.start_time):
        raise api_error(422, "VALIDATION_ERROR", "Registration deadline must be before event start")
    match = Match(public_id=uuid.uuid4().hex[:16], owner_organizer_id=organizer.id, **payload.model_dump())
    session.add(match)
    session.flush()
    audit(session, "EVENT_CREATED", "event", match.id, {"public_id": match.public_id}, actor_type="ORGANIZER", actor_id=organizer.id)
    session.commit(); session.refresh(match)
    logger.info("event_created event_id=%s", match.id)
    return match


def update_match(session: Session, match: Match, payload: EventUpdate, organizer: Organizer) -> Match:
    changes = payload.model_dump(exclude_unset=True)
    requested_status = changes.pop("status", None)
    before = {"status": match.status}
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
    audit(
        session, "EVENT_UPDATED", "event", match.id, {"fields": sorted(changes)},
        actor_type="ORGANIZER", actor_id=organizer.id, before=before, after={"status": match.status},
    )
    session.commit(); session.refresh(match)
    return match


def _refresh_full_status(session: Session, match: Match) -> None:
    if match.status in {"OPEN", "FULL"}:
        match.status = "FULL" if match_counts(session, match.id)["confirmed"] >= match.capacity else "OPEN"


def assert_registration_owner(registration: Registration, user_id: int) -> None:
    """A mismatch reports 404, not 403: an ownership check must not confirm to
    a caller that a registration exists at all when it isn't theirs."""
    if registration.user_id != user_id:
        raise api_error(404, "RESOURCE_NOT_FOUND", "Registration not found")


def create_registration(session: Session, match: Match, payload: RegistrationCreate, user: User) -> Registration:
    if match.registration_deadline < now_ist() or match.status not in {"OPEN", "FULL"}:
        raise api_error(409, "REGISTRATION_CLOSED", "Registration for this event has closed")
    # SQLite BEGIN IMMEDIATE serialises the capacity decision. PostgreSQL can
    # replace this with SELECT ... FOR UPDATE without changing this service API.
    # The database-level partial unique index (uq_registration_active_per_user)
    # is the final backstop even if this serialisation had a bug: a second
    # concurrent active registration for the same (event, user) can never be
    # written, full stop.
    session.rollback(); session.execute(text("BEGIN IMMEDIATE"))
    match = session.scalar(select(Match).where(Match.id == match.id))
    # Phone comes from the OTP-verified session, never from the request payload.
    phone = user.phone
    confirmed = match_counts(session, match.id)["confirmed"]
    registration = Registration(
        public_id=uuid.uuid4().hex, match_id=match.id, user_id=user.id, name=payload.name,
        phone=phone, email=str(payload.email) if payload.email else None, preferred_position=payload.preferred_position,
        status=WAITLISTED if confirmed >= match.capacity else PENDING,
    )
    session.add(registration)
    try:
        session.flush()
        audit(
            session, "PLAYER_WAITLISTED" if registration.status == WAITLISTED else "REGISTRATION_CREATED",
            "registration", registration.id, {"event_id": match.id}, actor_type="PLAYER", actor_id=user.id,
        )
        session.commit()
    except IntegrityError:
        session.rollback(); raise api_error(409, "REGISTRATION_EXISTS", "You already have an active registration for this event")
    session.refresh(registration)
    logger.info("registration_created registration_id=%s event_id=%s status=%s", registration.id, match.id, registration.status)
    return registration


def cancel_registration(session: Session, registration_id: int, *, actor_type: str, actor_id: int | None) -> Registration:
    """Cancels a registration. Idempotent: cancelling an already-CANCELLED
    registration is a no-op success, not an error (safe under double-click,
    retry, or timeout+retry). CANCELLED is terminal — this never reactivates
    or reuses a row; a player who wants back in registers again, which
    create_registration turns into a brand-new historical record."""
    session.rollback(); session.execute(text("BEGIN IMMEDIATE"))
    registration = session.scalar(select(Registration).options(joinedload(Registration.match)).where(Registration.id == registration_id))
    if not registration:
        raise api_error(404, "RESOURCE_NOT_FOUND", "Registration not found")
    if registration.status == CANCELLED:
        session.commit()
        return registration
    if registration.match.status not in CANCELLABLE_EVENT_STATUSES:
        session.commit()
        raise api_error(409, "EVENT_ALREADY_STARTED", "This event has already started or concluded; registrations can no longer be cancelled")
    transition_registration(
        session, registration, CANCELLED, event_type="REGISTRATION_CANCELLED",
        actor_type=actor_type, actor_id=actor_id, metadata={"event_id": registration.match_id},
    )
    registration.cancelled_at = now_ist()
    _refresh_full_status(session, registration.match)
    session.commit(); session.refresh(registration)
    logger.info("registration_cancelled registration_id=%s actor_type=%s", registration.id, actor_type)
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


async def submit_payment(session: Session, registration_key: str, upload: UploadFile, uploads_dir: Path, user_id: int) -> Registration:
    # The upload itself (network I/O + Pillow decode) happens outside any lock,
    # same as before — only the guard-check-then-write of the Payment row is
    # serialised, which is the part that was previously racy: two rapid
    # submissions could both pass the "not already in review" guard before
    # either committed, both write a file to disk, and race on the final DB
    # write, leaving one file orphaned on disk with no referencing row.
    registration = session.scalar(select(Registration).options(joinedload(Registration.match), joinedload(Registration.payment)).where(Registration.public_id == registration_key))
    if not registration: raise api_error(404, "RESOURCE_NOT_FOUND", "Registration not found")
    assert_registration_owner(registration, user_id)
    if registration.status == WAITLISTED: raise api_error(409, "EVENT_FULL", "This registration is on the waitlist")
    if registration.status == CONFIRMED: raise api_error(409, "PAYMENT_ALREADY_VERIFIED", "This registration is already confirmed")
    if registration.status == CANCELLED: raise api_error(409, "INVALID_STATE_TRANSITION", "This registration has been cancelled")

    filename, token = await store_payment_proof(upload, uploads_dir)

    session.rollback(); session.execute(text("BEGIN IMMEDIATE"))
    registration = session.scalar(select(Registration).options(joinedload(Registration.match), joinedload(Registration.payment)).where(Registration.public_id == registration_key))
    if not registration: raise api_error(404, "RESOURCE_NOT_FOUND", "Registration not found")
    # Re-check every guard inside the lock: state may have changed since the
    # pre-upload check above (e.g. a concurrent submission just claimed "in review").
    if registration.status in (WAITLISTED, CONFIRMED, CANCELLED) or (registration.payment and registration.payment.status == PAYMENT_SUBMITTED):
        (uploads_dir / filename).unlink(missing_ok=True)  # avoid leaving an orphaned file behind
        if registration.status == WAITLISTED: raise api_error(409, "EVENT_FULL", "This registration is on the waitlist")
        if registration.status == CONFIRMED: raise api_error(409, "PAYMENT_ALREADY_VERIFIED", "This registration is already confirmed")
        if registration.status == CANCELLED: raise api_error(409, "INVALID_STATE_TRANSITION", "This registration has been cancelled")
        raise api_error(409, "PAYMENT_IN_REVIEW", "A payment proof is already being reviewed")
    if registration.payment:
        old = uploads_dir / registration.payment.screenshot_path
        if old.is_file(): old.unlink()
        payment = registration.payment; payment.amount = registration.match.fee; payment.screenshot_path = filename; payment.screenshot_token = token
        payment.status = PAYMENT_SUBMITTED; payment.submitted_at = now_ist(); payment.verified_at = None; payment.rejection_reason = None
    else:
        payment = Payment(amount=registration.match.fee, screenshot_path=filename, screenshot_token=token, status=PAYMENT_SUBMITTED); registration.payment = payment
    session.flush()
    audit(session, "PAYMENT_SUBMITTED", "payment", payment.id, {"registration_id": registration.id}, actor_type="PLAYER", actor_id=user_id)
    session.commit(); session.refresh(registration)
    logger.info("payment_submitted registration_id=%s payment_id=%s", registration.id, registration.payment.id)
    return registration


def verify_payment(session: Session, payment_id: int, approve: bool, *, actor_id: int, reason: str | None = None) -> Registration:
    session.rollback(); session.execute(text("BEGIN IMMEDIATE"))
    payment = session.scalar(select(Payment).options(joinedload(Payment.registration).joinedload(Registration.match)).where(Payment.id == payment_id))
    if not payment: raise api_error(404, "RESOURCE_NOT_FOUND", "Payment not found")
    if payment.status != PAYMENT_SUBMITTED: raise api_error(409, "PAYMENT_ALREADY_REVIEWED", "Payment has already been reviewed")
    registration = payment.registration
    if approve:
        if match_counts(session, registration.match_id)["confirmed"] >= registration.match.capacity:
            transition_registration(session, registration, WAITLISTED, event_type="PLAYER_WAITLISTED", actor_type="ORGANIZER", actor_id=actor_id, metadata={"reason": "capacity"})
            payment.status = PAYMENT_REJECTED; payment.rejection_reason = "Event filled before payment could be confirmed"
        else:
            transition_registration(session, registration, CONFIRMED, event_type="PAYMENT_VERIFIED", actor_type="ORGANIZER", actor_id=actor_id, metadata={"payment_id": payment.id})
            payment.status = PAYMENT_VERIFIED; payment.verified_at = now_ist()
    else:
        transition_registration(session, registration, REJECTED, event_type="PAYMENT_REJECTED", actor_type="ORGANIZER", actor_id=actor_id, metadata={"payment_id": payment.id})
        payment.status = PAYMENT_REJECTED; payment.rejection_reason = reason; payment.verified_at = now_ist()
    _refresh_full_status(session, registration.match)
    session.commit(); session.refresh(registration)
    logger.info("payment_reviewed payment_id=%s approved=%s registration_id=%s", payment.id, approve, registration.id)
    return registration


def promote_waitlisted(session: Session, registration_id: int, *, actor_id: int) -> Registration:
    session.rollback(); session.execute(text("BEGIN IMMEDIATE"))
    registration = session.scalar(select(Registration).options(joinedload(Registration.match), joinedload(Registration.payment)).where(Registration.id == registration_id))
    if not registration: raise api_error(404, "RESOURCE_NOT_FOUND", "Registration not found")
    if registration.status != WAITLISTED: raise api_error(409, "INVALID_STATE_TRANSITION", "Only waitlisted players can be promoted")
    # Preserve FIFO: only the oldest eligible entry can be promoted.
    first = session.scalar(select(Registration).where(Registration.match_id == registration.match_id, Registration.status == WAITLISTED).order_by(Registration.created_at, Registration.id))
    if first.id != registration.id: raise api_error(409, "WAITLIST_ORDER", "Promote the next player on the waitlist first")
    if match_counts(session, registration.match_id)["confirmed"] >= registration.match.capacity: raise api_error(409, "EVENT_FULL", "No spots are available")
    transition_registration(session, registration, PENDING, event_type="PLAYER_PROMOTED", actor_type="ORGANIZER", actor_id=actor_id, metadata={"event_id": registration.match_id})
    _refresh_full_status(session, registration.match)
    session.commit(); session.refresh(registration)
    return registration


def backfill_missing_event_ownership(session: Session) -> None:
    """Assigns owner_organizer_id to any pre-existing event that predates
    ownership tracking. Must run at application startup, after the bootstrap
    organizer is guaranteed to exist — NOT from the schema migration itself,
    which runs earlier and may see zero organizers on a genuinely fresh or
    legacy database. Only backfills when unambiguous (exactly one organizer);
    with more than one, there's no historical record of who owned what, so
    this fails loudly rather than guessing and silently misassigning
    ownership. Idempotent and cheap: safe to call on every startup.
    """
    orphaned = session.scalar(select(func.count(Match.id)).where(Match.owner_organizer_id.is_(None)))
    if not orphaned:
        return
    organizers = session.scalars(select(Organizer.id)).all()
    if len(organizers) == 0:
        return  # nothing to assign to yet; a later startup (after an organizer exists) will retry
    if len(organizers) > 1:
        raise RuntimeError(
            f"Cannot safely backfill owner_organizer_id for {orphaned} event(s): {len(organizers)} organizer "
            "accounts already exist and there is no historical record of which organizer owns which "
            "pre-existing event. Resolve this manually (assign matches.owner_organizer_id for each orphaned "
            "event) before starting the application."
        )
    session.execute(text("UPDATE matches SET owner_organizer_id = :oid WHERE owner_organizer_id IS NULL"), {"oid": organizers[0]})
    session.commit()
    logger.info("event_ownership_backfilled organizer_id=%s events=%s", organizers[0], orphaned)


def seed_database(session: Session, owner_organizer_id: int | None = None) -> None:
    if session.scalar(select(func.count(Match.id))) > 0: return
    match = Match(public_id="sunday-cricket-2026", name="Sunday Cricket", date=date(2026, 8, 23), start_time=time(7), end_time=time(10), venue="PlayArena, Bellandur", capacity=22, fee=300, registration_deadline=datetime(2026, 8, 22, 20), upi_id="strangerclub@upi", status="OPEN", owner_organizer_id=owner_organizer_id)
    session.add(match); session.commit(); logger.info("seed_event_created public_id=%s", match.public_id)
