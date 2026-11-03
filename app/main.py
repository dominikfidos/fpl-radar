from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import Base, engine, get_db
from app.league.router import router as league_router
from app.metrics import router as metrics_router
from app.metrics import track_requests
from app.models import Player, Snapshot
from app.web import router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once on startup: create the tables if they do not exist yet.
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="FPL Radar API", version="1.0.0", lifespan=lifespan)
app.include_router(league_router)
app.include_router(web_router)
app.include_router(metrics_router)
app.middleware("http")(track_requests)


class PlayerStats(BaseModel):
    id: int
    name: str
    team: str
    position: str
    price: float
    form: float
    total_points: int
    selected_by_percent: float


class PricePoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # can be built from a DB row

    taken_at: datetime
    price: float
    form: float
    total_points: int
    selected_by_percent: float


class PriceChange(BaseModel):
    id: int
    name: str
    team: str
    old_price: float
    new_price: float
    change: float


def latest_snapshot_time(db: Session) -> datetime | None:
    """Timestamp of the newest collector run (None if nothing was collected yet)."""
    return db.scalar(select(func.max(Snapshot.taken_at)))


@app.get("/health")
def health():
    # Deliberately does not touch the database: a liveness probe should only
    # tell whether the process is alive, not whether its dependencies are.
    return {"status": "ok"}


@app.get("/api/players/top", response_model=list[PlayerStats])
def top_players(
    position: str | None = Query(None, pattern="^(GKP|DEF|MID|FWD)$"),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """Players in the best form, based on the newest snapshot."""
    latest = latest_snapshot_time(db)
    if latest is None:
        return []

    query = (
        select(Player, Snapshot)
        .join(Snapshot, Snapshot.player_id == Player.id)
        .where(Snapshot.taken_at == latest)
    )
    if position:
        query = query.where(Player.position == position)
    query = query.order_by(Snapshot.form.desc(), Snapshot.total_points.desc()).limit(limit)

    return [
        PlayerStats(
            id=player.id,
            name=player.name,
            team=player.team,
            position=player.position,
            price=snap.price,
            form=snap.form,
            total_points=snap.total_points,
            selected_by_percent=snap.selected_by_percent,
        )
        for player, snap in db.execute(query)
    ]


@app.get("/api/players/{player_id}/history", response_model=list[PricePoint])
def player_history(player_id: int, db: Session = Depends(get_db)):
    """How price, form and ownership of one player changed over time."""
    if db.get(Player, player_id) is None:
        raise HTTPException(status_code=404, detail="Player not found")
    query = select(Snapshot).where(Snapshot.player_id == player_id).order_by(Snapshot.taken_at)
    return db.scalars(query).all()


@app.get("/api/price-changes", response_model=list[PriceChange])
def price_changes(
    hours: int = Query(24, ge=1, le=168),
    db: Session = Depends(get_db),
):
    """Players whose price changed between the newest snapshot and the oldest one within `hours`."""
    newest = latest_snapshot_time(db)
    if newest is None:
        return []

    cutoff = newest - timedelta(hours=hours)
    baseline = db.scalar(select(func.min(Snapshot.taken_at)).where(Snapshot.taken_at >= cutoff))
    if baseline is None or baseline == newest:
        return []  # only one snapshot in the window, nothing to compare

    def prices_at(moment: datetime) -> dict[int, float]:
        rows = db.execute(
            select(Snapshot.player_id, Snapshot.price).where(Snapshot.taken_at == moment)
        )
        return {player_id: price for player_id, price in rows}

    old_prices = prices_at(baseline)
    new_prices = prices_at(newest)
    players = {player.id: player for player in db.scalars(select(Player))}

    changes = []
    for player_id, new_price in new_prices.items():
        old_price = old_prices.get(player_id)
        if old_price is not None and old_price != new_price:
            player = players[player_id]
            changes.append(
                PriceChange(
                    id=player_id,
                    name=player.name,
                    team=player.team,
                    old_price=old_price,
                    new_price=new_price,
                    change=round(new_price - old_price, 1),
                )
            )
    changes.sort(key=lambda item: abs(item.change), reverse=True)
    return changes
