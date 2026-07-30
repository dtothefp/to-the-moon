"""Which database objects Alembic autogenerate is allowed to see.

This lives in `common` rather than in alembic's env.py because env.py runs migrations as a
side effect of being imported, so nothing else can import from it. The drift test needs the
same filter the real autogenerate uses, and a second copy of the rules would eventually
disagree with the first.

Three kinds of object exist in the database but must never appear in a generated revision:

  * monthly partitions of raw_signals (`raw_signals_2026_07` and friends), created by a DO
    block in a dbmate migration and by partition maintenance, never by a model.
  * `daily_signal_rollup`, a materialized view, which reflects as a table with no model.
  * the two migration bookkeeping tables.

Without this filter, the first autogenerate run would confidently propose dropping the
partitioned data and the rollup.
"""

from __future__ import annotations

from typing import Any

PARTITION_PREFIXES = ("raw_signals_",)
SKIP_TABLES = frozenset({"schema_migrations", "alembic_version", "daily_signal_rollup"})


def include_object(obj: Any, name: str, type_: str, reflected: bool, compare_to: Any) -> bool:
    if type_ == "table":
        if name in SKIP_TABLES:
            return False
        if any(name.startswith(p) for p in PARTITION_PREFIXES):
            return False
    return True
