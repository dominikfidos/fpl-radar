"""Glue code: fetch data from the FPL client, run the analysis, build the report."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from app.league.analysis import (
    DEFAULT_TOTAL_MANAGERS,
    TRANSFER_HIT,
    build_player_views,
    captain_of,
    captain_options,
    captain_share,
    coverage,
    fixture_ticker,
    local_ownership,
    plan_transfers,
    price_watch,
    special_gameweeks,
    starting_xi,
    suggest_transfers,
    team_fixtures,
    top_differentials,
    upcoming_difficulties,
)
from app.league.backtest import WARMUP_GWS, build_rows, run_backtest
from app.league.client import FplApiError


class ReportError(ValueError):
    """The report cannot be built (wrong ids, season not started, ...)."""


def _pick_rivals(standings: list[dict], me: dict, rivals: int, mode: str) -> list[dict]:
    """The managers above `me`: the top of the table or the ones directly above."""
    above = sorted((row for row in standings if row["rank"] < me["rank"]), key=lambda r: r["rank"])
    return above[:rivals] if mode == "top" else above[-rivals:]


def build_backtest(client, *, warmup: int = WARMUP_GWS) -> dict:
    """How good were the model's predictions on the gameweeks already played?

    Needs the live data of every finished gameweek (cached for a long time, finished gameweeks
    never change), so the first run of a season can take a while and later runs are quick.
    """
    bootstrap = client.bootstrap()
    finished = sorted(e["id"] for e in bootstrap["events"] if e.get("finished"))
    if len(finished) <= warmup:
        raise ReportError(
            f"Not enough finished gameweeks for a backtest: {len(finished)} played, "
            f"need at least {warmup + 1}."
        )
    live = {gw: client.live(gw, 86400 * 7).get("elements", []) for gw in finished}
    targets = finished[warmup:]
    rows = build_rows(live, bootstrap["elements"], client.all_fixtures(), targets)
    report = run_backtest(rows)
    report.update(
        {
            "gameweeks": targets,
            "warmup": warmup,
            "notes": [
                "For each gameweek the model only used data from the gameweeks before it, never the one "
                "being predicted.",
                "Availability (injuries, doubts) is not modelled here because FPL does not keep that "
                "history, so real predictions can be a little better than shown.",
                "The baseline is 'average points per gameweek so far'. A model is only useful if it beats it.",
                "Football is noisy and only a few gameweeks are tested: tiny differences between settings "
                "(for example 0.01 in correlation) are noise, so do not tune the constants to them.",
                "A negative bias means the model predicts fewer points than players really score, because it "
                "ignores bonus, saves and similar points. That shifts all players alike, so rankings are not hurt.",
                "Only the expected-points constants can be tested this way. BENCH_WEIGHT and the captain "
                "factor are decision rules, not predictions of points.",
            ],
        }
    )
    return report


def top_and_me(standings: list[dict], me: dict, limit: int) -> list[dict]:
    """The first `limit` rows of the table, plus your row if you are lower down."""
    chosen = sorted(standings, key=lambda r: r["rank"])[:limit]
    if me not in chosen:
        chosen.append(me)
    return chosen


def build_rivals(
    client,
    league_id: int,
    entry_id: int,
    *,
    rivals: int = 5,
    mode: Literal["top", "above"] = "top",
    recent: int = 3,
) -> dict:
    """What the rivals above you are doing: captain, chips and recent transfers.

    recent: how many of the latest gameweeks of transfers to show.
    Squads (and so captains) are as of the current gameweek's deadline.
    """
    bootstrap = client.bootstrap()
    current = next((e for e in bootstrap["events"] if e.get("is_current")), None)
    if current is None:
        raise ReportError("No gameweek has been played yet, so there is nothing to look at.")
    gw = current["id"]
    names = {e["id"]: e.get("web_name", str(e["id"])) for e in bootstrap["elements"]}

    standings = client.league_standings(league_id)
    me = next((row for row in standings if row["entry"] == entry_id), None)
    if me is None:
        raise ReportError(f"Entry {entry_id} was not found in league {league_id}.")
    chosen = _pick_rivals(standings, me, rivals, mode)

    ttl = 86400 if current.get("finished") else 900
    cards = []
    bought: Counter = Counter()
    sold: Counter = Counter()
    for row in chosen:
        try:
            picks = client.entry_picks(row["entry"], gw, ttl)
            history = client.entry_history(row["entry"], ttl)
            transfers = client.entry_transfers(row["entry"], ttl)
        except FplApiError:
            continue  # one unreadable rival must not break the whole view
        captain = captain_of(picks)
        recent_moves = sorted(
            (t for t in transfers if t.get("event", 0) > gw - recent),
            key=lambda t: (-t.get("event", 0), t.get("time", "")),
        )
        for move in recent_moves:
            bought[move["element_in"]] += 1
            sold[move["element_out"]] += 1
        hits = sum(
            h.get("event_transfers_cost", 0) for h in history.get("current", []) if h.get("event", 0) > gw - recent
        )
        cards.append(
            {
                "entry": row["entry"],
                "team": row.get("entry_name"),
                "manager": row.get("player_name"),
                "rank": row["rank"],
                "total": row.get("total"),
                "captain": {"id": captain, "name": names.get(captain, "?")} if captain else None,
                "active_chip": picks.get("active_chip"),
                "chips_used": [
                    {"name": c.get("name"), "event": c.get("event")} for c in history.get("chips", [])
                ],
                "transfers": [
                    {
                        "event": m["event"],
                        "in": names.get(m["element_in"], "?"),
                        "out": names.get(m["element_out"], "?"),
                    }
                    for m in recent_moves
                ],
                "hits": hits,
            }
        )

    def top_names(counter: Counter) -> list[dict]:
        return [{"name": names.get(pid, "?"), "rivals": n} for pid, n in counter.most_common(5)]

    return {
        "gameweek": gw,
        "recent_gameweeks": recent,
        "rivals": cards,
        "trending_in": top_names(bought),
        "trending_out": top_names(sold),
    }


def build_live(client, league_id: int, entry_id: int, *, limit: int = 20) -> dict:
    """Live league table for the running gameweek.

    Points = sum(player live points x pick multiplier) - transfer cost.
    Live total = points before this gameweek (total - event_total) + points now.
    Only the top `limit` managers (plus you) are fetched, to stay polite to the FPL API.
    Automatic substitutions are not applied (FPL only confirms them after the gameweek).
    """
    bootstrap = client.bootstrap()
    current = next((e for e in bootstrap["events"] if e.get("is_current")), None)
    if current is None:
        raise ReportError("No gameweek has been played yet, so there is nothing live to show.")
    gw = current["id"]

    standings = client.league_standings(league_id)
    me = next((row for row in standings if row["entry"] == entry_id), None)
    if me is None:
        raise ReportError(f"Entry {entry_id} was not found in league {league_id}.")
    chosen = top_and_me(standings, me, limit)

    live_points = {
        item["id"]: item.get("stats", {}).get("total_points", 0)
        for item in client.live(gw).get("elements", [])
    }

    rows = []
    for row in chosen:
        try:
            picks = client.entry_picks(row["entry"], gw, 300)
        except FplApiError:
            if row["entry"] == entry_id:
                raise
            continue  # a rival whose squad we cannot read is simply left out
        gw_points = sum(
            live_points.get(p["element"], 0) * p.get("multiplier", 1) for p in picks["picks"]
        )
        history = picks.get("entry_history", {})
        cost = history.get("event_transfers_cost", 0)
        before = row.get("total", 0) - row.get("event_total", 0)
        rows.append(
            {
                "entry": row["entry"],
                "team": row.get("entry_name"),
                "manager": row.get("player_name"),
                "official_rank": row["rank"],
                "gw_points": gw_points - cost,
                "transfer_cost": cost,
                "chip": picks.get("active_chip"),
                "live_total": before + gw_points - cost,
            }
        )

    rows.sort(key=lambda r: (-r["live_total"], r["official_rank"]))
    my_total = next((r["live_total"] for r in rows if r["entry"] == entry_id), 0)
    for i, r in enumerate(rows, start=1):
        r["live_rank"] = i
        r["rank_change"] = r["official_rank"] - i
        r["gap_to_me"] = r["live_total"] - my_total
        r["is_me"] = r["entry"] == entry_id

    return {
        "gameweek": gw,
        "finished": bool(current.get("finished")),
        "shown": len(rows),
        "league_size": len(standings),
        "rows": rows,
        "notes": [
            "Automatic substitutions are not applied until FPL confirms them after the gameweek.",
            "Data is cached for about a minute; only the top of the league (and you) is shown.",
        ],
    }


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
    free_transfers: int = 1,
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
    chosen = _pick_rivals(standings, me, rivals, mode)

    # --- squads (as of the last deadline) ---------------------------------
    ttl = 86400 if current.get("finished") else 900
    my_picks = client.entry_picks(entry_id, current["id"], ttl)
    rival_picks = {row["entry"]: client.entry_picks(row["entry"], current["id"], ttl) for row in chosen}
    rival_squads = {entry: starting_xi(picks) for entry, picks in rival_picks.items()}
    my_squad = [p["element"] for p in my_picks["picks"]]
    bench = {p["element"] for p in my_picks["picks"] if p.get("position", 0) > 11}
    bank = my_picks.get("entry_history", {}).get("bank", 0) / 10

    # --- analysis -----------------------------------------------------------
    ownership = local_ownership(rival_squads)
    fixtures = client.fixtures()
    difficulties = upcoming_difficulties(fixtures, first_event, horizon)
    views = build_player_views(
        bootstrap["elements"], bootstrap["teams"], difficulties, ownership, played_gws, uniqueness
    )
    gameweeks = list(range(first_event, first_event + horizon))
    by_team = team_fixtures(fixtures, first_event, horizon)
    all_team_ids = [t["id"] for t in bootstrap["teams"]]
    team_names = {t["id"]: t.get("short_name") or t["name"] for t in bootstrap["teams"]}

    # Captaincy looks at the NEXT gameweek only (a captain is chosen per gameweek).
    next_views = build_player_views(
        bootstrap["elements"],
        bootstrap["teams"],
        upcoming_difficulties(fixtures, first_event, 1),
        ownership,
        played_gws,
        uniqueness,
    )
    rival_captains = captain_share([captain_of(picks) for picks in rival_picks.values()])
    options = captain_options(my_squad, next_views, rival_captains, uniqueness)

    suggestions = suggest_transfers(
        my_squad, views, bank, top, rivals_count=len(rival_squads), bench_ids=bench
    )

    plans = plan_transfers(
        my_squad, views, bank, bench_ids=bench, free_transfers=free_transfers, limit=3
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
        "captaincy": {
            "gameweek": first_event,
            "safe": max(options, key=lambda o: o.player.expected_points).to_dict() if options else None,
            "differential": options[0].to_dict() if options else None,
            "options": [o.to_dict() for o in options],
        },
        "fixtures": {
            "gameweeks": gameweeks,
            "ticker": fixture_ticker(
                [views[i] for i in my_squad if i in views], by_team, team_names, gameweeks
            ),
            **{
                kind: {
                    gw: [team_names.get(t, "?") for t in teams] for gw, teams in found.items()
                }
                for kind, found in special_gameweeks(all_team_ids, by_team, gameweeks).items()
            },
        },
        "prices": price_watch(
            bootstrap["elements"],
            views,
            bootstrap.get("total_players") or DEFAULT_TOTAL_MANAGERS,
            set(my_squad),
        ),
        "suggestions": [s.to_dict() for s in suggestions],
        "planner": {
            "free_transfers": free_transfers,
            "hit_cost": TRANSFER_HIT,
            "plans": [p.to_dict() for p in plans],
        },
        "differentials": [
            v.to_dict() for v in top_differentials(views, set(my_squad), max_ownership, top)
        ],
        "coverage": coverage(rival_squads, my_squad, views),
    }
