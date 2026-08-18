from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base
from .migrations import upgrade


def make_session_factory(database_path: Path) -> sessionmaker[Session]:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        upgrade(connection)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
