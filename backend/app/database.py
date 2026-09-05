from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base
from .migrations import upgrade

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run_alembic_migrations(database_path: Path, *, fresh: bool) -> None:
    """Alembic is the authoritative migration mechanism from its baseline
    (0001) forward; backend/app/migrations.py remains frozen, historical
    bootstrap logic for databases that predate it.

    A freshly created database was just built by Base.metadata.create_all()
    from the current models — i.e. it already IS the schema every Alembic
    revision would produce — so it's stamped straight to head rather than
    replaying history against itself. An existing database runs the real
    upgrade path.
    """
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    if fresh:
        command.stamp(config, "head")
    else:
        command.upgrade(config, "head")


def make_session_factory(database_path: Path) -> sessionmaker[Session]:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    is_fresh = not database_path.exists()
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        upgrade(connection)
    engine.dispose()  # Alembic opens its own connection to the same file next
    _run_alembic_migrations(database_path, fresh=is_fresh)
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
