"""Glue code: fetch data from the FPL client, run the analysis, build the report."""

from __future__ import annotations

from typing import Literal

from app.league.analysis import (
    build_player_views,
    coverage,
    local_ownership,
    starting_xi,
    suggest_transfers,
    top_differentials,
    upcoming_difficulties,
)


class ReportError(ValueError):
    """The report cannot be built (wrong ids, season not started, ...)."""


def build_report(
    client,
    league_id: int,
    entry_id: int,
    *,
    rivals: int = 5,
    mode: Literal["top", "above"] = "top",
    horizon: int = 3,
    top: int = 10,
    uniqueness: float = 0.5,
    max_ownership: float = 0.4,
) -> dict:
    """League differential report for one manager in one classic mini-league.

    rivals: how many managers above you to analyse
    mode:   "top"   -> the first `rivals` places of the table
            "above" -> the `rivals` managers directly above you
    horizon: how many upcoming gameweeks to project
    """
    bootstrap = client.bootstrap()
    events = bootstrap["events"]
    current = next((e for e in events if e.get("is_current")), None)
    upcoming = next((e for e in events if e.get("is_next")), None)
    if current is None:
        raise ReportError("No gameweek has been played yet, so there are no squads to analyse.")
    if upcoming is None:
        raise ReportError("The season is over: there is no upcoming gameweek.")
    played_gws = sum(1 for e in events if e.get("finished"))
    first_event = upcoming["id"]

    # --- who is above me? ------------------------------------------------
    standings = client.league_standings(league_id)
    me = next((row for row in standings if row["entry"] == entry_id), None)
    if me is None:
        raise ReportError(f"Entry {entry_id} was not found in league {league_id}.")
    above = sorted((row for row in standings if row["rank"] < me["rank"]), key=lambda r: r["rank"])
    chosen = above[:rivals] if mode == "top" else above[-rivals:]

    # --- squads (as of the last deadline) ---------------------------------
    ttl = 86400 if current.get("finished") else 900
    my_picks = client.entry_picks(entry_id, current["id"], ttl)
    rival_squads = {
        row["entry"]: starting_xi(client.entry_picks(row["entry"], current["id"], ttl))
        for row in chosen
    }
    my_squad = [p["element"] for p in my_picks["picks"]]
    bench = {p["element"] for p in my_picks["picks"] if p.get("position", 0) > 11}
    bank = my_picks.get("entry_history", {}).get("bank", 0) / 10

    # --- analysis -----------------------------------------------------------
    ownership = local_ownership(rival_squads)
    difficulties = upcoming_difficulties(client.fixtures(), first_event, horizon)
    views = build_player_views(
        bootstrap["elements"], bootstrap["teams"], difficulties, ownership, played_gws, uniqueness
    )
    suggestions = suggest_transfers(
        my_squad, views, bank, top, rivals_count=len(rival_squads), bench_ids=bench
    )

    notes = [
        f"Squads are as of the gameweek {current['id']} deadline: FPL hides other managers' "
        "squads until the deadline, so transfers they made since are not visible.",
        "Scores are a simplified heuristic (xG/xA/xGC, minutes, fixture difficulty), not a prediction.",
        "Each suggestion is a separate single transfer with its own target player; the bank is "
        "not shared between them. Swaps of bench players count 25% of their gain.",
    ]
    if not chosen:
        notes.append("You are already first in this league, so there are no rivals above you.")

    return {
        "meta": {
            "league_id": league_id,
            "entry_id": entry_id,
            "my_rank": me["rank"],
            "squad_as_of_gw": current["id"],
            "horizon_gws": list(range(first_event, first_event + horizon)),
            "uniqueness": uniqueness,
            "bank": bank,
            "rivals": [
                {
                    "entry": row["entry"],
                    "team": row.get("entry_name"),
                    "manager": row.get("player_name"),
                    "rank": row["rank"],
                    "total": row.get("total"),
                }
                for row in chosen
            ],
            "notes": notes,
        },
        "my_squad": [views[i].to_dict() for i in my_squad if i in views],
        "suggestions": [s.to_dict() for s in suggestions],
        "differentials": [
            v.to_dict() for v in top_differentials(views, set(my_squad), max_ownership, top)
        ],
        "coverage": coverage(rival_squads, my_squad, views),
    }
