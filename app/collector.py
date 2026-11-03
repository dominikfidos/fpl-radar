"""Collector: runs once, saves a snapshot of all FPL players, then exits.

Run it with:  python -m app.collector
In Kubernetes the same image runs this command on a schedule (CronJob).
"""

import logging
import os
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.migrate import migrate_on_startup
from app.models import Player, Snapshot

# Unofficial public endpoint used by the FPL website itself.
FPL_URL = os.getenv(
    "FPL_API_URL", "https://fantasy.premierleague.com/api/bootstrap-static/"
)
POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

log = logging.getLogger("collector")


def fetch_bootstrap() -> dict:
    """Download one big JSON with all players and teams (a single request)."""
    response = httpx.get(
        FPL_URL,
        headers={"User-Agent": "fpl-radar-portfolio/1.0"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def save_snapshot(db: Session, data: dict) -> int:
    """Store the current state of every player. Returns the number of players saved."""
    teams = {team["id"]: team["name"] for team in data["teams"]}
    players = {player.id: player for player in db.scalars(select(Player))}
    taken_at = datetime.now(UTC)

    # 1) make sure every player exists (and keep names/teams up to date)
    for element in data["elements"]:
        player = players.get(element["id"])
        if player is None:
            player = Player(id=element["id"])
            db.add(player)
        player.name = element["web_name"]
        player.team = teams.get(element["team"], "?")
        player.position = POSITIONS.get(element["element_type"], "?")
    db.flush()  # send players to the DB now, so snapshots can point at them

    # 2) add one snapshot row per player
    for element in data["elements"]:
        db.add(
            Snapshot(
                player_id=element["id"],
                taken_at=taken_at,
                price=element["now_cost"] / 10,  # the API gives tenths of a million
                form=float(element.get("form") or 0),
                total_points=int(element.get("total_points") or 0),
                selected_by_percent=float(element.get("selected_by_percent") or 0),
            )
        )
    db.commit()
    return len(data["elements"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    migrate_on_startup()
    data = fetch_bootstrap()
    with SessionLocal() as db:
        saved = save_snapshot(db, data)
    log.info("Saved %s player snapshots", saved)


if __name__ == "__main__":
    main()
