from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Player(Base):
    """One row per FPL player (changes rarely)."""

    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)  # same id as in the FPL API
    name: Mapped[str] = mapped_column(String(100))
    team: Mapped[str] = mapped_column(String(50))
    position: Mapped[str] = mapped_column(String(3))  # GKP / DEF / MID / FWD


class Snapshot(Base):
    """One row per player per collector run: how the player looked at that moment."""

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    price: Mapped[float]  # in millions, e.g. 10.5
    form: Mapped[float]
    total_points: Mapped[int]
    selected_by_percent: Mapped[float]


class RankHistory(Base):
    """A manager's place in a mini-league after one finished gameweek.

    Rank is counted among the managers we track (the top of the league plus you),
    from their total points after that gameweek. Ties share a rank.
    """

    __tablename__ = "rank_history"

    league_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    entry_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    event: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)  # gameweek number
    rank: Mapped[int]
    total_points: Mapped[int]
    event_points: Mapped[int]
    team: Mapped[str | None] = mapped_column(String(100))
    manager: Mapped[str | None] = mapped_column(String(100))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ApiCache(Base):
    """Cached responses of the FPL API (key = request path)."""

    __tablename__ = "api_cache"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[Any] = mapped_column(JSON)
