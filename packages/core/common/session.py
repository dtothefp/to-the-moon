"""The SQLAlchemy engine and session factory, alongside the existing psycopg path.

This does not replace `common.db`. That module stays the one place DATABASE_URL is read, and
every existing psycopg call site keeps working exactly as it did. What this adds is a second,
higher-level door into the same database for new code.

The driver URL. DATABASE_URL is stored in libpq form (`postgresql://...`) because dbmate, psql,
and psycopg all want it that way. SQLAlchemy needs to be told which DBAPI to use, so
`sqlalchemy_url()` rewrites the scheme to `postgresql+psycopg://` (psycopg 3, already a
dependency; the bare `postgresql://` scheme would send SQLAlchemy looking for psycopg2).

Pooling. pool_pre_ping is on because Supabase's pooler and Railway both drop idle connections,
and a stale pooled connection surfaces as a confusing OperationalError on the next query
rather than at checkout. The cost is one cheap round trip per checkout, which is the right
trade for a background worker that can sit idle between runs.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from common.db import DATABASE_URL

# Small by default: the API and the worker each hold their own pool, and Supabase's
# transaction pooler counts every one of them. Override per service when a real
# concurrency number is known.
POOL_SIZE = int(os.environ.get("SQLALCHEMY_POOL_SIZE", "5"))
MAX_OVERFLOW = int(os.environ.get("SQLALCHEMY_MAX_OVERFLOW", "5"))

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def sqlalchemy_url(url: str | None = None) -> str:
    """Rewrite a libpq URL to the psycopg 3 SQLAlchemy dialect. Idempotent."""
    raw = url or DATABASE_URL
    if raw.startswith("postgresql+"):
        return raw
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg://" + raw[len("postgresql://") :]
    if raw.startswith("postgres://"):
        return "postgresql+psycopg://" + raw[len("postgres://") :]
    return raw


def get_engine() -> Engine:
    """Process-wide engine, created on first use. One pool per process, not per call."""
    global _engine
    if _engine is None:
        _engine = create_engine(
            sqlalchemy_url(),
            pool_pre_ping=True,
            pool_size=POOL_SIZE,
            max_overflow=MAX_OVERFLOW,
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transaction boundary for a unit of work: commit on clean exit, roll back on any raise.

    expire_on_commit is off on the factory so objects stay readable after the commit, which
    is what a Celery task wants when it returns a summary of what it just wrote.
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def db_session() -> Iterator[Session]:
    """FastAPI dependency. Use as `Depends(db_session)`; FastAPI closes it per request."""
    with session_scope() as session:
        yield session
