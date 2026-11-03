import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.database import SessionLocal
from app.league.cache import DbCache
from app.league.client import FplApiError, FplClient
from app.league.service import ReportError, build_report
from app.ratelimit import rate_limit

router = APIRouter(prefix="/api/league", tags=["league"])


def get_fpl_client():
    """One client per request, closed afterwards (tests replace this)."""
    client = FplClient(cache=DbCache(SessionLocal))
    try:
        yield client
    finally:
        client.close()


def _env_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value.isdigit() else None


@router.get("/report", dependencies=[Depends(rate_limit)])
def league_report(
    league_id: int | None = Query(None, description="Defaults to env FPL_LEAGUE_ID"),
    entry_id: int | None = Query(None, description="Your team id, defaults to env FPL_ENTRY_ID"),
    rivals: int = Query(5, ge=1, le=20),
    mode: Literal["top", "above"] = "top",
    horizon: int = Query(3, ge=1, le=5),
    top: int = Query(10, ge=1, le=30),
    uniqueness: float = Query(0.5, ge=0, le=1),
    client: FplClient = Depends(get_fpl_client),
):
    """Differential suggestions based on the squads of the managers above you."""
    league_id = league_id or _env_int("FPL_LEAGUE_ID")
    entry_id = entry_id or _env_int("FPL_ENTRY_ID")
    if league_id is None or entry_id is None:
        raise HTTPException(
            status_code=422,
            detail="Provide league_id and entry_id (or set FPL_LEAGUE_ID and FPL_ENTRY_ID).",
        )
    try:
        return build_report(
            client,
            league_id,
            entry_id,
            rivals=rivals,
            mode=mode,
            horizon=horizon,
            top=top,
            uniqueness=uniqueness,
        )
    except ReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FplApiError as exc:
        raise HTTPException(status_code=502, detail=f"FPL API problem: {exc}") from exc
