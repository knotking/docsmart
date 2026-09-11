"""Layer S (substrate): engine and session management.

One code path for SQLite (local) and Postgres/Cloud SQL (GCP) - the difference is the URL
(constraint 9). SQLite needs two specific pragmas to behave under FastAPI's threadpool;
Postgres needs neither, so they are applied conditionally.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from termguard.config import Settings, get_settings

_engine: Engine | None = None
_engine_url: str | None = None


def get_engine(settings: Settings | None = None) -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine, _engine_url
    settings = settings or get_settings()
    if _engine is not None and _engine_url == settings.db_url:
        return _engine

    kwargs: dict = {"echo": False}
    if settings.is_sqlite:
        # check_same_thread=False: FastAPI runs sync endpoints in a threadpool.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:  # pragma: no cover - exercised in GCP deployments
        # Cloud SQL connections go stale behind the proxy; recycle and pre-ping.
        kwargs.update(pool_pre_ping=True, pool_recycle=1800)

    engine = create_engine(settings.db_url, **kwargs)

    if settings.is_sqlite:
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")     # concurrent reads during a run
            cur.execute("PRAGMA foreign_keys=ON")      # the version chain must not dangle
            cur.close()

    _engine, _engine_url = engine, settings.db_url
    return engine


def init_db(settings: Settings | None = None) -> Engine:
    """Create tables if absent. Safe to call repeatedly."""
    import termguard.models  # noqa: F401  (registers tables on SQLModel.metadata)

    engine = get_engine(settings)
    SQLModel.metadata.create_all(engine)
    return engine


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """Transactional session: commit on success, roll back on error."""
    with Session(get_engine(settings)) as session:
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with Session(get_engine()) as session:
        yield session


def reset_engine() -> None:
    """Drop the cached engine. Tests use this when pointing at a fresh database."""
    global _engine, _engine_url
    if _engine is not None:
        _engine.dispose()
    _engine, _engine_url = None, None
