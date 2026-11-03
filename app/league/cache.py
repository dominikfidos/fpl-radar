"""A tiny cache for FPL API responses, stored in the normal database.

Why not Redis? For a few dozen requests per gameweek the database we already
run is enough, and it is one component less to operate. Redis would be a
reasonable later upgrade (see the README, "Next steps").
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models import ApiCache


class DbCache:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def get(self, key: str, ttl: int) -> Any | None:
        """Cached value if it is younger than `ttl` seconds, otherwise None."""
        with self._session_factory() as db:
            row = db.get(ApiCache, key)
            if row is None:
                return None
            fetched_at = row.fetched_at
            if fetched_at.tzinfo is None:  # SQLite drops the timezone
                fetched_at = fetched_at.replace(tzinfo=UTC)
            if datetime.now(UTC) - fetched_at >= timedelta(seconds=ttl):
                return None
            return row.payload

    def set(self, key: str, value: Any) -> None:
        with self._session_factory() as db:
            db.merge(ApiCache(key=key, fetched_at=datetime.now(UTC), payload=value))
            db.commit()
