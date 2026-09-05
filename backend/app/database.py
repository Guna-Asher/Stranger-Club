from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from .models import Base
from .migrations import upgrade as sqlite_legacy_upgrade

PROJECT_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger("stranger_club")

# Arbitrary fixed constant identifying this application's migration lock.
# Postgres advisory locks are keyed globally by this integer across every
# session in the database — two processes calling pg_advisory_lock with the
# same key genuinely serialize against each other, and the lock is released
# automatically if the holding connection drops (crash-safe: a killed
# migration process never leaves the lock stuck).
POSTGRES_MIGRATION_ADVISORY_LOCK_KEY = 875_219_003


def dialect_name(database_url: str) -> str:
    return make_url(database_url).get_backend_name()


def _run_alembic_migrations(database_url: str, *, fresh: bool) -> None:
    """PostgreSQL: always the real `alembic upgrade head` — never create_all(),
    never a stamp-only shortcut. A genuinely fresh PostgreSQL database is
    bootstrapped entirely by migration 0000 (see its docstring); every
    revision after that is a no-op against what 0000 just built. Guarded by a
    session-scoped Postgres advisory lock so concurrent callers (multiple
    instances, or a dev docker-compose + a CI job hitting the same database)
    never race to apply migrations simultaneously.

    SQLite: unchanged from before Phase 3 — a freshly created database was
    just built by Base.metadata.create_all() + the frozen hand-rolled
    migrations.py, so it is stamped straight to head; an existing database
    runs the real upgrade path.
    """
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)

    if dialect_name(database_url) == "postgresql":
        lock_engine = create_engine(database_url)
        try:
            with lock_engine.connect() as connection:
                connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": POSTGRES_MIGRATION_ADVISORY_LOCK_KEY})
                try:
                    logger.info("migration_lock_acquired key=%s", POSTGRES_MIGRATION_ADVISORY_LOCK_KEY)
                    command.upgrade(config, "head")
                finally:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": POSTGRES_MIGRATION_ADVISORY_LOCK_KEY})
                    logger.info("migration_lock_released key=%s", POSTGRES_MIGRATION_ADVISORY_LOCK_KEY)
        finally:
            lock_engine.dispose()
    elif fresh:
        command.stamp(config, "head")
    else:
        command.upgrade(config, "head")


def _make_sqlite_engine(database_url: str):
    return create_engine(database_url, connect_args={"check_same_thread": False})


def _make_postgres_engine(database_url: str, *, pool_size: int, max_overflow: int):
    return create_engine(
        database_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=10,       # fail fast rather than queue indefinitely under saturation
        pool_recycle=1800,     # survive a managed provider silently dropping idle connections
        pool_pre_ping=True,    # convert a dead pooled connection into a clean retry, not a mystery failure
        connect_args={"options": "-c statement_timeout=15000"},  # 15s: no single query holds a lock forever
    )


def run_migrations_to_head(database_url: str) -> None:
    """Public entry point for the explicit deployment migration step (see
    backend/app/migrate.py). Always the real upgrade path — never the
    fresh-database stamp shortcut, which only makes sense for the SQLite
    dev/test bootstrap in make_session_factory."""
    _run_alembic_migrations(database_url, fresh=False)


def make_session_factory(
    database_url: str, *, auto_migrate: bool = True, pool_size: int = 5, max_overflow: int = 5,
) -> sessionmaker[Session]:
    dialect = dialect_name(database_url)

    if dialect == "sqlite":
        database_path = Path(make_url(database_url).database)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        is_fresh = not database_path.exists()
        engine = _make_sqlite_engine(database_url)
        # Dev/test-only bootstrap: create_all() + the frozen hand-rolled
        # migrations.py, exactly as before Phase 3. PostgreSQL never takes
        # this path — see _run_alembic_migrations.
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            sqlite_legacy_upgrade(connection)
        engine.dispose()  # Alembic opens its own connection to the same file next
        if auto_migrate:
            _run_alembic_migrations(database_url, fresh=is_fresh)
        engine = _make_sqlite_engine(database_url)
        return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    if dialect == "postgresql":
        if auto_migrate:
            _run_alembic_migrations(database_url, fresh=False)
        engine = _make_postgres_engine(database_url, pool_size=pool_size, max_overflow=max_overflow)
        return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    raise ValueError(f"Unsupported database dialect: {dialect!r} (expected sqlite or postgresql)")
