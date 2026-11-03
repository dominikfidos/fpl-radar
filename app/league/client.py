"""HTTP client for the (unofficial) Fantasy Premier League API.

Responsibilities:
- one polite place that talks to the internet (User-Agent, delay between calls)
- retries with backoff on rate limiting (429) and server errors (5xx)
- optional cache (see cache.py) so repeated reports do not hit the API again
"""

from __future__ import annotations

import time
from typing import Any, Callable, Protocol

import httpx

BASE_URL = "https://fantasy.premierleague.com/api/"
USER_AGENT = "fpl-radar-portfolio/1.0"


class FplApiError(RuntimeError):
    """The FPL API could not give us what we asked for."""


class Cache(Protocol):
    def get(self, key: str, ttl: int) -> Any | None: ...

    def set(self, key: str, value: Any) -> None: ...


class FplClient:
    def __init__(
        self,
        cache: Cache | None = None,
        delay: float = 0.5,
        retries: int = 3,
        timeout: float = 20.0,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._cache = cache
        self._delay = delay  # pause before every real request: be polite
        self._retries = retries
        self._sleep = sleep
        self._http = http or httpx.Client(
            base_url=BASE_URL, timeout=timeout, headers={"User-Agent": USER_AGENT}
        )

    def close(self) -> None:
        self._http.close()

    # -- low level ---------------------------------------------------------
    def _get(self, path: str, ttl: int) -> Any:
        if self._cache is not None:
            hit = self._cache.get(path, ttl)
            if hit is not None:
                return hit
        data = self._fetch(path)
        if self._cache is not None:
            self._cache.set(path, data)
        return data

    def _fetch(self, path: str) -> Any:
        last_problem = "unknown"
        for attempt in range(self._retries + 1):
            self._sleep(self._delay)
            try:
                response = self._http.get(path)
            except httpx.TransportError as exc:
                last_problem = f"network error: {exc}"
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise FplApiError(f"{path}: response is not valid JSON") from exc
                if response.status_code == 404:
                    raise FplApiError(f"{path}: not found (404)")
                if response.status_code == 429 or response.status_code >= 500:
                    last_problem = f"HTTP {response.status_code}"
                    retry_after = response.headers.get("Retry-After", "")
                    if retry_after.isdigit():
                        self._sleep(min(int(retry_after), 60))
                else:
                    raise FplApiError(f"{path}: HTTP {response.status_code}")
            self._sleep(2**attempt)  # exponential backoff: 1s, 2s, 4s ...
        raise FplApiError(
            f"{path}: giving up after {self._retries + 1} attempts ({last_problem})"
        )

    # -- endpoints we use --------------------------------------------------
    def bootstrap(self) -> dict:
        """All players, teams and gameweeks (one big JSON)."""
        return self._get("bootstrap-static/", ttl=900)

    def fixtures(self) -> list[dict]:
        return self._get("fixtures/?future=1", ttl=900)

    def league_standings(self, league_id: int, max_pages: int = 10) -> list[dict]:
        """All rows of a classic mini-league (the API returns 50 per page)."""
        rows: list[dict] = []
        for page in range(1, max_pages + 1):
            data = self._get(
                f"leagues-classic/{league_id}/standings/?page_standings={page}", ttl=300
            )
            block = data.get("standings", {})
            rows.extend(block.get("results", []))
            if not block.get("has_next"):
                break
        return rows

    def entry_picks(self, entry_id: int, event: int, ttl: int = 900) -> dict:
        """A manager's squad for one gameweek.

        FPL hides squads until that gameweek's deadline has passed.
        """
        return self._get(f"entry/{entry_id}/event/{event}/picks/", ttl=ttl)
