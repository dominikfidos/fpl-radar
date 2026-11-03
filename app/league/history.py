"""Rank history of a mini-league: where every manager stood after each finished gameweek.

FPL only shows the CURRENT league table. But every manager's own history (points after each
gameweek) is public, so we can rebuild past tables: rank = how many managers had more points.
The result is stored in the database (table rank_history), so:
- the chart works straight away for gameweeks that were played before you first opened the app,
- once the data is stored, showing the chart needs no FPL requests at all.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.league.client import FplApiError
from app.league.service import ReportError, top_and_me
from app.models import RankHistory

DEFAULT_TRACKED = 25  # how many managers (from the top of the table) we rebuild history for


def compute_ranks(totals: dict[int, dict[int, int]], events: list[int]) -> dict[int, dict[int, int]]:
    """ranks[event][entry] from totals[entry][event] (total points after that gameweek).

    Managers with equal points share a rank (1, 2, 2, 4 ...).
    """
    ranks: dict[int, dict[int, int]] = {}
    for event in events:
        points = {entry: by_event[event] for entry, by_event in totals.items() if event in by_event}
        ranks[event] = {
            entry: 1 + sum(1 for other in points.values() if other > mine) for entry, mine in points.items()
        }
    return ranks


def record_rank_history(
    session_factory: Callable[[], Session],
    client,
    league_id: int,
    entry_id: int,
    *,
    tracked: int = DEFAULT_TRACKED,
) -> int:
    """Rebuild and store the history of the finished gameweeks. Returns the number of rows written.

    Safe to call as often as you like: when every finished gameweek is already stored for every
    tracked manager it returns 0 without asking FPL for anything but the (cached) gameweek list.
    """
    bootstrap = client.bootstrap()
    finished = sorted(e["id"] for e in bootstrap["events"] if e.get("finished"))
    if not finished:
        return 0

    standings = client.league_standings(league_id)
    me = next((row for row in standings if row["entry"] == entry_id), None)
    if me is None:
        raise ReportError(f"Entry {entry_id} was not found in league {league_id}.")
    chosen = top_and_me(standings, me, tracked)
    wanted = {row["entry"] for row in chosen}

    with session_factory() as db:
        stored_events = set(
            db.scalars(select(RankHistory.event).where(RankHistory.league_id == league_id).distinct())
        )
        stored_entries = set(
            db.scalars(
                select(RankHistory.entry_id).where(
                    RankHistory.league_id == league_id, RankHistory.event == finished[-1]
                )
            )
        )
    if set(finished) <= stored_events and wanted <= stored_entries:
        return 0  # already up to date

    totals: dict[int, dict[int, int]] = {}
    event_points: dict[int, dict[int, int]] = {}
    for row in chosen:
        try:
            history = client.entry_history(row["entry"], 3600)
        except FplApiError:
            if row["entry"] == entry_id:
                raise
            continue  # a manager whose history we cannot read is left out of the chart
        for item in history.get("current", []):
            if item.get("event") in finished:
                totals.setdefault(row["entry"], {})[item["event"]] = item.get("total_points", 0)
                event_points.setdefault(row["entry"], {})[item["event"]] = item.get("points", 0)

    ranks = compute_ranks(totals, finished)
    names = {row["entry"]: row for row in chosen}
    now = datetime.now(UTC)
    written = 0
    with session_factory() as db:
        for event, per_entry in ranks.items():
            for entry, rank in per_entry.items():
                db.merge(
                    RankHistory(
                        league_id=league_id,
                        entry_id=entry,
                        event=event,
                        rank=rank,
                        total_points=totals[entry][event],
                        event_points=event_points[entry][event],
                        team=names[entry].get("entry_name"),
                        manager=names[entry].get("player_name"),
                        recorded_at=now,
                    )
                )
                written += 1
        db.commit()
    return written


def load_rank_history(
    session_factory: Callable[[], Session], league_id: int, entry_id: int, *, show: int = 10
) -> dict:
    """Stored history shaped for a chart: one series per manager, one value per gameweek.

    Shows the `show` managers who are best placed now, plus you (even if you are lower).
    """
    with session_factory() as db:
        rows = db.scalars(
            select(RankHistory).where(RankHistory.league_id == league_id).order_by(RankHistory.event)
        ).all()
    if not rows:
        return {"league_id": league_id, "gameweeks": [], "tracked": 0, "series": []}

    gameweeks = sorted({r.event for r in rows})
    by_entry: dict[int, dict[int, RankHistory]] = {}
    for r in rows:
        by_entry.setdefault(r.entry_id, {})[r.event] = r

    def latest(entry: int) -> tuple[int, int]:
        last = max(by_entry[entry])
        return (-last, by_entry[entry][last].rank)  # most recent data first, then best rank

    ordered = sorted(by_entry, key=lambda e: (latest(e)[0], latest(e)[1], e))
    chosen = ordered[:show]
    if entry_id in by_entry and entry_id not in chosen:
        chosen.append(entry_id)

    series = []
    for entry in chosen:
        points = by_entry[entry]
        newest = points[max(points)]
        series.append(
            {
                "entry": entry,
                "team": newest.team,
                "manager": newest.manager,
                "is_me": entry == entry_id,
                "ranks": [points[g].rank if g in points else None for g in gameweeks],
                "totals": [points[g].total_points if g in points else None for g in gameweeks],
            }
        )
    return {"league_id": league_id, "gameweeks": gameweeks, "tracked": len(by_entry), "series": series}


def build_history(
    session_factory: Callable[[], Session],
    client,
    league_id: int,
    entry_id: int,
    *,
    show: int = 10,
    tracked: int = DEFAULT_TRACKED,
) -> dict:
    """Make sure the history is stored (cheap when it already is), then return it."""
    written = record_rank_history(session_factory, client, league_id, entry_id, tracked=tracked)
    data = load_rank_history(session_factory, league_id, entry_id, show=show)
    data["rows_written"] = written
    return data
