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

from .models import Match, Organizer, OrganizerSession, now_ist
from .services import api_error, event_summary

SESSION_COOKIE = "sc_organizer_session"
SESSION_HOURS = int(os.getenv("SC_SESSION_HOURS", "12"))
PASSWORD_HASHER = PasswordHasher()
REGISTRATION_RATE_LIMIT = 20
REGISTRATION_RATE_WINDOW_SECONDS = 3600
PAYMENT_UPLOAD_RATE_LIMIT = 20
PAYMENT_UPLOAD_RATE_WINDOW_SECONDS = 3600


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


def publish_event_update(request: Request, session: Session, match_id: int, kind: str) -> None:
    match = session.get(Match, match_id)
    if match: request.app.state.broadcaster.publish(match.public_id, kind, event_summary(session, match))
