"""SQLAlchemy models for every table in the database.

Why this exists. Until now the repo spoke raw SQL through psycopg everywhere, and the schema
lived only in `db/migrations/*.sql`. That was the right call while the schema was one drill's
worth of tables and the point was to see the SQL. It stops paying once the repo hosts more
than one workload: the jobs index and the content-hub pipeline both want their own tables,
and hand-writing every DDL, every INSERT, and every SELECT for each of them is where the
bugs and the drift live.

These models are the declarative mirror of the schema that dbmate already built. They serve
three jobs at once:

  * Alembic autogenerate diffs this metadata against the live database, so new tables are
    written once (here) rather than twice (here and in SQL).
  * New query code can use the ORM or Core select() with real column objects instead of
    string SQL.
  * The existing psycopg call paths keep working untouched. Nothing here forces a rewrite.

What is deliberately NOT modelled: the monthly partitions of raw_signals
(`raw_signals_2026_07` and friends), the `daily_signal_rollup` materialized view, and
dbmate's own `schema_migrations` bookkeeping table. Alembic's env.py filters all three out
of autogenerate, because none of them is something a model can express and each would
otherwise show up as a spurious DROP in every generated revision.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, REAL, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """One MetaData for the whole database. Alembic's target_metadata points at this."""


TS = DateTime(timezone=True)


# ---------------------------------------------------------------------------
# Influencer / signal pipeline (workload 1)
# ---------------------------------------------------------------------------


class Influencer(Base):
    """A scrape target. Renamed from `competitors` in the Module 2 migration, which is why
    the constraint names in the database still read `competitors_pkey`."""

    __tablename__ = "influencers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    instagram_handle: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    # The incremental-scrape watermark. NULL means never scraped, which the scraper reads as
    # "pull the newest post only" rather than "pull everything".
    last_scraped_at: Mapped[dt.datetime | None] = mapped_column(TS)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    influencer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("influencers.id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class RawSignal(Base):
    """Append-only scraped items, RANGE partitioned by captured_at.

    Two things here are load-bearing and easy to break. The partition key has to appear in
    every unique constraint (Postgres enforces uniqueness per partition, so it cannot police
    a key that does not include the partition column), which is why captured_at is in both
    the primary key and the dedup constraint. And `postgresql_partition_by` is what makes a
    CREATE TABLE emitted from this model come out partitioned rather than plain, which
    matters the day someone builds a fresh database from metadata instead of from dbmate.

    caption_tsv is a Postgres GENERATED column. SQLAlchemy is told it exists so autogenerate
    does not try to drop it, but nothing ever writes to it.
    """

    __tablename__ = "raw_signals"
    __table_args__ = (
        PrimaryKeyConstraint("id", "captured_at"),
        # The database calls this one competitors_content_hash_captured_at_key, a leftover
        # from the pre-rename migration. Naming it here keeps autogenerate from proposing a
        # drop-and-recreate under SQLAlchemy's default name.
        UniqueConstraint(
            "influencer_id",
            "content_hash",
            "captured_at",
            name="raw_signals_competitor_id_content_hash_captured_at_key",
        ),
        Index("raw_signals_caption_tsv_gin", "caption_tsv", postgresql_using="gin"),
        {"postgresql_partition_by": "RANGE (captured_at)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, autoincrement=True)
    influencer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_id: Mapped[int | None] = mapped_column(BigInteger)
    captured_at: Mapped[dt.datetime] = mapped_column(TS, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Module 6's lexical half. Postgres maintains it, nothing in Python ever assigns to it.
    caption_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english'::regconfig, COALESCE((payload ->> 'caption'::text), ''::text))", persisted=True),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    influencer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("influencers.id"), nullable=False)
    raw_signal_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(TS, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class Embedding(Base):
    """The original Module 1 embeddings table, keyed by (ref_kind, ref_id).

    Module 6 added `signal_embeddings` below, which is keyed by content_hash and is what the
    search path actually reads. This table is still in the schema and still modelled, but new
    work should use SignalEmbedding.
    """

    __tablename__ = "embeddings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    influencer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ref_kind: Mapped[str] = mapped_column(Text, nullable=False)
    ref_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))
    created_at: Mapped[dt.datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class Digest(Base):
    """One Module 5 digest-bot run. `session_id` is the Managed Agent session that produced
    it, so a finished digest can be traced back to the agent transcript that wrote it."""

    __tablename__ = "digests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="digests_status_check",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    session_id: Mapped[str | None] = mapped_column(Text)
    content_md: Mapped[str | None] = mapped_column(Text)
    word_count: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    completed_at: Mapped[dt.datetime | None] = mapped_column(TS)


class Run(Base):
    """The durable job-of-record for one pipeline run.

    This row survives a page refresh, a worker crash, and a Redis flush, which the
    high-frequency progress in Redis does not. Status transitions are written here; per-tick
    progress is not.
    """

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'rating', 'completed', 'failed')",
            name="runs_status_check",
        ),
        CheckConstraint("mode IN ('live', 'demo')", name="runs_mode_check"),
        # Partial and descending: the dashboard only ever asks for the newest in-flight runs,
        # so the index covers that query and skips every finished row.
        Index(
            "runs_active_idx",
            text("created_at DESC"),
            postgresql_where=text("status IN ('queued', 'running', 'rating')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="live")
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    done_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    inserted: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rated_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Which rating model this run used ("provider/model"), NULL when the rating layer is
    # inert. It lives on the run because the model is data, not deployment config.
    model: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    started_at: Mapped[dt.datetime | None] = mapped_column(TS)
    finished_at: Mapped[dt.datetime | None] = mapped_column(TS)


class SignalRating(Base):
    """One LLM rating per signal, keyed by content_hash rather than by row id.

    Keying on the hash is what makes re-rating idempotent across reprocessing: the same
    caption scraped twice produces the same hash and therefore the same rating row.
    """

    __tablename__ = "signal_ratings"
    __table_args__ = (
        CheckConstraint("relevance BETWEEN 0 AND 1", name="signal_ratings_relevance_check"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="signal_ratings_confidence_check"),
    )

    content_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    # REAL, not Float: Float renders as DOUBLE PRECISION and the columns are single precision.
    relevance: Mapped[float] = mapped_column(REAL, nullable=False)
    confidence: Mapped[float] = mapped_column(REAL, nullable=False)
    topics: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    rated_at: Mapped[dt.datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class SignalEmbedding(Base):
    """Module 6's vector table. 1536 dims because that is text-embedding-3-small's width,
    and the HNSW index is built for cosine distance."""

    __tablename__ = "signal_embeddings"
    __table_args__ = (
        Index(
            "signal_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    content_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    embedded_at: Mapped[dt.datetime] = mapped_column(TS, nullable=False, server_default=func.now())


# ---------------------------------------------------------------------------
# Messaging drill
# ---------------------------------------------------------------------------


class MsgUser(Base):
    __tablename__ = "msg_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    external_id: Mapped[str | None] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class MsgConversation(Base):
    __tablename__ = "msg_conversations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class MsgParticipant(Base):
    """A DM is a conversation with two participants, a group is one with N. Groups get no
    table of their own, which is the shape Garett McCann endorsed in the 2026-07-13 round."""

    __tablename__ = "msg_participants"
    __table_args__ = (Index("msg_participants_user_idx", "user_id"),)

    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("msg_conversations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("msg_users.id", ondelete="CASCADE"), primary_key=True)
    joined_at: Mapped[dt.datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    last_read_message_id: Mapped[int | None] = mapped_column(BigInteger)


class MsgMessage(Base):
    __tablename__ = "msg_messages"
    __table_args__ = (
        # The idempotency key. A client retrying a send reuses client_msg_id, the second
        # INSERT conflicts, and the API returns the original row instead of a duplicate.
        Index("msg_messages_idem_idx", "sender_id", "client_msg_id", unique=True),
        # Cursor pagination rides this. Ordered by id, not created_at (a sequence is
        # monotonic within one database, wall-clock time is not), and DESC because a
        # conversation always opens on its newest page.
        Index("msg_messages_conv_id_idx", "conversation_id", text("id DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("msg_conversations.id", ondelete="CASCADE"), nullable=False
    )
    sender_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("msg_users.id"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    client_msg_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TS, nullable=False, server_default=func.now())
