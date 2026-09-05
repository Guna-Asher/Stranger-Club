from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from .config import AppConfig, load_config
from .database import dialect_name, make_session_factory
from .deps import PASSWORD_HASHER, PLATFORM_ADMIN, require_admin
from .middleware import CorrelationIdMiddleware, RequestIdLogFilter, current_request_id
from .models import Match, Organizer
from .otp.base import OtpProvider
from .otp.console import ConsoleOtpProvider
from .realtime import InProcessBroadcaster, PostgresBroadcaster
from .routers import admin, auth, events, fixtures, payments, player_auth, registrations, teams
from .services import api_error, backfill_missing_event_ownership, seed_database
from .storage import LocalFilesystemStorage, Storage

logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s",
)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(RequestIdLogFilter())
logger = logging.getLogger("stranger_club")

# The Alembic revision this application version expects the database to be
# at. /ready compares this against the database's actual alembic_version —
# a mismatch (new code deployed before its migration ran, or a migration
# that partially failed) fails readiness instead of serving traffic against
# an unexpected schema. SQLite dev/test databases are stamped to head at
# every startup (see database.py) so this check only applies to PostgreSQL.
ALEMBIC_EXPECTED_HEAD = "0008"


def _resolve_database_url(config: AppConfig, data_dir: Path | None, database_url: str | None) -> str:
    if database_url is not None:
        return database_url
    if data_dir is not None:
        return f"sqlite:///{Path(data_dir) / 'stranger_club.db'}"
    return config.database_url


def _build_storage(config: AppConfig, uploads_dir: Path, prefix: str) -> Storage:
    if config.storage.backend == "s3":
        from .storage_s3 import S3Storage
        return S3Storage(config.storage, prefix=prefix)
    return LocalFilesystemStorage(uploads_dir / prefix)


def require_platform_admin(organizer: Organizer = Depends(require_admin)) -> Organizer:
    if organizer.role != PLATFORM_ADMIN:
        # 404, not 403: consistent with the rest of the app's ownership
        # checks — a non-platform-admin should not learn this endpoint
        # exists at all.
        raise api_error(404, "RESOURCE_NOT_FOUND", "Not found")
    return organizer


def create_app(
    data_dir: Path | None = None,
    database_url: str | None = None,
    frontend_dir: Path | None = None,
    admin_username: str | None = None,
    admin_password: str | None = None,
    otp_provider: OtpProvider | None = None,
    proof_storage: Storage | None = None,
    qr_storage: Storage | None = None,
) -> FastAPI:
    config = load_config()
    resolved_database_url = _resolve_database_url(config, data_dir, database_url)
    dialect = dialect_name(resolved_database_url)

    data_dir = Path(data_dir) if data_dir is not None else Path("data")
    uploads_dir = data_dir / "uploads"

    session_factory = make_session_factory(
        resolved_database_url, auto_migrate=config.auto_migrate,
        pool_size=config.db_pool_size, max_overflow=config.db_max_overflow,
    )
    username = admin_username or config.admin_username
    password = admin_password or config.admin_password

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with session_factory() as session:
            organizer = session.scalar(select(Organizer).where(Organizer.username == username))
            if not organizer:
                if not password:
                    raise RuntimeError(
                        "No organizer account exists and SC_ADMIN_PASSWORD is not set. "
                        "Set SC_ADMIN_PASSWORD (and optionally SC_ADMIN_USERNAME) before starting the app."
                    )
                organizer = Organizer(username=username, password_hash=PASSWORD_HASHER.hash(password))
                session.add(organizer)
                session.commit()
                logger.info("organizer_created username=%s", username)
            # The organizer must exist before seeding, so the seed event has a
            # real owner from the moment it's created.
            seed_database(session, owner_organizer_id=organizer.id)
            # Backfill any pre-existing event that predates ownership
            # tracking. Deliberately runs here (after the organizer above is
            # guaranteed to exist), not in the Alembic migration, which runs
            # earlier and may see zero organizers on a fresh/legacy database.
            backfill_missing_event_ownership(session)
        yield
        # Graceful shutdown: release pooled connections and stop the
        # realtime listener thread cleanly rather than letting process exit
        # tear them down.
        session_factory.kw["bind"].dispose()
        app.state.broadcaster.stop()

    app = FastAPI(title="Stranger Club API", version="3.0.0", lifespan=lifespan)
    app.add_middleware(CorrelationIdMiddleware)

    app.state.config = config
    app.state.session_factory = session_factory
    app.state.uploads_dir = uploads_dir
    app.state.proof_storage = proof_storage if proof_storage is not None else _build_storage(config, uploads_dir, "proofs")
    app.state.qr_storage = qr_storage if qr_storage is not None else _build_storage(config, uploads_dir, "qr")
    app.state.broadcaster = (
        PostgresBroadcaster(resolved_database_url) if dialect == "postgresql" else InProcessBroadcaster()
    )
    app.state.otp_provider = otp_provider or ConsoleOtpProvider()

    @app.exception_handler(HTTPException)
    async def http_error_handler(_: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "REQUEST_FAILED", "message": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content={"error": detail, "request_id": current_request_id()})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError):
        message = exc.errors()[0].get("msg", "Invalid request") if exc.errors() else "Invalid request"
        return JSONResponse(status_code=422, content={"error": {"code": "VALIDATION_ERROR", "message": message}, "request_id": current_request_id()})

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_: Request, exc: Exception):
        logger.exception("unexpected_error", exc_info=exc)
        return JSONResponse(status_code=500, content={"error": {"code": "INTERNAL_ERROR", "message": "Something went wrong. Please try again."}, "request_id": current_request_id()})

    @app.get("/health")
    def health(): return {"status": "ok"}

    @app.get("/ready")
    def ready(request: Request):
        try:
            with request.app.state.session_factory() as session:
                session.execute(select(Match.id).limit(1))
                if dialect == "postgresql":
                    current = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
                    if current != ALEMBIC_EXPECTED_HEAD:
                        logger.error("readiness_failed reason=alembic_head_mismatch expected=%s actual=%s", ALEMBIC_EXPECTED_HEAD, current)
                        raise api_error(503, "NOT_READY", "Database schema is not at the expected migration.")
        except HTTPException:
            raise
        except Exception:
            logger.error("readiness_failed reason=database_unavailable")
            raise api_error(503, "NOT_READY", "Database is unavailable")
        return {"status": "ready"}

    @app.get("/internal/diagnostics")
    def diagnostics(request: Request, _organizer: Organizer = Depends(require_platform_admin)):
        """PLATFORM_ADMIN-only. Safe operational data only — never
        connection strings, credentials, session tokens, or user/payment
        data. For manual troubleshooting, not a metrics-scrape endpoint."""
        engine = session_factory.kw["bind"]
        pool = engine.pool
        broadcaster = request.app.state.broadcaster
        subscribers = getattr(broadcaster, "subscribers", None)
        if subscribers is None:
            subscribers = getattr(broadcaster, "_subscribers", {})
        return {
            "database": {
                "dialect": engine.dialect.name,
                "pool_checked_out": pool.checkedout() if hasattr(pool, "checkedout") else None,
                "pool_size": pool.size() if hasattr(pool, "size") else None,
            },
            "realtime": {
                "backend": type(broadcaster).__name__,
                "active_event_topics": len(subscribers),
                "total_subscribers": getattr(broadcaster, "_total", None),
            },
            "storage": {
                "proof_backend": type(request.app.state.proof_storage).__name__,
                "qr_backend": type(request.app.state.qr_storage).__name__,
            },
        }

    app.include_router(auth.router)
    app.include_router(player_auth.router)
    app.include_router(events.router)
    app.include_router(registrations.router)
    app.include_router(payments.router)
    app.include_router(admin.router)
    app.include_router(teams.router)
    app.include_router(fixtures.router)

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
