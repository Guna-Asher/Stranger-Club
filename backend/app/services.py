from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import date, datetime, time
from io import BytesIO
from pathlib import Path

from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from .models import (
    AuditLog, EventPaymentConfiguration, Fixture, FIXTURE_ROSTER_LOCKING_STATUSES, Match, Organizer, Payment,
    PaymentProof, Registration, Team, TeamMember, User, now_ist,
)
from .schemas import (
    EventUpdate, FixtureCreate, FixtureUpdate, MatchCreate, PaymentConfigurationUpdate, RegistrationCreate,
    TeamCreate, TeamUpdate,
)
from .storage import Storage

logger = logging.getLogger("stranger_club")

CONFIRMED = "CONFIRMED"
PENDING = "PENDING"
PAYMENT_AWAITING_PROOF = "AWAITING_PROOF"
PAYMENT_SUBMITTED = "SUBMITTED"
PAYMENT_VERIFIED = "VERIFIED"
PAYMENT_REJECTED = "REJECTED"
PROOF_PENDING = "PENDING"
PROOF_ACCEPTED = "ACCEPTED"
PROOF_REJECTED = "REJECTED"
PROOF_SUPERSEDED = "SUPERSEDED"
REJECTED = "REJECTED"
WAITLISTED = "WAITLISTED"
CANCELLED = "CANCELLED"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
QR_MAX_UPLOAD_BYTES = 2 * 1024 * 1024
ALLOWED_FORMATS = {"JPEG": (".jpg", "image/jpeg"), "PNG": (".png", "image/png"), "WEBP": (".webp", "image/webp")}
UTR_PATTERN = re.compile(r"^[A-Za-z0-9]{6,30}$")
# Payment never transitions on its own — VERIFIED and REJECTED only ever
# happen via an explicit organizer review (see review_payment). Opening a UPI
# app, returning from it, or uploading a proof never appears here: none of
# those are payment confirmation. REJECTED -> SUBMITTED is a resubmission.
# VERIFIED has no outgoing edges: once verified, always verified, even if the
# registration is later cancelled (see cancel_registration).
VALID_PAYMENT_TRANSITIONS = {
    PAYMENT_AWAITING_PROOF: {PAYMENT_SUBMITTED},
    PAYMENT_SUBMITTED: {PAYMENT_VERIFIED, PAYMENT_REJECTED},
    PAYMENT_REJECTED: {PAYMENT_SUBMITTED},
    PAYMENT_VERIFIED: set(),
}
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
# Forward-only, same reasoning as VALID_EVENT_TRANSITIONS: no COMPLETED ->
# SCHEDULED or other reverse transition. "Fixture" is an internal name only
# (see models.Fixture) — every user-facing string says "Match"/"Matches".
VALID_FIXTURE_TRANSITIONS = {
    "SCHEDULED": {"IN_PROGRESS", "CANCELLED"},
    "IN_PROGRESS": {"COMPLETED", "CANCELLED"},
    "COMPLETED": set(),
    "CANCELLED": set(),
}


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def begin_serialized_write(session: Session) -> None:
    """Starts the transaction a subsequent row lock will serialize against.
    SQLite: BEGIN IMMEDIATE takes a whole-database write lock up front (the
    original mechanism, unchanged). PostgreSQL: nothing to do here — the
    actual lock comes from lock_row() below, scoped to one specific row
    rather than the whole database."""
    session.rollback()
    if session.bind.dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))


def lock_row(session: Session, model, id_: int) -> None:
    """PostgreSQL row-level lock for the duration of the current transaction.
    Deliberately a bare `SELECT id ... FOR UPDATE` with no joins: PostgreSQL
    rejects FOR UPDATE combined with the outer joins joinedload() produces
    for collection relationships ("FOR UPDATE cannot be applied to the
    nullable side of an outer join"). Lock the single row first, then load
    the full eager-loaded object graph in a separate, plain read within the
    same transaction — it sees the now-locked, up-to-date row.
    On SQLite this is a no-op: begin_serialized_write() already took a
    whole-database write lock before this is ever called."""
    if session.bind.dialect.name == "postgresql":
        session.execute(select(model.id).where(model.id == id_).with_for_update())


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


def transition_payment(
    session: Session, payment: Payment, to_status: str, *, event_type: str,
    actor_type: str, actor_id: int | None, metadata: dict | None = None,
) -> None:
    """The single place a Payment's status is ever changed. Enforces
    VALID_PAYMENT_TRANSITIONS and records an attributed, before/after audit
    entry — callers never assign `.status` directly. There is deliberately no
    path into VERIFIED except an explicit organizer review (see
    review_payment): nothing about submitting a proof, opening a UPI app, or
    the player's own client state can ever move a Payment into VERIFIED."""
    from_status = payment.status
    if to_status not in VALID_PAYMENT_TRANSITIONS.get(from_status, set()):
        raise api_error(409, "INVALID_STATE_TRANSITION", f"Cannot move a payment from {from_status} to {to_status}")
    payment.status = to_status
    audit(
        session, event_type, "payment", payment.id, metadata,
        actor_type=actor_type, actor_id=actor_id, before={"status": from_status}, after={"status": to_status},
    )
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
        "collected_amount": session.scalar(select(func.coalesce(func.sum(Payment.amount_due), 0)).join(Registration).where(Registration.match_id == match_id, Payment.status == PAYMENT_VERIFIED)) or 0,
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
        "payment_configuration": payment_configuration_to_response(match.payment_configuration),
        "status": match.status, "confirmed_count": summary["confirmed"],
        "pending_count": summary["pending"], "payment_submitted_count": summary["payment_submitted"],
        "waitlist_count": summary["waitlisted"], "available_slots": summary["available"],
        "collected_amount": summary["collected_amount"],
    }


def payment_configuration_to_response(config: EventPaymentConfiguration) -> dict:
    return {
        "payee_upi_id": config.payee_upi_id, "payee_name": config.payee_name, "qr_source": config.qr_source,
        "has_custom_qr": config.qr_source == "UPLOADED" and bool(config.qr_storage_key),
        "updated_at": config.updated_at,
    }


def build_upi_uri(payee_upi_id: str, payee_name: str, amount: int) -> str:
    """Server-authoritative UPI deep-link. Always built from a Payment's own
    frozen snapshot fields (see Payment model) — never from live event
    configuration and never overridable by anything the client sends."""
    from urllib.parse import quote
    return f"upi://pay?pa={quote(payee_upi_id)}&pn={quote(payee_name)}&am={amount}&cu=INR"


def _current_proof(payment: Payment) -> PaymentProof | None:
    """The proof a viewer should currently see: the one PENDING proof if
    there is one, otherwise the most recently uploaded proof regardless of
    its terminal status (so a VERIFIED/REJECTED payment still shows what was
    reviewed)."""
    if not payment.proofs:
        return None
    pending = next((p for p in payment.proofs if p.status == PROOF_PENDING), None)
    return pending or max(payment.proofs, key=lambda p: p.uploaded_at)


def _find_duplicate_proofs(session: Session, proof: PaymentProof) -> list[dict]:
    """Exact SHA-256 matches of this proof's screenshot on a *different*
    payment. A deterministic signal for the organizer to look at, never an
    automatic fraud determination — see PAYMENT_PROOF_SUBMITTED audit entry
    for the same computation at submission time."""
    rows = session.execute(
        select(PaymentProof, Registration)
        .join(Payment, PaymentProof.payment_id == Payment.id)
        .join(Registration, Payment.registration_id == Registration.id)
        .where(PaymentProof.screenshot_hash == proof.screenshot_hash, Payment.id != proof.payment_id)
    ).all()
    return [{"proof_id": p.id, "registration_id": r.id, "player_name": r.name} for p, r in rows]


def payment_to_response(payment: Payment | None, *, viewer: str = "player", session: Session | None = None) -> dict | None:
    """viewer='player' returns the minimal, never-implies-success view (see
    schemas.PaymentResponse); viewer='organizer' additionally includes the
    screenshot, UTR, pending-proof identity, and cross-registration duplicate
    warnings needed for review. Player responses never include any of the
    organizer-only fields, regardless of what's passed."""
    if not payment:
        return None
    current = _current_proof(payment)
    data = {
        "id": payment.id, "status": payment.status, "amount_due": payment.amount_due,
        "payee_upi_id_snapshot": payment.payee_upi_id_snapshot, "payee_name_snapshot": payment.payee_name_snapshot,
        "qr_source_snapshot": payment.qr_source_snapshot,
        "upi_uri": build_upi_uri(payment.payee_upi_id_snapshot, payment.payee_name_snapshot, payment.amount_due),
        "qr_image_url": f"/api/registrations/{payment.registration.public_id}/payment-qr" if payment.qr_source_snapshot == "UPLOADED" else None,
        "submitted_at": payment.submitted_at, "verified_at": payment.verified_at, "rejection_reason": payment.rejection_reason,
        "utr_reference": current.utr_reference if current else None,
    }
    if viewer == "organizer":
        pending = next((p for p in payment.proofs if p.status == PROOF_PENDING), None)
        data["screenshot_url"] = f"/api/payment-proofs/{current.id}" if current else None
        data["pending_proof_id"] = pending.id if pending else None
        data["proof_count"] = len(payment.proofs)
        data["duplicate_of"] = _find_duplicate_proofs(session, current) if session and current else []
    return data


def registration_to_response(registration: Registration, *, viewer: str = "player", session: Session | None = None) -> dict:
    return {"id": registration.id, "public_id": registration.public_id, "match_id": registration.match_id,
            "name": registration.name, "phone": registration.phone, "email": registration.email, "status": registration.status,
            "preferred_position": registration.preferred_position or "NO_PREFERENCE", "assigned_position": registration.assigned_position,
            "created_at": registration.created_at, "updated_at": registration.updated_at,
            "payment": payment_to_response(registration.payment, viewer=viewer, session=session)}


def get_public_match(session: Session, public_id: str) -> Match:
    match = session.scalar(
        select(Match).options(joinedload(Match.payment_configuration))
        .where(Match.public_id == public_id, Match.status.in_(("OPEN", "FULL", "ONGOING")))
    )
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
    data = payload.model_dump()
    payee_upi_id = data.pop("upi_id")
    payee_name = data.pop("payee_name")
    match = Match(public_id=uuid.uuid4().hex[:16], owner_organizer_id=organizer.id, **data)
    session.add(match)
    session.flush()
    # Every event gets a payment configuration atomically at creation — there
    # is no window where a match exists but registering against it would have
    # nothing to snapshot a Payment from.
    config = EventPaymentConfiguration(
        match_id=match.id, payee_upi_id=payee_upi_id, payee_name=payee_name,
        qr_source="GENERATED", updated_by_organizer_id=organizer.id,
    )
    session.add(config)
    session.flush()
    audit(session, "EVENT_CREATED", "event", match.id, {"public_id": match.public_id}, actor_type="ORGANIZER", actor_id=organizer.id)
    session.commit(); session.refresh(match)
    logger.info("event_created event_id=%s", match.id)
    return match


def update_payment_configuration(session: Session, match: Match, payload: PaymentConfigurationUpdate, organizer: Organizer) -> EventPaymentConfiguration:
    """Editing an event's live payment configuration. Deliberately
    unrestricted even after registrations/payments exist: every Payment
    already froze its own snapshot at creation time (see Payment model), so
    an edit here can never silently reinterpret a historical payment —
    there's no correctness reason to block it, only an audit trail need,
    which is recorded below."""
    config = match.payment_configuration
    if not config:
        raise api_error(404, "RESOURCE_NOT_FOUND", "Payment configuration not found")
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("qr_source") == "UPLOADED":
        raise api_error(422, "VALIDATION_ERROR", "Upload a QR image to switch to a custom QR")
    before = {"payee_upi_id": config.payee_upi_id, "payee_name": config.payee_name, "qr_source": config.qr_source}
    for field, value in changes.items():
        setattr(config, field, value)
    if changes.get("qr_source") == "GENERATED":
        config.qr_storage_key = None
    config.updated_by_organizer_id = organizer.id
    audit(
        session, "PAYMENT_CONFIG_UPDATED", "event_payment_configuration", config.id, {"fields": sorted(changes)},
        actor_type="ORGANIZER", actor_id=organizer.id, before=before,
        after={"payee_upi_id": config.payee_upi_id, "payee_name": config.payee_name, "qr_source": config.qr_source},
    )
    session.commit(); session.refresh(config)
    return config


async def set_payment_configuration_qr(session: Session, match: Match, upload: UploadFile, storage: Storage, organizer: Organizer) -> EventPaymentConfiguration:
    config = match.payment_configuration
    if not config:
        raise api_error(404, "RESOURCE_NOT_FOUND", "Payment configuration not found")
    content, _digest, _content_type, image_format = await read_and_validate_image(upload, max_bytes=QR_MAX_UPLOAD_BYTES)
    key = persist_image(storage, content, image_format)
    before = {"qr_source": config.qr_source}
    config.qr_source = "UPLOADED"
    config.qr_storage_key = key
    config.updated_by_organizer_id = organizer.id
    audit(
        session, "PAYMENT_CONFIG_UPDATED", "event_payment_configuration", config.id, {"field": "qr"},
        actor_type="ORGANIZER", actor_id=organizer.id, before=before, after={"qr_source": "UPLOADED"},
    )
    session.commit(); session.refresh(config)
    return config


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
    # Serialises the capacity decision — a whole-database lock on SQLite, a
    # single Match row lock on PostgreSQL (see lock_row). The database-level
    # partial unique index (uq_registration_active_per_user) is the final
    # backstop even if this serialisation had a bug: a second concurrent
    # active registration for the same (event, user) can never be written,
    # full stop.
    begin_serialized_write(session)
    lock_row(session, Match, match.id)
    match = session.scalar(select(Match).options(joinedload(Match.payment_configuration)).where(Match.id == match.id))
    config = match.payment_configuration
    if not config:
        raise api_error(409, "EVENT_PAYMENT_NOT_CONFIGURED", "This event has no payment configuration")
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
        # Freeze this event's payment configuration onto the Payment row now,
        # for the lifetime of this record — see Payment model docstring.
        payment = Payment(
            registration_id=registration.id, status=PAYMENT_AWAITING_PROOF, amount_due=match.fee,
            payee_upi_id_snapshot=config.payee_upi_id, payee_name_snapshot=config.payee_name,
            qr_source_snapshot=config.qr_source, qr_storage_key_snapshot=config.qr_storage_key,
        )
        session.add(payment)
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
    begin_serialized_write(session)
    lock_row(session, Registration, registration_id)
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
    # A cancelled registration is no longer a valid participant — it must
    # come off any team it was on, regardless of that team's roster-lock
    # state (see _assert_roster_unlocked): the team already played with this
    # person on it, if it played at all, and that fact is untouched here —
    # this only prevents the *now-cancelled* player from still appearing on
    # a live/future roster.
    member = session.scalar(select(TeamMember).where(TeamMember.registration_id == registration.id))
    if member:
        _delete_team_membership(
            session, member, event_type="TEAM_MEMBER_REMOVED",
            actor_type=actor_type, actor_id=actor_id, reason="registration_cancelled",
        )
    _refresh_full_status(session, registration.match)
    session.commit(); session.refresh(registration)
    logger.info("registration_cancelled registration_id=%s actor_type=%s", registration.id, actor_type)
    return registration


def validate_utr(value: str | None) -> str | None:
    """UTR is optional, player-supplied evidence — never verified against any
    real payment network, never treated as proof of payment on its own. Only
    a loose syntax/length check; there is no single universal UTR format."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not UTR_PATTERN.match(value):
        raise api_error(422, "VALIDATION_ERROR", "UTR/reference should be 6-30 letters and numbers")
    return value


async def read_and_validate_image(upload: UploadFile, *, max_bytes: int = MAX_UPLOAD_BYTES) -> tuple[bytes, str, str, str]:
    """Validates extension, Content-Type header, size, and — via an actual
    Pillow decode, not just header inspection — the image's real magic
    bytes/format. Returns (content, sha256_hex, content_type, image_format);
    does not write anything to storage."""
    if Path(upload.filename or "").suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"} or upload.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise api_error(400, "INVALID_UPLOAD", "Upload a JPEG, PNG, or WEBP image")
    content = await upload.read(max_bytes + 1)
    if len(content) > max_bytes: raise api_error(413, "UPLOAD_TOO_LARGE", "File must be 5 MB or smaller")
    try:
        image = Image.open(BytesIO(content)); image.verify()
        image = Image.open(BytesIO(content)); image_format = image.format
    except (UnidentifiedImageError, OSError):
        raise api_error(400, "INVALID_UPLOAD", "Invalid image")
    if image_format not in ALLOWED_FORMATS: raise api_error(400, "INVALID_UPLOAD", "Upload a JPEG, PNG, or WEBP image")
    digest = hashlib.sha256(content).hexdigest()
    return content, digest, ALLOWED_FORMATS[image_format][1], image_format


def persist_image(storage: Storage, content: bytes, image_format: str) -> str:
    """Generates an opaque storage key (never derived from user input) and
    writes the bytes. No filename, path, or extension the caller supplied is
    ever used — this is what makes path traversal structurally impossible."""
    extension, content_type = ALLOWED_FORMATS[image_format]
    key = f"{uuid.uuid4().hex}{extension}"
    storage.put(key, content, content_type)
    return key


def _load_registration_for_payment(session: Session, registration_key: str) -> Registration:
    registration = session.scalar(
        select(Registration).options(
            joinedload(Registration.match), joinedload(Registration.payment).joinedload(Payment.proofs),
        ).where(Registration.public_id == registration_key)
    )
    if not registration: raise api_error(404, "RESOURCE_NOT_FOUND", "Registration not found")
    return registration


def _assert_registration_open_for_payment(registration: Registration) -> None:
    if registration.status == WAITLISTED: raise api_error(409, "EVENT_FULL", "This registration is on the waitlist")
    if registration.status == CONFIRMED: raise api_error(409, "PAYMENT_ALREADY_VERIFIED", "This registration is already confirmed")
    if registration.status == CANCELLED: raise api_error(409, "INVALID_STATE_TRANSITION", "This registration has been cancelled")


async def submit_payment_proof(session: Session, registration_key: str, upload: UploadFile, utr: str | None, storage: Storage, user_id: int) -> Registration:
    """Appends a new PaymentProof. Never sets Payment to VERIFIED — only an
    organizer review can do that (see review_payment). A player may resubmit
    at any point before VERIFIED, including while one proof is still PENDING
    (e.g. they realise they picked the wrong screenshot); the previously
    PENDING proof is marked SUPERSEDED, never deleted or overwritten.

    Two-phase, same shape as the Phase 2B concurrency fix: the upload itself
    (network I/O + Pillow decode + hash) happens with no lock held; only the
    guard-check-then-write of the proof row is serialised under BEGIN
    IMMEDIATE, with every guard re-checked after the lock is acquired.
    """
    utr = validate_utr(utr)
    registration = _load_registration_for_payment(session, registration_key)
    assert_registration_owner(registration, user_id)
    _assert_registration_open_for_payment(registration)
    payment = registration.payment

    content, digest, content_type, image_format = await read_and_validate_image(upload)

    # Idempotent-retry short-circuit: if the caller's own still-PENDING proof
    # already has this exact hash, this is almost certainly the same logical
    # request replayed (e.g. a mobile client that timed out waiting for the
    # response and retried) — return current state rather than creating a
    # duplicate row or raising a false conflict.
    existing_pending = next((p for p in payment.proofs if p.status == PROOF_PENDING), None)
    if existing_pending and existing_pending.submitted_by_user_id == user_id and existing_pending.screenshot_hash == digest:
        return registration
    if payment.status == PAYMENT_VERIFIED:
        raise api_error(409, "PAYMENT_ALREADY_VERIFIED", "This registration is already confirmed")

    key = persist_image(storage, content, image_format)

    begin_serialized_write(session)
    lock_row(session, Payment, payment.id)
    registration = _load_registration_for_payment(session, registration_key)
    payment = registration.payment
    try:
        _assert_registration_open_for_payment(registration)
        if payment.status == PAYMENT_VERIFIED:
            raise api_error(409, "PAYMENT_ALREADY_VERIFIED", "This registration is already confirmed")
        existing_pending = next((p for p in payment.proofs if p.status == PROOF_PENDING), None)
        if existing_pending and existing_pending.submitted_by_user_id == user_id and existing_pending.screenshot_hash == digest:
            # This exact upload already exists as the pending proof — nothing
            # new to keep. The object just written under `key` is left in
            # place, unreferenced: the application's storage credential has
            # no delete permission over payment-proof objects (evidence is
            # append-only), so this harmless orphan is left for the
            # exceptional, separately-credentialed reconciliation process
            # (scripts/reconcile_storage.py) to report, never for normal
            # request-handling code to clean up itself.
            session.commit()
            return registration
    except HTTPException:
        # Lost the race — `key` is an orphaned object, same reasoning as
        # above: never deleted by this code path.
        raise

    if existing_pending:
        existing_pending.status = PROOF_SUPERSEDED
        existing_pending.superseded_at = now_ist()
    duplicate_hits = _find_duplicate_proofs_by_hash(session, digest, exclude_payment_id=payment.id)
    proof = PaymentProof(
        payment_id=payment.id, storage_key=key, screenshot_hash=digest, file_size=len(content),
        content_type=content_type, utr_reference=utr, submitted_by_user_id=user_id, status=PROOF_PENDING,
    )
    session.add(proof)
    session.flush()
    if payment.status != PAYMENT_SUBMITTED:
        transition_payment(session, payment, PAYMENT_SUBMITTED, event_type="PAYMENT_PROOF_SUBMITTED", actor_type="PLAYER", actor_id=user_id, metadata={"proof_id": proof.id})
    payment.submitted_at = payment.submitted_at or now_ist()
    payment.rejection_reason = None
    audit(
        session, "PAYMENT_PROOF_SUBMITTED", "payment_proof", proof.id,
        {"payment_id": payment.id, "screenshot_hash": digest, "duplicate_of": [d["proof_id"] for d in duplicate_hits]},
        actor_type="PLAYER", actor_id=user_id,
    )
    session.commit(); session.refresh(registration)
    logger.info("payment_proof_submitted registration_id=%s payment_id=%s proof_id=%s", registration.id, payment.id, proof.id)
    return registration


def _find_duplicate_proofs_by_hash(session: Session, screenshot_hash: str, *, exclude_payment_id: int) -> list[dict]:
    rows = session.execute(
        select(PaymentProof, Registration)
        .join(Payment, PaymentProof.payment_id == Payment.id)
        .join(Registration, Payment.registration_id == Registration.id)
        .where(PaymentProof.screenshot_hash == screenshot_hash, Payment.id != exclude_payment_id)
    ).all()
    return [{"proof_id": p.id, "registration_id": r.id, "player_name": r.name} for p, r in rows]


def review_payment(session: Session, payment_id: int, approve: bool, *, actor_id: int, reason: str | None = None) -> Registration:
    """The single place a Payment is ever reviewed. Requires exactly one
    PENDING proof (an invariant the database also enforces — see
    uq_payment_proof_one_pending); a SUBMITTED payment with none would be a
    data-integrity bug, not a normal 409, so it raises rather than guessing
    which proof to act on."""
    begin_serialized_write(session)
    lock_row(session, Payment, payment_id)
    payment = session.scalar(
        select(Payment).options(joinedload(Payment.registration).joinedload(Registration.match), joinedload(Payment.proofs))
        .where(Payment.id == payment_id)
    )
    if not payment: raise api_error(404, "RESOURCE_NOT_FOUND", "Payment not found")
    if payment.status != PAYMENT_SUBMITTED: raise api_error(409, "PAYMENT_ALREADY_REVIEWED", "Payment has already been reviewed")
    pending = next((p for p in payment.proofs if p.status == PROOF_PENDING), None)
    if not pending:
        raise RuntimeError(f"Payment {payment.id} is SUBMITTED but has no PENDING proof")
    registration = payment.registration
    if approve:
        if match_counts(session, registration.match_id)["confirmed"] >= registration.match.capacity:
            transition_registration(session, registration, WAITLISTED, event_type="PLAYER_WAITLISTED", actor_type="ORGANIZER", actor_id=actor_id, metadata={"reason": "capacity"})
            transition_payment(session, payment, PAYMENT_REJECTED, event_type="PAYMENT_REJECTED", actor_type="ORGANIZER", actor_id=actor_id, metadata={"reason": "capacity"})
            payment.rejection_reason = "Event filled before payment could be confirmed"
            pending.status = PROOF_REJECTED; pending.rejection_reason = payment.rejection_reason
        else:
            transition_registration(session, registration, CONFIRMED, event_type="PAYMENT_VERIFIED", actor_type="ORGANIZER", actor_id=actor_id, metadata={"payment_id": payment.id})
            transition_payment(session, payment, PAYMENT_VERIFIED, event_type="PAYMENT_VERIFIED", actor_type="ORGANIZER", actor_id=actor_id, metadata={"proof_id": pending.id})
            payment.verified_at = now_ist()
            pending.status = PROOF_ACCEPTED
    else:
        transition_registration(session, registration, REJECTED, event_type="PAYMENT_REJECTED", actor_type="ORGANIZER", actor_id=actor_id, metadata={"payment_id": payment.id})
        transition_payment(session, payment, PAYMENT_REJECTED, event_type="PAYMENT_REJECTED", actor_type="ORGANIZER", actor_id=actor_id, metadata={"proof_id": pending.id})
        payment.rejection_reason = reason
        pending.status = PROOF_REJECTED; pending.rejection_reason = reason
    pending.reviewed_by_organizer_id = actor_id; pending.reviewed_at = now_ist()
    _refresh_full_status(session, registration.match)
    session.commit(); session.refresh(registration)
    logger.info("payment_reviewed payment_id=%s approved=%s registration_id=%s", payment.id, approve, registration.id)
    return registration


def promote_waitlisted(session: Session, registration_id: int, *, actor_id: int) -> Registration:
    begin_serialized_write(session)
    lock_row(session, Registration, registration_id)
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


# ---------------------------------------------------------------------------
# Phase 4: Teams and Matches. "Fixture" is an internal name only, chosen to
# avoid colliding with the Match class above (which is actually the Event
# entity) — every user-facing string says "Match"/"Matches" (see
# routers/fixtures.py and the frontend). No results/stats/media here — see
# the Phase 4 plan's "Forward compatibility" section for why Fixture.id and
# the roster-lock rule below are enough to support those later without a
# redesign.
# ---------------------------------------------------------------------------

def _team_has_locking_fixture(session: Session, team_id: int) -> bool:
    return bool(session.scalar(
        select(func.count(Fixture.id)).where(
            or_(Fixture.team_a_id == team_id, Fixture.team_b_id == team_id),
            Fixture.status.in_(FIXTURE_ROSTER_LOCKING_STATUSES),
        )
    ))


def _assert_roster_unlocked(session: Session, team_id: int) -> None:
    """A team's roster freezes the moment any of its fixtures reaches
    IN_PROGRESS or COMPLETED — see models.TeamMember's docstring. This is
    the one check that makes "who was on Team A when it played" a durable
    fact without a per-fixture roster snapshot table."""
    if _team_has_locking_fixture(session, team_id):
        raise api_error(409, "TEAM_ROSTER_LOCKED", "This team has already played a match; its roster can no longer be changed")


def team_member_count(session: Session, team_id: int) -> int:
    return session.scalar(select(func.count(TeamMember.id)).where(TeamMember.team_id == team_id)) or 0


def team_to_response(session: Session, team: Team) -> dict:
    return {
        "id": team.id, "event_id": team.event_id, "name": team.name, "short_code": team.short_code,
        "max_size": team.max_size, "member_count": team_member_count(session, team.id),
        "created_at": team.created_at, "updated_at": team.updated_at,
    }


def team_member_to_response(member: TeamMember) -> dict:
    registration = member.registration
    return {
        "id": member.id, "team_id": member.team_id, "registration_id": member.registration_id,
        "player_name": registration.name, "preferred_position": registration.preferred_position or "NO_PREFERENCE",
        "assigned_position": registration.assigned_position, "created_at": member.created_at,
    }


def team_roster_response(session: Session, team: Team) -> dict:
    members = session.scalars(
        select(TeamMember).options(joinedload(TeamMember.registration))
        .where(TeamMember.team_id == team.id).order_by(TeamMember.created_at)
    ).all()
    data = team_to_response(session, team)
    data["members"] = [team_member_to_response(m) for m in members]
    return data


def create_team(session: Session, match: Match, payload: TeamCreate, organizer: Organizer) -> Team:
    if match.status == "CANCELLED":
        raise api_error(409, "EVENT_CANCELLED", "Cannot create teams for a cancelled event")
    team = Team(event_id=match.id, name=payload.name, short_code=payload.short_code, max_size=payload.max_size)
    session.add(team)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise api_error(409, "TEAM_NAME_TAKEN", "A team with this name already exists for this event")
    audit(session, "TEAM_CREATED", "team", team.id, {"event_id": match.id, "name": team.name}, actor_type="ORGANIZER", actor_id=organizer.id)
    session.commit(); session.refresh(team)
    logger.info("team_created team_id=%s event_id=%s", team.id, match.id)
    return team


def update_team(session: Session, team: Team, payload: TeamUpdate, organizer: Organizer) -> Team:
    changes = payload.model_dump(exclude_unset=True)
    before = {"name": team.name, "short_code": team.short_code, "max_size": team.max_size}
    for field, value in changes.items():
        setattr(team, field, value)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise api_error(409, "TEAM_NAME_TAKEN", "A team with this name already exists for this event")
    audit(
        session, "TEAM_UPDATED", "team", team.id, {"fields": sorted(changes)},
        actor_type="ORGANIZER", actor_id=organizer.id, before=before, after=changes,
    )
    session.commit(); session.refresh(team)
    return team


def delete_team(session: Session, team: Team, organizer: Organizer) -> None:
    """Removing a team is only safe when it has never appeared in a match —
    otherwise the match's team reference would dangle, destroying history
    (see the Phase 4 plan's match invariants). Current members are cascaded
    (ORM cascade="all, delete-orphan" on Team.members) — safe here precisely
    *because* no fixture exists, so no roster-lock question arises."""
    has_fixture = bool(session.scalar(
        select(func.count(Fixture.id)).where(or_(Fixture.team_a_id == team.id, Fixture.team_b_id == team.id))
    ))
    if has_fixture:
        raise api_error(409, "TEAM_HAS_MATCHES", "Remove this team's matches before deleting it")
    audit(
        session, "TEAM_REMOVED", "team", team.id, {"event_id": team.event_id, "name": team.name},
        actor_type="ORGANIZER", actor_id=organizer.id,
    )
    session.delete(team)
    session.commit()


def _delete_team_membership(
    session: Session, member: TeamMember, *, event_type: str, actor_type: str, actor_id: int | None, reason: str | None = None,
) -> None:
    metadata = {"team_id": member.team_id, "registration_id": member.registration_id}
    if reason:
        metadata["reason"] = reason
    audit(session, event_type, "team_member", member.id, metadata, actor_type=actor_type, actor_id=actor_id)
    session.delete(member)
    session.flush()


def assign_team_member(session: Session, team: Team, registration: Registration, organizer: Organizer) -> TeamMember:
    """Assigns a CONFIRMED registration to a team in the same event. Locked
    on the Team row (not Registration): the invariant this protects —
    capacity, and "not already on a team" — is fundamentally about the team's
    current membership set, exactly the same reasoning create_registration
    uses for locking Match rather than the incoming Registration."""
    if registration.match_id != team.event_id:
        raise api_error(409, "CROSS_EVENT_REGISTRATION", "This registration does not belong to this event")
    begin_serialized_write(session)
    lock_row(session, Team, team.id)
    team = session.get(Team, team.id)
    registration = session.get(Registration, registration.id)
    if registration.status != CONFIRMED:
        raise api_error(409, "REGISTRATION_NOT_CONFIRMED", "Only confirmed players can be assigned to a team")
    _assert_roster_unlocked(session, team.id)
    if team.max_size is not None and team_member_count(session, team.id) >= team.max_size:
        raise api_error(409, "TEAM_FULL", "This team is already at capacity")
    member = TeamMember(team_id=team.id, registration_id=registration.id, event_id=team.event_id, assigned_by_organizer_id=organizer.id)
    session.add(member)
    try:
        session.flush()
    except IntegrityError:
        # The DB's own backstop — uq_team_member_registration — catching a
        # race this lock should already have prevented (two organizer tabs
        # assigning the same player at once).
        session.rollback()
        raise api_error(409, "ALREADY_ON_A_TEAM", "This player is already assigned to a team for this event")
    audit(
        session, "TEAM_MEMBER_ASSIGNED", "team_member", member.id, {"team_id": team.id, "registration_id": registration.id},
        actor_type="ORGANIZER", actor_id=organizer.id,
    )
    session.commit(); session.refresh(member)
    logger.info("team_member_assigned team_id=%s registration_id=%s", team.id, registration.id)
    return member


def move_team_member(session: Session, member: TeamMember, new_team: Team, organizer: Organizer) -> TeamMember:
    if new_team.event_id != member.event_id:
        raise api_error(409, "CROSS_EVENT_TEAM", "Cannot move a player to a team from a different event")
    old_team_id = member.team_id
    if old_team_id == new_team.id:
        return member
    # Lock both team rows in ascending id order — the one place this phase
    # takes two row locks at once. A fixed, deterministic order across every
    # caller is what prevents two concurrent "swap A<->B" moves from
    # deadlocking on PostgreSQL.
    first_id, second_id = sorted((old_team_id, new_team.id))
    begin_serialized_write(session)
    lock_row(session, Team, first_id)
    lock_row(session, Team, second_id)
    member = session.get(TeamMember, member.id)
    new_team = session.get(Team, new_team.id)
    _assert_roster_unlocked(session, old_team_id)
    _assert_roster_unlocked(session, new_team.id)
    if new_team.max_size is not None and team_member_count(session, new_team.id) >= new_team.max_size:
        raise api_error(409, "TEAM_FULL", "The destination team is already at capacity")
    before = {"team_id": old_team_id}
    member.team_id = new_team.id
    session.flush()
    audit(
        session, "TEAM_MEMBER_MOVED", "team_member", member.id, {"from_team_id": old_team_id, "to_team_id": new_team.id},
        actor_type="ORGANIZER", actor_id=organizer.id, before=before, after={"team_id": new_team.id},
    )
    session.commit(); session.refresh(member)
    return member


def remove_team_member(session: Session, member: TeamMember, organizer: Organizer) -> None:
    _assert_roster_unlocked(session, member.team_id)
    _delete_team_membership(session, member, event_type="TEAM_MEMBER_REMOVED", actor_type="ORGANIZER", actor_id=organizer.id)
    session.commit()


def _load_team_in_event_or_422(session: Session, team_id: int, event_id: int) -> Team:
    team = session.get(Team, team_id)
    if not team or team.event_id != event_id:
        raise api_error(422, "VALIDATION_ERROR", "Team does not belong to this event")
    return team


def create_fixture(session: Session, match: Match, payload: FixtureCreate, organizer: Organizer) -> Fixture:
    if match.status == "CANCELLED":
        raise api_error(409, "EVENT_CANCELLED", "Cannot create matches for a cancelled event")
    _load_team_in_event_or_422(session, payload.team_a_id, match.id)
    _load_team_in_event_or_422(session, payload.team_b_id, match.id)
    fixture = Fixture(
        event_id=match.id, team_a_id=payload.team_a_id, team_b_id=payload.team_b_id, sequence=payload.sequence,
        scheduled_at=payload.scheduled_at, venue_override=payload.venue_override,
    )
    session.add(fixture)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise api_error(409, "FIXTURE_SEQUENCE_TAKEN", "A match with this number already exists for this event")
    audit(
        session, "FIXTURE_CREATED", "fixture", fixture.id,
        {"event_id": match.id, "team_a_id": fixture.team_a_id, "team_b_id": fixture.team_b_id},
        actor_type="ORGANIZER", actor_id=organizer.id,
    )
    session.commit(); session.refresh(fixture)
    logger.info("fixture_created fixture_id=%s event_id=%s", fixture.id, match.id)
    return fixture


def update_fixture(session: Session, fixture: Fixture, payload: FixtureUpdate, organizer: Organizer) -> Fixture:
    """Team/time/sequence/venue can only change while SCHEDULED ("edit
    before it starts"); once IN_PROGRESS/COMPLETED/CANCELLED, only a further
    valid status transition is allowed — this is what keeps a played match a
    stable historical object (see the Phase 4 plan's forward-compatibility
    notes for Results/Media)."""
    changes = payload.model_dump(exclude_unset=True)
    changed_fields = sorted(changes)
    requested_status = changes.pop("status", None)
    before = {"status": fixture.status}
    if changes and fixture.status != "SCHEDULED":
        raise api_error(409, "FIXTURE_ALREADY_STARTED", "This match can no longer be edited")
    if "team_a_id" in changes or "team_b_id" in changes:
        team_a_id = changes.get("team_a_id", fixture.team_a_id)
        team_b_id = changes.get("team_b_id", fixture.team_b_id)
        if team_a_id == team_b_id:
            raise api_error(422, "VALIDATION_ERROR", "A team cannot play itself")
        _load_team_in_event_or_422(session, team_a_id, fixture.event_id)
        _load_team_in_event_or_422(session, team_b_id, fixture.event_id)
    for field, value in changes.items():
        setattr(fixture, field, value)
    if requested_status and requested_status != fixture.status:
        if requested_status not in VALID_FIXTURE_TRANSITIONS.get(fixture.status, set()):
            raise api_error(409, "INVALID_STATE_TRANSITION", f"Cannot move a match from {fixture.status} to {requested_status}")
        fixture.status = requested_status
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise api_error(409, "FIXTURE_SEQUENCE_TAKEN", "A match with this number already exists for this event")
    audit(
        session, "FIXTURE_STATUS_CHANGED" if requested_status else "FIXTURE_UPDATED", "fixture", fixture.id,
        {"fields": changed_fields}, actor_type="ORGANIZER", actor_id=organizer.id, before=before, after={"status": fixture.status},
    )
    session.commit(); session.refresh(fixture)
    return fixture


def fixture_to_response(fixture: Fixture) -> dict:
    return {
        "id": fixture.id, "event_id": fixture.event_id,
        "team_a": {"id": fixture.team_a.id, "name": fixture.team_a.name, "short_code": fixture.team_a.short_code},
        "team_b": {"id": fixture.team_b.id, "name": fixture.team_b.name, "short_code": fixture.team_b.short_code},
        "sequence": fixture.sequence, "scheduled_at": fixture.scheduled_at, "venue_override": fixture.venue_override,
        "status": fixture.status, "created_at": fixture.created_at, "updated_at": fixture.updated_at,
    }


def _player_active_registration(session: Session, event_id: int, user_id: int) -> Registration | None:
    return session.scalar(
        select(Registration).where(
            Registration.match_id == event_id, Registration.user_id == user_id,
            Registration.status.in_(ACTIVE_REGISTRATION_STATUSES),
        )
    )


def player_team_response(session: Session, match: Match, user: User) -> dict:
    registration = _player_active_registration(session, match.id, user.id)
    member = session.scalar(select(TeamMember).where(TeamMember.registration_id == registration.id)) if registration else None
    if not member:
        return {"team": None, "teammates": []}
    team = session.get(Team, member.team_id)
    teammates = session.scalars(
        select(TeamMember).options(joinedload(TeamMember.registration))
        .where(TeamMember.team_id == team.id, TeamMember.id != member.id)
    ).all()
    return {
        "team": {"id": team.id, "name": team.name, "short_code": team.short_code},
        "teammates": [
            {
                "name": m.registration.name, "preferred_position": m.registration.preferred_position or "NO_PREFERENCE",
                "assigned_position": m.registration.assigned_position,
            }
            for m in teammates
        ],
    }


def player_fixtures_response(session: Session, match: Match, user: User) -> list[dict]:
    registration = _player_active_registration(session, match.id, user.id)
    my_member = session.scalar(select(TeamMember).where(TeamMember.registration_id == registration.id)) if registration else None
    my_team_id = my_member.team_id if my_member else None
    fixtures = session.scalars(
        select(Fixture).options(joinedload(Fixture.team_a), joinedload(Fixture.team_b))
        .where(Fixture.event_id == match.id).order_by(Fixture.scheduled_at)
    ).unique().all()
    return [
        {
            "id": f.id, "sequence": f.sequence, "scheduled_at": f.scheduled_at, "venue_override": f.venue_override,
            "status": f.status, "team_a": {"id": f.team_a.id, "name": f.team_a.name, "short_code": f.team_a.short_code},
            "team_b": {"id": f.team_b.id, "name": f.team_b.name, "short_code": f.team_b.short_code},
            "my_team_id": my_team_id if my_team_id in (f.team_a_id, f.team_b_id) else None,
        }
        for f in fixtures
    ]


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
    match = Match(public_id="sunday-cricket-2026", name="Sunday Cricket", date=date(2026, 8, 23), start_time=time(7), end_time=time(10), venue="PlayArena, Bellandur", capacity=22, fee=300, registration_deadline=datetime(2026, 8, 22, 20), status="OPEN", owner_organizer_id=owner_organizer_id)
    session.add(match); session.flush()
    session.add(EventPaymentConfiguration(match_id=match.id, payee_upi_id="strangerclub@upi", payee_name="Stranger Club", qr_source="GENERATED", updated_by_organizer_id=owner_organizer_id))
    session.commit(); logger.info("seed_event_created public_id=%s", match.public_id)
