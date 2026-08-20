"""baseline: hand off from dbmate to alembic

Intentionally empty. Everything through 20260721000001_messaging_schema.sql was applied by
dbmate, and those files stay the way a fresh database gets built. This revision is the marker
that says "Alembic owns the schema from here forward".

Why empty rather than a full CREATE of every table. Two reasons. The production database
already has the schema, so a revision that created it would have to be stamped past anyway.
And the fresh-database path still runs the dbmate applier first, so by the time
`alembic upgrade head` runs, the tables exist. An empty baseline is correct in both cases,
where a table-creating baseline would be wrong in both.

Revision ID: 20260729_0000_baseline
Revises:
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "20260729_0000_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
