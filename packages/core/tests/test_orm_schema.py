"""Do the SQLAlchemy models actually match the database dbmate built?

The offline test needs no database and catches the cheap mistakes: a model that forgot its
primary key, a Vector column with the wrong width, a table name typo.

The integration test is the one that matters. It reflects the live schema and asserts that
every modelled table exists with the same columns, and, more importantly, that Alembic's
autogenerate finds NOTHING to do. A non-empty diff means the models and the database have
drifted, which is exactly the failure this whole layer exists to prevent. It is marked
integration, so it skips when no database is reachable.
"""

from __future__ import annotations

import pytest
from common.models import Base, RawSignal, Run, SignalEmbedding
from common.session import sqlalchemy_url
from sqlalchemy import inspect


def test_url_rewrites_to_psycopg_dialect() -> None:
    assert sqlalchemy_url("postgresql://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert sqlalchemy_url("postgres://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    # already-qualified URLs pass through untouched
    assert sqlalchemy_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


def test_every_model_has_a_primary_key() -> None:
    for table in Base.metadata.tables.values():
        assert table.primary_key.columns, f"{table.name} has no primary key"


def test_raw_signals_keeps_the_partition_key_in_its_unique_constraints() -> None:
    """Postgres cannot enforce a unique constraint that omits the partition column, so if
    captured_at ever falls out of one of these, the table stops being creatable."""
    assert "captured_at" in RawSignal.__table__.primary_key.columns
    uniques = [c for c in RawSignal.__table__.constraints if c.__class__.__name__ == "UniqueConstraint"]
    assert uniques, "raw_signals lost its dedup constraint"
    for c in uniques:
        assert "captured_at" in c.columns

    assert RawSignal.__table__.dialect_options["postgresql"]["partition_by"] == "RANGE (captured_at)"


def test_vector_width_matches_the_embedding_model() -> None:
    """1536 is text-embedding-3-small. Changing it is a migration, not an edit."""
    assert SignalEmbedding.__table__.c.embedding.type.dim == 1536


def test_runs_status_check_covers_the_states_the_worker_writes() -> None:
    checks = [c for c in Run.__table__.constraints if c.__class__.__name__ == "CheckConstraint"]
    status = next(c for c in checks if c.name == "runs_status_check")
    for state in ("queued", "running", "rating", "completed", "failed"):
        assert state in str(status.sqltext)


@pytest.mark.integration
def test_models_match_the_live_schema() -> None:
    from common.session import get_engine
    from sqlalchemy.exc import OperationalError

    engine = get_engine()
    try:
        with engine.connect() as conn:
            live = set(inspect(conn).get_table_names())
    except OperationalError:
        pytest.skip("no database reachable")

    for table in Base.metadata.tables.values():
        assert table.name in live, f"model {table.name} has no table in the database"


@pytest.mark.integration
def test_autogenerate_finds_no_drift() -> None:
    """The real assertion: `alembic revision --autogenerate` on a clean checkout should
    produce an empty revision. Anything else means models and schema disagree."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from common.schema_filter import include_object
    from common.session import get_engine
    from sqlalchemy.exc import OperationalError

    engine = get_engine()
    try:
        conn = engine.connect()
    except OperationalError:
        pytest.skip("no database reachable")

    with conn:
        ctx = MigrationContext.configure(
            conn,
            opts={"include_object": include_object, "compare_type": True},
        )
        diff = compare_metadata(ctx, Base.metadata)

    assert diff == [], f"models drifted from the database: {diff}"
