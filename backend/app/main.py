from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import secrets
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .database import make_session_factory
from .models import Match, Organizer, OrganizerSession, Payment, Registration, now_ist
from .schemas import AdminMatchDetail, AuthResponse, EventSummary, EventUpdate, LoginRequest, MatchCreate, MatchResponse, RegistrationCreate, RegistrationResponse, RejectRequest
from .services import (
    PAYMENT_SUBMITTED, api_error, create_match, create_registration, event_summary, get_public_match,
    match_to_response, promote_waitlisted, registration_to_response, seed_database, submit_payment,
    update_match, verify_payment,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("stranger_club")
SESSION_COOKIE = "sc_organizer_session"
SESSION_HOURS = int(os.getenv("SC_SESSION_HOURS", "12"))
PASSWORD_HASHER = PasswordHasher()


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


def create_app(data_dir: Path | None = None, frontend_dir: Path | None = None, admin_username: str | None = None, admin_password: str | None = None) -> FastAPI:
    data_dir = data_dir or Path(os.getenv("SC_DATA_DIR", "data")); uploads_dir = data_dir / "uploads"
    session_factory = make_session_factory(data_dir / "stranger_club.db")
    username = admin_username or os.getenv("SC_ADMIN_USERNAME", "organizer")
    password = admin_password or os.getenv("SC_ADMIN_PASSWORD") or ("stranger-club-dev" if os.getenv("SC_ENV", "development") != "production" else "")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with session_factory() as session:
            seed_database(session)
            if password and not session.scalar(select(Organizer).where(Organizer.username == username)):
                session.add(Organizer(username=username, password_hash=PASSWORD_HASHER.hash(password)))
                session.commit()
                if not os.getenv("SC_ADMIN_PASSWORD"): logger.warning("development organizer created; set SC_ADMIN_PASSWORD before production")
        yield

    app = FastAPI(title="Stranger Club API", version="2.0.0", lifespan=lifespan)
    app.state.session_factory = session_factory; app.state.uploads_dir = uploads_dir; app.state.broadcaster = EventBroadcaster(); app.state.login_attempts: dict[str, list[float]] = {}

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

    def publish(session: Session, match_id: int, kind: str) -> None:
        match = session.get(Match, match_id)
        if match: app.state.broadcaster.publish(match.public_id, kind, event_summary(session, match))

    @app.exception_handler(HTTPException)
    async def http_error_handler(_: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "REQUEST_FAILED", "message": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content={"error": detail})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError):
        message = exc.errors()[0].get("msg", "Invalid request") if exc.errors() else "Invalid request"
        return JSONResponse(status_code=422, content={"error": {"code": "VALIDATION_ERROR", "message": message}})

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_: Request, exc: Exception):
        logger.exception("unexpected_error", exc_info=exc)
        return JSONResponse(status_code=500, content={"error": {"code": "INTERNAL_ERROR", "message": "Something went wrong. Please try again."}})

    @app.get("/health")
    def health(): return {"status": "ok"}

    @app.get("/ready")
    def ready(request: Request):
        try:
            with request.app.state.session_factory() as session: session.execute(select(Match.id).limit(1))
        except Exception: raise api_error(503, "NOT_READY", "Database is unavailable")
        return {"status": "ready"}

    @app.post("/api/auth/login", response_model=AuthResponse)
    def login(payload: LoginRequest, request: Request, response: Response, session: Session = Depends(get_session)):
        client = request.client.host if request.client else "unknown"; attempts = [value for value in request.app.state.login_attempts.get(client, []) if time.time() - value < 900]
        request.app.state.login_attempts[client] = attempts
        if len(attempts) >= 8: raise api_error(429, "RATE_LIMITED", "Too many login attempts. Please try again later.")
        organizer = session.scalar(select(Organizer).where(Organizer.username == payload.username))
        valid = False
        if organizer and organizer.is_active:
            try: valid = PASSWORD_HASHER.verify(organizer.password_hash, payload.password)
            except VerifyMismatchError: valid = False
        if not valid:
            attempts.append(time.time()); request.app.state.login_attempts[client] = attempts
            logger.warning("login_failed remote=%s", client)
            raise api_error(401, "INVALID_CREDENTIALS", "Invalid username or password.")
        raw = secrets.token_urlsafe(32); csrf = secrets.token_urlsafe(24)
        session.add(OrganizerSession(token_hash=token_hash(raw), csrf_token=csrf, organizer_id=organizer.id, expires_at=now_ist() + timedelta(hours=SESSION_HOURS)))
        session.commit(); request.app.state.login_attempts.pop(client, None)
        response.set_cookie(SESSION_COOKIE, raw, httponly=True, secure=os.getenv("SC_COOKIE_SECURE", "false").lower() == "true", samesite="lax", max_age=SESSION_HOURS * 3600, path="/")
        logger.info("login_succeeded organizer_id=%s", organizer.id)
        return {"organizer": {"username": organizer.username, "role": organizer.role}, "csrf_token": csrf}

    @app.get("/api/auth/me", response_model=AuthResponse)
    def me(context: tuple[Organizer, OrganizerSession] = Depends(auth_context)):
        return {"organizer": {"username": context[0].username, "role": context[0].role}, "csrf_token": context[1].csrf_token}

    @app.post("/api/auth/logout", status_code=204)
    def logout(response: Response, _: Organizer = Depends(require_csrf), context: tuple[Organizer, OrganizerSession] = Depends(auth_context), session: Session = Depends(get_session)):
        session.delete(context[1]); session.commit(); response.delete_cookie(SESSION_COOKIE, path="/")
        logger.info("logout organizer_id=%s", context[0].id)

    @app.get("/api/events", response_model=list[MatchResponse])
    def public_events(session: Session = Depends(get_session)):
        return [match_to_response(session, item) for item in session.scalars(select(Match).where(Match.status.in_(("OPEN", "FULL", "ONGOING"))).order_by(Match.date, Match.start_time)).all()]

    @app.get("/api/events/{public_id}", response_model=MatchResponse)
    @app.get("/api/matches/{public_id}", response_model=MatchResponse)
    def public_event(public_id: str, session: Session = Depends(get_session)): return match_to_response(session, get_public_match(session, public_id))

    @app.get("/api/events/{public_id}/summary", response_model=EventSummary)
    def public_summary(public_id: str, session: Session = Depends(get_session)): return event_summary(session, get_public_match(session, public_id))

    @app.get("/api/events/{public_id}/stream")
    def event_stream(public_id: str, request: Request, session: Session = Depends(get_session)):
        initial_summary = event_summary(session, get_public_match(session, public_id)); channel = request.app.state.broadcaster.subscribe(public_id)
        def stream():
            try:
                yield f"event: summary\ndata: {json.dumps(initial_summary, default=str)}\n\n"
                while True:
                    try: item = channel.get(timeout=15); yield f"event: {item['type']}\ndata: {json.dumps(item['summary'], default=str)}\n\n"
                    except queue.Empty: yield ": keepalive\n\n"
            finally: request.app.state.broadcaster.unsubscribe(public_id, channel)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/events/{public_id}/registrations", response_model=RegistrationResponse, status_code=201)
    @app.post("/api/matches/{public_id}/registrations", response_model=RegistrationResponse, status_code=201)
    def register(public_id: str, payload: RegistrationCreate, request: Request, session: Session = Depends(get_session)):
        registration = create_registration(session, get_public_match(session, public_id), payload); publish(session, registration.match_id, "REGISTRATION_CREATED"); return registration_to_response(registration)

    @app.get("/api/registrations/{registration_key}", response_model=RegistrationResponse)
    def registration_status(registration_key: str, session: Session = Depends(get_session)):
        condition = Registration.public_id == registration_key if not registration_key.isdigit() else Registration.id == int(registration_key)
        registration = session.scalar(select(Registration).options(joinedload(Registration.payment)).where(condition))
        if not registration: raise api_error(404, "RESOURCE_NOT_FOUND", "Registration not found")
        return registration_to_response(registration)

    @app.post("/api/registrations/{registration_key}/payment", response_model=RegistrationResponse)
    async def upload_payment(request: Request, registration_key: str, screenshot: UploadFile = File(...), session: Session = Depends(get_session)):
        registration = await submit_payment(session, registration_key, screenshot, request.app.state.uploads_dir); publish(session, registration.match_id, "PAYMENT_SUBMITTED"); return registration_to_response(registration)

    @app.get("/api/admin/events", response_model=list[MatchResponse])
    @app.get("/api/admin/matches", response_model=list[MatchResponse])
    def admin_events(_: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
        return [match_to_response(session, item) for item in session.scalars(select(Match).order_by(Match.date.desc(), Match.start_time.desc())).all()]

    @app.post("/api/admin/events", response_model=MatchResponse, status_code=201)
    @app.post("/api/admin/matches", response_model=MatchResponse, status_code=201)
    def admin_create_event(payload: MatchCreate, request: Request, _: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
        match = create_match(session, payload); publish(session, match.id, "EVENT_CREATED"); return match_to_response(session, match)

    @app.get("/api/admin/events/{match_id}", response_model=AdminMatchDetail)
    @app.get("/api/admin/matches/{match_id}", response_model=AdminMatchDetail)
    def admin_event_detail(match_id: int, _: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
        match = session.scalar(select(Match).options(joinedload(Match.registrations).joinedload(Registration.payment)).where(Match.id == match_id))
        if not match: raise api_error(404, "EVENT_NOT_FOUND", "Event not found")
        data = match_to_response(session, match); data["registrations"] = [registration_to_response(item, include_proof=True) for item in sorted(match.registrations, key=lambda item: item.created_at, reverse=True)]; return data

    @app.patch("/api/admin/events/{match_id}", response_model=MatchResponse)
    def admin_update_event(match_id: int, payload: EventUpdate, request: Request, _: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
        match = session.get(Match, match_id)
        if not match: raise api_error(404, "EVENT_NOT_FOUND", "Event not found")
        match = update_match(session, match, payload); publish(session, match.id, "EVENT_UPDATED"); return match_to_response(session, match)

    @app.get("/api/admin/events/{match_id}/summary", response_model=EventSummary)
    def admin_summary(match_id: int, _: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
        match = session.get(Match, match_id)
        if not match: raise api_error(404, "EVENT_NOT_FOUND", "Event not found")
        return event_summary(session, match)

    @app.get("/api/admin/events/{match_id}/registrations", response_model=list[RegistrationResponse])
    @app.get("/api/admin/matches/{match_id}/registrations", response_model=list[RegistrationResponse])
    def admin_registrations(match_id: int, _: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
        if not session.get(Match, match_id): raise api_error(404, "EVENT_NOT_FOUND", "Event not found")
        records = session.scalars(select(Registration).options(joinedload(Registration.payment)).where(Registration.match_id == match_id).order_by(Registration.created_at.desc())).all(); return [registration_to_response(item, include_proof=True) for item in records]

    @app.get("/api/admin/payments/pending", response_model=list[RegistrationResponse])
    def pending_payments(_: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
        records = session.scalars(select(Registration).options(joinedload(Registration.payment)).join(Payment).where(Payment.status == PAYMENT_SUBMITTED).order_by(Payment.submitted_at)).all(); return [registration_to_response(item, include_proof=True) for item in records]

    @app.post("/api/admin/payments/{payment_id}/confirm", response_model=RegistrationResponse)
    def confirm_payment(payment_id: int, request: Request, _: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
        registration = verify_payment(session, payment_id, approve=True); publish(session, registration.match_id, "PAYMENT_VERIFIED"); return registration_to_response(registration, include_proof=True)

    @app.post("/api/admin/payments/{payment_id}/reject", response_model=RegistrationResponse)
    def reject_payment(payment_id: int, payload: RejectRequest, request: Request, _: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
        registration = verify_payment(session, payment_id, approve=False, reason=payload.reason.strip()); publish(session, registration.match_id, "PAYMENT_REJECTED"); return registration_to_response(registration, include_proof=True)

    @app.post("/api/admin/registrations/{registration_id}/promote", response_model=RegistrationResponse)
    def promote(registration_id: int, request: Request, _: Organizer = Depends(require_csrf), session: Session = Depends(get_session)):
        registration = promote_waitlisted(session, registration_id); publish(session, registration.match_id, "WAITLIST_UPDATED"); return registration_to_response(registration, include_proof=True)

    @app.get("/api/payment-proofs/{token}")
    def payment_proof(token: str, request: Request, _: Organizer = Depends(require_admin), session: Session = Depends(get_session)):
        payment = session.scalar(select(Payment).where(Payment.screenshot_token == token))
        if not payment: raise api_error(404, "RESOURCE_NOT_FOUND", "Payment proof not found")
        file_path = request.app.state.uploads_dir / payment.screenshot_path
        if not file_path.is_file(): raise api_error(404, "RESOURCE_NOT_FOUND", "Payment proof not found")
        return FileResponse(file_path)

    frontend_dir = frontend_dir or Path(__file__).resolve().parents[2] / "frontend_dist"
    if frontend_dir.is_dir() and (frontend_dir / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=frontend_dir / "assets"), name="assets")
        @app.get("/{path:path}", include_in_schema=False)
        def frontend(path: str): return FileResponse(frontend_dir / "index.html")
    return app


app = create_app()
