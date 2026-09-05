from __future__ import annotations

import hashlib
import os
import queue
import secrets
import time

from argon2 import PasswordHasher
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .models import Match, Organizer, OrganizerSession, Payment, PaymentProof, PlayerSession, Registration, User, now_ist
from .services import api_error, event_summary

PLATFORM_ADMIN = "PLATFORM_ADMIN"

SESSION_COOKIE = "sc_organizer_session"
SESSION_HOURS = int(os.getenv("SC_SESSION_HOURS", "12"))
PASSWORD_HASHER = PasswordHasher()
REGISTRATION_RATE_LIMIT = 20
REGISTRATION_RATE_WINDOW_SECONDS = 3600
PAYMENT_UPLOAD_RATE_LIMIT = 20
PAYMENT_UPLOAD_RATE_WINDOW_SECONDS = 3600

PLAYER_SESSION_COOKIE = "sc_player_session"
PLAYER_SESSION_DAYS = int(os.getenv("SC_PLAYER_SESSION_DAYS", "30"))
OTP_REQUEST_PHONE_LIMIT = 5
OTP_REQUEST_PHONE_WINDOW_SECONDS = 3600
OTP_REQUEST_IP_LIMIT = 10
OTP_REQUEST_IP_WINDOW_SECONDS = 3600
OTP_VERIFY_IP_LIMIT = 20
OTP_VERIFY_IP_WINDOW_SECONDS = 3600


class EventBroadcaster:
    """In-process fanout, deliberately small for this single-container deployment."""
    def __init__(self): self.subscribers: dict[str, set[queue.Queue]] = {}
    def subscribe(self, public_id: str) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=20); self.subscribers.setdefault(public_id, set()).add(channel); return channel
    def unsubscribe(self, public_id: str, channel: queue.Queue) -> None:
        self.subscribers.get(public_id, set()).discard(channel)
    def publish(self, public_id: str, event_type: str, payload: dict) -> None:
        message = {"type": event_type, "summary": payload}
        for channel in list(self.subscribers.get(public_id, set())):
            try: channel.put_nowait(message)
            except queue.Full:
                try: channel.get_nowait(); channel.put_nowait(message)
                except queue.Empty: pass


def token_hash(raw: str) -> str: return hashlib.sha256(raw.encode()).hexdigest()


def enforce_rate_limit(buckets: dict[str, list[float]], client: str, limit: int, window_seconds: int) -> None:
    """Small in-memory sliding-window limiter shared by public, unauthenticated endpoints."""
    attempts = [value for value in buckets.get(client, []) if time.time() - value < window_seconds]
    if len(attempts) >= limit:
        raise api_error(429, "RATE_LIMITED", "Too many requests. Please try again later.")
    attempts.append(time.time())
    buckets[client] = attempts


def get_session(request: Request):
    with request.app.state.session_factory() as session: yield session


def auth_context(request: Request, session: Session = Depends(get_session)) -> tuple[Organizer, OrganizerSession]:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw: raise api_error(401, "UNAUTHORIZED", "Authentication required")
    active = session.scalar(select(OrganizerSession).options(joinedload(OrganizerSession.organizer)).where(OrganizerSession.token_hash == token_hash(raw)))
    if not active or active.expires_at <= now_ist() or not active.organizer.is_active:
        if active: session.delete(active); session.commit()
        raise api_error(401, "UNAUTHORIZED", "Authentication required")
    active.last_seen_at = now_ist(); session.commit()
    return active.organizer, active


def require_admin(context: tuple[Organizer, OrganizerSession] = Depends(auth_context)) -> Organizer: return context[0]


def require_csrf(request: Request, context: tuple[Organizer, OrganizerSession] = Depends(auth_context)) -> Organizer:
    if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), context[1].csrf_token):
        raise api_error(403, "CSRF_INVALID", "Security token is invalid or expired")
    return context[0]


def organizer_owns_event(organizer: Organizer, match: Match) -> bool:
    return organizer.role == PLATFORM_ADMIN or match.owner_organizer_id == organizer.id


def organizer_events_filter(organizer: Organizer):
    """SQLAlchemy WHERE clause for list endpoints: PLATFORM_ADMIN sees every
    event, a regular organizer sees only events they own. One shared helper
    so the two admin list endpoints (events, pending payments) can't drift."""
    if organizer.role == PLATFORM_ADMIN:
        return True
    return Match.owner_organizer_id == organizer.id


def require_event_access(match_id: int, organizer: Organizer = Depends(require_admin), session: Session = Depends(get_session)) -> Match:
    """Loads a Match by its internal id, enforcing organizer ownership (or
    PLATFORM_ADMIN). 404, not 403, on a mismatch — the existing player
    ownership pattern, applied here too: a non-owning organizer must not be
    able to tell "wrong owner" apart from "doesn't exist"."""
    match = session.get(Match, match_id)
    if not match or not organizer_owns_event(organizer, match):
        raise api_error(404, "EVENT_NOT_FOUND", "Event not found")
    return match


def require_payment_access(payment_id: int, organizer: Organizer = Depends(require_admin), session: Session = Depends(get_session)) -> Payment:
    payment = session.scalar(select(Payment).options(joinedload(Payment.registration).joinedload(Registration.match)).where(Payment.id == payment_id))
    if not payment or not organizer_owns_event(organizer, payment.registration.match):
        raise api_error(404, "RESOURCE_NOT_FOUND", "Payment not found")
    return payment


def require_proof_access(proof_id: int, organizer: Organizer = Depends(require_admin), session: Session = Depends(get_session)) -> PaymentProof:
    proof = session.scalar(
        select(PaymentProof).options(joinedload(PaymentProof.payment).joinedload(Payment.registration).joinedload(Registration.match))
        .where(PaymentProof.id == proof_id)
    )
    if not proof or not organizer_owns_event(organizer, proof.payment.registration.match):
        raise api_error(404, "RESOURCE_NOT_FOUND", "Payment proof not found")
    return proof


def require_registration_access(registration_id: int, organizer: Organizer = Depends(require_admin), session: Session = Depends(get_session)) -> Registration:
    registration = session.scalar(select(Registration).options(joinedload(Registration.match)).where(Registration.id == registration_id))
    if not registration or not organizer_owns_event(organizer, registration.match):
        raise api_error(404, "RESOURCE_NOT_FOUND", "Registration not found")
    return registration


def publish_event_update(request: Request, session: Session, match_id: int, kind: str) -> None:
    match = session.get(Match, match_id)
    if match: request.app.state.broadcaster.publish(match.public_id, kind, event_summary(session, match))


def player_auth_context(request: Request, session: Session = Depends(get_session)) -> tuple[User, PlayerSession]:
    raw = request.cookies.get(PLAYER_SESSION_COOKIE)
    if not raw: raise api_error(401, "UNAUTHORIZED", "Authentication required")
    active = session.scalar(select(PlayerSession).options(joinedload(PlayerSession.user)).where(PlayerSession.token_hash == token_hash(raw)))
    if not active or active.expires_at <= now_ist() or not active.user.is_active:
        if active: session.delete(active); session.commit()
        raise api_error(401, "UNAUTHORIZED", "Authentication required")
    active.last_seen_at = now_ist(); session.commit()
    return active.user, active


def require_player(context: tuple[User, PlayerSession] = Depends(player_auth_context)) -> User: return context[0]


def require_player_csrf(request: Request, context: tuple[User, PlayerSession] = Depends(player_auth_context)) -> User:
    if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), context[1].csrf_token):
        raise api_error(403, "CSRF_INVALID", "Security token is invalid or expired")
    return context[0]
