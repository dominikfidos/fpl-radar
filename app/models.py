from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
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
