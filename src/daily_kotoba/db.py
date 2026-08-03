"""Engine/session setup, SQLite pragmas, and first-boot seed copy."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from daily_kotoba.config import Settings
from daily_kotoba.models import Base

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def ensure_db_ready(settings: Settings) -> None:
    """Copy the baked seed DB into place on first boot, then create any missing tables."""
    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        seed_path = Path(settings.seed_db)
        if seed_path.exists():
            logger.info("copying seed DB %s -> %s", seed_path, db_path)
            shutil.copy2(seed_path, db_path)
        else:
            logger.warning("no seed DB at %s; starting with an empty database", seed_path)

    engine = get_engine(settings)
    Base.metadata.create_all(engine)


def get_engine(settings: Settings) -> Engine:
    global _engine
    if _engine is None:
        # check_same_thread=False + a roomy pool: many short-lived requests (each with
        # its own Session) can land on different worker threads; WAL + busy_timeout
        # (see the pragma listener above) is what actually serializes writers safely.
        _engine = create_engine(
            f"sqlite:///{settings.db_path}",
            connect_args={"check_same_thread": False},
            pool_size=20,
            max_overflow=20,
        )
    return _engine


def get_sessionmaker(settings: Settings) -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(settings), expire_on_commit=False)
    return _SessionLocal


def reset_engine_cache() -> None:
    """Test helper: drop cached engine/sessionmaker so a new Settings takes effect."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def session_scope(settings: Settings) -> Iterator[Session]:
    session_factory = get_sessionmaker(settings)
    with session_factory() as session:
        yield session
