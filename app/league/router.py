import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.database import SessionLocal
from app.league.cache import DbCache
from app.league.client import FplApiError, FplClient
from app.league.history import build_history
from app.league.service import ReportError, build_backtest, build_live, build_report, build_rivals
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


@router.get("/live", dependencies=[Depends(rate_limit)])
def league_live(
    league_id: int | None = Query(None, description="Defaults to env FPL_LEAGUE_ID"),
    entry_id: int | None = Query(None, description="Your team id, defaults to env FPL_ENTRY_ID"),
    limit: int = Query(20, ge=1, le=50, description="How many top managers to show"),
    client: FplClient = Depends(get_fpl_client),
):
    """Live table of the running gameweek (points of players x picks)."""
    league_id = league_id or _env_int("FPL_LEAGUE_ID")
    entry_id = entry_id or _env_int("FPL_ENTRY_ID")
    if league_id is None or entry_id is None:
        raise HTTPException(
            status_code=422,
            detail="Provide league_id and entry_id (or set FPL_LEAGUE_ID and FPL_ENTRY_ID).",
        )
    try:
        return build_live(client, league_id, entry_id, limit=limit)
    except ReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FplApiError as exc:
        raise HTTPException(status_code=502, detail=f"FPL API problem: {exc}") from exc


@router.get("/backtest", dependencies=[Depends(rate_limit)])
def league_backtest(client: FplClient = Depends(get_fpl_client)):
    """How well the expected-points model predicted the gameweeks already played (no ids needed)."""
    try:
        return build_backtest(client)
    except ReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FplApiError as exc:
        raise HTTPException(status_code=502, detail=f"FPL API problem: {exc}") from exc


@router.get("/history", dependencies=[Depends(rate_limit)])
def league_history(
    league_id: int | None = Query(None, description="Defaults to env FPL_LEAGUE_ID"),
    entry_id: int | None = Query(None, description="Your team id, defaults to env FPL_ENTRY_ID"),
    show: int = Query(10, ge=1, le=30, description="How many managers to show (best placed now, plus you)"),
    client: FplClient = Depends(get_fpl_client),
):
    """League rank after every finished gameweek (rebuilt from public data and stored in the database)."""
    league_id = league_id or _env_int("FPL_LEAGUE_ID")
    entry_id = entry_id or _env_int("FPL_ENTRY_ID")
    if league_id is None or entry_id is None:
        raise HTTPException(
            status_code=422,
            detail="Provide league_id and entry_id (or set FPL_LEAGUE_ID and FPL_ENTRY_ID).",
        )
    try:
        return build_history(SessionLocal, client, league_id, entry_id, show=show)
    except ReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FplApiError as exc:
        raise HTTPException(status_code=502, detail=f"FPL API problem: {exc}") from exc


@router.get("/rivals", dependencies=[Depends(rate_limit)])
def league_rivals(
    league_id: int | None = Query(None, description="Defaults to env FPL_LEAGUE_ID"),
    entry_id: int | None = Query(None, description="Your team id, defaults to env FPL_ENTRY_ID"),
    rivals: int = Query(5, ge=1, le=20),
    mode: Literal["top", "above"] = "top",
    recent: int = Query(3, ge=1, le=5, description="How many recent gameweeks of transfers"),
    client: FplClient = Depends(get_fpl_client),
):
    """Captains, chips and recent transfers of the managers above you."""
    league_id = league_id or _env_int("FPL_LEAGUE_ID")
    entry_id = entry_id or _env_int("FPL_ENTRY_ID")
    if league_id is None or entry_id is None:
        raise HTTPException(
            status_code=422,
            detail="Provide league_id and entry_id (or set FPL_LEAGUE_ID and FPL_ENTRY_ID).",
        )
    try:
        return build_rivals(client, league_id, entry_id, rivals=rivals, mode=mode, recent=recent)
    except ReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FplApiError as exc:
        raise HTTPException(status_code=502, detail=f"FPL API problem: {exc}") from exc


@router.get("/report", dependencies=[Depends(rate_limit)])
def league_report(
    league_id: int | None = Query(None, description="Defaults to env FPL_LEAGUE_ID"),
    entry_id: int | None = Query(None, description="Your team id, defaults to env FPL_ENTRY_ID"),
    rivals: int = Query(5, ge=1, le=20),
    mode: Literal["top", "above"] = "top",
    horizon: int = Query(3, ge=1, le=5),
    top: int = Query(10, ge=1, le=30),
    uniqueness: float = Query(0.5, ge=0, le=1),
    free_transfers: int = Query(1, ge=0, le=5, description="Free transfers you have (the API does not tell)"),
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
            free_transfers=free_transfers,
        )
    except ReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FplApiError as exc:
        raise HTTPException(status_code=502, detail=f"FPL API problem: {exc}") from exc
