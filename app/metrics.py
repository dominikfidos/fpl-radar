"""Prometheus metrics: numbers that tell an operator how the service is doing.

Prometheus visits GET /metrics every few seconds, stores the numbers, and
Grafana draws them. The four questions we want to answer at a glance:
  - Is it up and how busy is it?            -> http_requests_total
  - Is it slow?                              -> http_request_duration_seconds
  - Is something failing?                    -> status="5xx" in http_requests_total, fpl_api_requests_total{outcome="error"}
  - Are visitors being throttled?            -> rate_limit_rejections_total
"""

from __future__ import annotations

import hmac
import os
import time

from fastapi import APIRouter, Header, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "path", "status"]
)
# A report can take 10-20 s on a cold cache, so the buckets go up to 60 s.
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "Time to answer a request",
    ["method", "path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60),
)
FPL_REQUESTS = Counter(
    "fpl_api_requests_total",
    "Data lookups for the FPL API: served from cache, fetched fine, or failed",
    ["outcome"],  # cache_hit | ok | error
)
RATE_LIMITED = Counter(
    "rate_limit_rejections_total", "Requests answered with 429 by the rate limiter"
)

router = APIRouter()


async def track_requests(request: Request, call_next):
    """Middleware: count and time every request.

    The label is the route TEMPLATE ("/api/players/{player_id}/history"), never the
    real URL, otherwise every player id would create a new time series.
    """
    if request.url.path == "/metrics":
        return await call_next(request)
    started = time.perf_counter()
    status = 500  # if call_next raises, this is what we record
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        path = route.path if route is not None else "unmatched"
        HTTP_REQUESTS.labels(request.method, path, str(status)).inc()
        HTTP_LATENCY.labels(request.method, path).observe(time.perf_counter() - started)


@router.get("/metrics", include_in_schema=False)
def metrics(authorization: str | None = Header(None)):
    """Optional protection: set METRICS_TOKEN and Prometheus must send it as a Bearer token."""
    token = os.getenv("METRICS_TOKEN", "").strip()
    if token and not hmac.compare_digest(authorization or "", f"Bearer {token}"):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
