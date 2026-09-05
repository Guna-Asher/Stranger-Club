from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from .database import make_session_factory
from .deps import PASSWORD_HASHER, EventBroadcaster
from .models import Match, Organizer
from .otp.base import OtpProvider
from .otp.console import ConsoleOtpProvider
from .routers import admin, auth, events, payments, player_auth, registrations
from .services import api_error, seed_database

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("stranger_club")


def create_app(data_dir: Path | None = None, frontend_dir: Path | None = None, admin_username: str | None = None, admin_password: str | None = None, otp_provider: OtpProvider | None = None) -> FastAPI:
    data_dir = data_dir or Path(os.getenv("SC_DATA_DIR", "data")); uploads_dir = data_dir / "uploads"
    session_factory = make_session_factory(data_dir / "stranger_club.db")
    username = admin_username or os.getenv("SC_ADMIN_USERNAME", "organizer")
    password = admin_password or os.getenv("SC_ADMIN_PASSWORD")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with session_factory() as session:
            seed_database(session)
            if not session.scalar(select(Organizer).where(Organizer.username == username)):
                if not password:
                    raise RuntimeError(
                        "No organizer account exists and SC_ADMIN_PASSWORD is not set. "
                        "Set SC_ADMIN_PASSWORD (and optionally SC_ADMIN_USERNAME) before starting the app."
                    )
                session.add(Organizer(username=username, password_hash=PASSWORD_HASHER.hash(password)))
                session.commit()
                logger.info("organizer_created username=%s", username)
        yield

    app = FastAPI(title="Stranger Club API", version="2.0.0", lifespan=lifespan)
    app.state.session_factory = session_factory; app.state.uploads_dir = uploads_dir; app.state.broadcaster = EventBroadcaster(); app.state.login_attempts: dict[str, list[float]] = {}
    app.state.registration_attempts: dict[str, list[float]] = {}; app.state.payment_upload_attempts: dict[str, list[float]] = {}
    app.state.otp_provider = otp_provider or ConsoleOtpProvider()
    app.state.otp_request_ip_attempts: dict[str, list[float]] = {}; app.state.otp_request_phone_attempts: dict[str, list[float]] = {}
    app.state.otp_verify_ip_attempts: dict[str, list[float]] = {}

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

    app.include_router(auth.router)
    app.include_router(player_auth.router)
    app.include_router(events.router)
    app.include_router(registrations.router)
    app.include_router(payments.router)
    app.include_router(admin.router)

    frontend_dir = frontend_dir or Path(__file__).resolve().parents[2] / "frontend_dist"
    if frontend_dir.is_dir() and (frontend_dir / "index.html").is_file():
        frontend_root = frontend_dir.resolve()
        app.mount("/assets", StaticFiles(directory=frontend_dir / "assets"), name="assets")
        @app.get("/{path:path}", include_in_schema=False)
        def frontend(path: str):
            candidate = (frontend_dir / path).resolve()
            if path and candidate.is_relative_to(frontend_root) and candidate.is_file(): return FileResponse(candidate)
            return FileResponse(frontend_dir / "index.html")
    return app


app = create_app()
