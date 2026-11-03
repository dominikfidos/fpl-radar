"""A very small in-memory rate limiter for the public report endpoint.

Why: every report makes about 8-25 requests to the (unofficial) FPL API.
Without a limit one visitor could hammer it through us, and FPL could block
our server for everybody.

Limits: it counts per process. With several replicas (Kubernetes) each pod
keeps its own counters, so the real limit is "per pod". That is good enough
here; a shared limiter would need Redis.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import HTTPException, Request

from app.metrics import RATE_LIMITED


class RateLimiter:
    def __init__(
        self,
        max_calls: int,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_calls = max_calls
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def retry_after(self, key: str) -> float:
        """0 if the call is allowed (and counted), else seconds until a slot frees up."""
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.max_calls:
            return self.window - (now - hits[0])
        hits.append(now)
        if len(self._hits) > 10_000:  # keep memory bounded
            self._prune(now)
        return 0.0

    def _prune(self, now: float) -> None:
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] >= self.window]:
            del self._hits[key]


def client_ip(request: Request) -> str:
    """Caller address. Behind a proxy the LAST X-Forwarded-For entry is the one our
    own proxy added (earlier entries can be forged by the caller)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


_limiters: dict[int, RateLimiter] = {}


def rate_limit(request: Request) -> None:
    """FastAPI dependency. RATE_LIMIT_PER_MINUTE=0 switches the limit off."""
    raw = os.getenv("RATE_LIMIT_PER_MINUTE", "15").strip()
    max_calls = int(raw) if raw.isdigit() else 15
    if max_calls <= 0:
        return
    limiter = _limiters.setdefault(max_calls, RateLimiter(max_calls))
    wait = limiter.retry_after(client_ip(request))
    if wait > 0:
        RATE_LIMITED.inc()
        raise HTTPException(
            status_code=429,
            detail="Too many requests - please wait a moment and try again.",
            headers={"Retry-After": str(int(wait) + 1)},
        )
