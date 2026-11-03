"""Pure analysis for the league differential finder.

No network and no database in this file, so it is easy to test.

The model is deliberately simple and transparent. It is a HEURISTIC, not a
crystal ball: every assumption is written next to the code that uses it.

Idea in one paragraph
---------------------
1. For every player we estimate expected points (EP) over the next few
   gameweeks from xG / xA / xGC per 90 minutes, minutes played, availability
   and fixture difficulty.
2. We measure "local ownership": the share of the rivals above you (in your
   mini-league) who own the player.
3. League Differential Score (LDS) = EP * (1 - uniqueness * ownership).
   A player that every rival owns is worth less to you than an equally good
   player that nobody owns, because the first one cannot help you gain ground.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# FPL scoring (simplified): points for a goal depend on the position.
GOAL_POINTS = {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4}
CLEAN_SHEET_POINTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
ASSIST_POINTS = 3
APPEARANCE_POINTS = 2
MAX_PER_CLUB = 3
AVERAGE_GOALS_CONCEDED_PER_90 = 1.3
# Small-sample protection: a player with few minutes has noisy xG/xA, so his
# rates are pulled towards a typical value for his position. After
# PRIOR_MINUTES minutes his own numbers and the typical value count equally.
PRIOR_MINUTES = 450
PRIOR_XG_PER_90 = {"GKP": 0.0, "DEF": 0.04, "MID": 0.12, "FWD": 0.30}
PRIOR_XA_PER_90 = {"GKP": 0.01, "DEF": 0.06, "MID": 0.10, "FWD": 0.10}
# A swap on the bench changes little (bench players rarely score), so it counts less.
BENCH_WEIGHT = 0.25
# injured, unavailable, suspended, not in the squad
UNAVAILABLE_STATUSES = {"i", "u", "s", "n"}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _num(value, default: float = 0.0) -> float:
    """The FPL API sends many numbers as strings ("7.5"). Convert safely."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _per90(element: dict, per90_key: str, total_key: str) -> float | None:
    """Rate per 90 minutes. Uses the ready value if present, else computes it."""
    if element.get(per90_key) is not None:
        return _num(element[per90_key])
    minutes = _num(element.get("minutes"))
    if minutes > 0 and element.get(total_key) is not None:
        return _num(element[total_key]) / minutes * 90
    return None


# --------------------------------------------------------------------------
# Expected points
# --------------------------------------------------------------------------
def _shrink(rate: float | None, minutes: float, prior: float) -> float:
    """Blend the player's own per-90 rate with a typical value (see PRIOR_MINUTES)."""
    if rate is None:
        return prior
    return (rate * minutes + prior * PRIOR_MINUTES) / (minutes + PRIOR_MINUTES)


def availability(element: dict) -> float:
    """0..1: how likely the player is to play. Injured/suspended give 0."""
    if element.get("status", "a") in UNAVAILABLE_STATUSES:
        return 0.0
    chance = element.get("chance_of_playing_next_round")
    if chance is None:
        return 1.0
    return max(0.0, min(1.0, _num(chance) / 100))


def minutes_share(element: dict, played_gws: int) -> float:
    """Share of available minutes the player actually played this season.

    Rough proxy for "is he a regular starter?".
    """
    if played_gws <= 0:
        return 1.0
    return max(0.0, min(1.0, _num(element.get("minutes")) / (90 * played_gws)))


def expected_points_per_match(element: dict, played_gws: int) -> float:
    """Simplified expected FPL points in ONE average match.

    Ignores bonus points, saves, cards and penalties on purpose.
    """
    position = POSITIONS.get(element.get("element_type"), "MID")
    share = minutes_share(element, played_gws) * availability(element)
    if share <= 0:
        return 0.0

    minutes = _num(element.get("minutes"))
    xg = _shrink(_per90(element, "expected_goals_per_90", "expected_goals"), minutes,
                 PRIOR_XG_PER_90[position])
    xa = _shrink(_per90(element, "expected_assists_per_90", "expected_assists"), minutes,
                 PRIOR_XA_PER_90[position])
    xgc = _shrink(_per90(element, "expected_goals_conceded_per_90", "expected_goals_conceded"),
                  minutes, AVERAGE_GOALS_CONCEDED_PER_90)

    points = APPEARANCE_POINTS + xg * GOAL_POINTS[position] + xa * ASSIST_POINTS
    if CLEAN_SHEET_POINTS[position]:
        # Probability of a clean sheet ~ exp(-expected goals conceded) (Poisson).
        points += math.exp(-xgc) * CLEAN_SHEET_POINTS[position]
    if position in ("GKP", "DEF"):
        points -= 0.5 * xgc  # -1 point for every 2 goals conceded
    return share * points


def fixture_multiplier(difficulty: int) -> float:
    """FPL difficulty 1 (easy) .. 5 (hard). 3 is neutral."""
    return max(0.5, min(1.5, 1 + (3 - difficulty) * 0.12))


def upcoming_difficulties(
    fixtures: list[dict], first_event: int, horizon: int
) -> dict[int, list[int]]:
    """team_id -> list of difficulties, one entry per fixture in the window.

    A double gameweek gives a team two entries, a blank gameweek none.
    """
    last_event = first_event + horizon - 1
    by_team: dict[int, list[int]] = {}
    for fixture in fixtures:
        event = fixture.get("event")
        if event is None or not (first_event <= event <= last_event):
            continue
        by_team.setdefault(fixture["team_h"], []).append(fixture.get("team_h_difficulty") or 3)
        by_team.setdefault(fixture["team_a"], []).append(fixture.get("team_a_difficulty") or 3)
    return by_team


def expected_points(element: dict, difficulties: list[int], played_gws: int) -> float:
    per_match = expected_points_per_match(element, played_gws)
    return per_match * sum(fixture_multiplier(d) for d in difficulties)


# --------------------------------------------------------------------------
# Local ownership and the differential score
# --------------------------------------------------------------------------
def starting_xi(picks_response: dict) -> list[int]:
    """Element ids of the 11 starters (bench players are positions 12-15)."""
    return [p["element"] for p in picks_response.get("picks", []) if p.get("position", 99) <= 11]


def local_ownership(squads: dict[int, list[int]]) -> dict[int, float]:
    """Share (0..1) of the given squads that own each player."""
    if not squads:
        return {}
    counts = Counter(pid for squad in squads.values() for pid in set(squad))
    return {pid: n / len(squads) for pid, n in counts.items()}


def league_differential_score(ep: float, ownership: float, uniqueness: float = 0.5) -> float:
    """EP, reduced by how many rivals already own the player.

    uniqueness = 0 -> pure expected points (ignore the rivals)
    uniqueness = 1 -> EP * (1 - ownership): a player all rivals own scores 0
    """
    return ep * max(0.0, 1.0 - uniqueness * ownership)


@dataclass(frozen=True)
class PlayerView:
    id: int
    name: str
    team_id: int
    team: str
    position: str
    price: float
    fixtures: int
    expected_points: float
    ownership: float
    score: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "team": self.team,
            "position": self.position,
            "price": self.price,
            "fixtures": self.fixtures,
            "expected_points": round(self.expected_points, 2),
            "ownership": round(self.ownership, 2),
            "score": round(self.score, 2),
        }


def build_player_views(
    elements: list[dict],
    teams: list[dict],
    difficulties: dict[int, list[int]],
    ownership: dict[int, float],
    played_gws: int,
    uniqueness: float,
) -> dict[int, PlayerView]:
    team_names = {t["id"]: t.get("short_name") or t["name"] for t in teams}
    views: dict[int, PlayerView] = {}
    for element in elements:
        diffs = difficulties.get(element["team"], [])
        ep = expected_points(element, diffs, played_gws)
        own = ownership.get(element["id"], 0.0)
        views[element["id"]] = PlayerView(
            id=element["id"],
            name=element.get("web_name", str(element["id"])),
            team_id=element["team"],
            team=team_names.get(element["team"], "?"),
            position=POSITIONS.get(element.get("element_type"), "?"),
            price=element.get("now_cost", 0) / 10,
            fixtures=len(diffs),
            expected_points=ep,
            ownership=own,
            score=league_differential_score(ep, own, uniqueness),
        )
    return views


# --------------------------------------------------------------------------
# Recommendations
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Suggestion:
    out: PlayerView
    into: PlayerView
    gain: float
    reason: str

    def to_dict(self) -> dict:
        return {
            "out": self.out.to_dict(),
            "in": self.into.to_dict(),
            "score_gain": round(self.gain, 2),
            "reason": self.reason,
        }


def _reason(out: PlayerView, into: PlayerView, rivals_count: int) -> str:
    owners = round(into.ownership * rivals_count)
    return (
        f"{into.name} ({into.team}) is owned by {owners}/{rivals_count} of the rivals above you "
        f"and projects {into.expected_points:.1f} pts over {into.fixtures} fixture(s); "
        f"{out.name} projects {out.expected_points:.1f} pts and is owned by "
        f"{round(out.ownership * rivals_count)}/{rivals_count}."
    )


def suggest_transfers(
    squad_ids: list[int],
    views: dict[int, PlayerView],
    bank: float,
    limit: int = 10,
    rivals_count: int = 0,
    bench_ids: set[int] | None = None,
) -> list[Suggestion]:
    """Best swaps for your squad, best first, every target player used only once.

    Rules: same position, price within budget (sell price + bank), at most
    3 players per club afterwards, candidate must be expected to play.
    Every swap is judged on its own (the bank is not shared between swaps).
    Swapping a bench player counts only BENCH_WEIGHT of the gain.
    """
    bench_ids = bench_ids or set()
    squad = [views[i] for i in squad_ids if i in views]
    owned = set(squad_ids)
    club_counts = Counter(p.team_id for p in squad)

    candidates: dict[str, list[PlayerView]] = {}
    for view in views.values():
        if view.id in owned or view.expected_points <= 0:
            continue
        candidates.setdefault(view.position, []).append(view)
    for pool in candidates.values():
        pool.sort(key=lambda v: v.score, reverse=True)

    def best_for(out: PlayerView, used_in: set[int]) -> Suggestion | None:
        for cand in candidates.get(out.position, []):
            raw_gain = cand.score - out.score
            if raw_gain <= 0:
                return None  # pool is sorted: nobody further down can be better
            if cand.id in used_in:
                continue
            if round(cand.price, 1) > round(out.price + bank, 1):
                continue
            clubs_after = club_counts[cand.team_id] + 1 - (1 if cand.team_id == out.team_id else 0)
            if clubs_after > MAX_PER_CLUB:
                continue
            weight = BENCH_WEIGHT if out.id in bench_ids else 1.0
            return Suggestion(out, cand, raw_gain * weight, _reason(out, cand, rivals_count))
        return None

    # Greedy: take the best swap overall, mark its target as used, repeat.
    suggestions: list[Suggestion] = []
    used_in: set[int] = set()
    used_out: set[int] = set()
    while len(suggestions) < limit:
        options = [
            s for out in squad if out.id not in used_out
            if (s := best_for(out, used_in)) is not None
        ]
        if not options:
            break
        best = max(options, key=lambda s: s.gain)
        suggestions.append(best)
        used_in.add(best.into.id)
        used_out.add(best.out.id)
    return suggestions


def top_differentials(
    views: dict[int, PlayerView],
    owned_ids: set[int],
    max_ownership: float = 0.4,
    limit: int = 10,
) -> list[PlayerView]:
    """Best-scoring players you do not own and few rivals own."""
    pool = [
        v
        for v in views.values()
        if v.id not in owned_ids and v.ownership <= max_ownership and v.expected_points > 0
    ]
    pool.sort(key=lambda v: v.score, reverse=True)
    return pool[:limit]


def coverage(
    rival_squads: dict[int, list[int]], my_ids: list[int], views: dict[int, PlayerView]
) -> list[dict]:
    """Data for the 'squad coverage' chart: who owns what among the rivals."""
    ownership = local_ownership(rival_squads)
    ids = set(my_ids) | set(ownership)
    rows = []
    for pid in ids:
        view = views.get(pid)
        if view is None:
            continue
        rows.append(
            {
                "id": pid,
                "name": view.name,
                "team": view.team,
                "position": view.position,
                "rivals_owning": round(ownership.get(pid, 0.0) * len(rival_squads)),
                "share": round(ownership.get(pid, 0.0), 2),
                "mine": pid in my_ids,
            }
        )
    rows.sort(key=lambda r: (-r["share"], r["name"]))
    return rows
