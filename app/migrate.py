"""Bring the database schema up to date by running the Alembic migrations.

The API, the collector and the league refresh job all call this on start, so any of them can be
the first to touch a fresh database. Set SKIP_MIGRATIONS=1 to turn it off (the tests do that and
create their own tables).
You can also run it by hand:  alembic upgrade head
"""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent  # the folder with alembic.ini and migrations/


def upgrade(url: str | None = None) -> None:
    """Apply every migration that is not applied yet (does nothing when up to date)."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.attributes["embedded"] = True  # keep the application's logging settings
    if url:
        config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


def migrate_on_startup() -> None:
    if os.getenv("SKIP_MIGRATIONS") == "1":
        return
    upgrade()
