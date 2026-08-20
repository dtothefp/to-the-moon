"""The single deploy-time schema entrypoint: dbmate history first, then Alembic.

Railway's preDeployCommand runs this, so a deploy never ships code ahead of its schema.

Order matters and is not arbitrary. The dbmate applier owns the frozen historical migrations
(everything through the messaging schema) and is still what builds a database from empty.
Alembic owns every change from its baseline forward. Running dbmate first means both the
existing production database and a brand-new one arrive at the same place: on the existing
one dbmate reports "up to date" and Alembic applies whatever is new, on a fresh one dbmate
builds the world and Alembic's empty baseline is a no-op.

Both steps are idempotent, so re-running this is always safe.
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

DB_DIR = Path(__file__).resolve().parent


def run_alembic_upgrade() -> None:
    # The ini resolves script_location through %(here)s, so this works from any cwd, which
    # matters because Railway's preDeployCommand runs from the repo root.
    command.upgrade(Config(str(DB_DIR / "alembic.ini")), "head")


def main() -> int:
    # db/ ships as files, not as an installed module (the wheel only packages `common`), so
    # the sibling applier is imported by path rather than by package name.
    sys.path.insert(0, str(DB_DIR))
    from migrate import main as dbmate_main

    rc = dbmate_main()
    if rc != 0:
        print("upgrade: dbmate step failed, not running alembic", file=sys.stderr)
        return rc

    print("upgrade: running alembic upgrade head")
    run_alembic_upgrade()
    print("upgrade: schema is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
