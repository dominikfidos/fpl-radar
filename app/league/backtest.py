"""Backtest of the expected-points model on gameweeks that are already played.

Question answered: "if the model had made its prediction BEFORE gameweek N, how close would it
have been to what the players really scored?"

How we avoid cheating (look-ahead): for gameweek N the model only sees what happened in
gameweeks 1..N-1 (summed minutes, xG, xA, xGC). It never sees gameweek N itself.

Known simplifications, shown in the report notes:
- we do not know who was injured or doubtful at the time, so availability is not modelled;
- the model is compared with a naive baseline (average points per gameweek so far), because a
  model is only useful if it beats something simple.

Pure code: no network, no database.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from app.league import analysis

WARMUP_GWS = 3  # the first gameweeks have too little data to judge, so predictions start after these
TOP_N = 10  # "the 10 players the model likes best"
PRIOR_MINUTES_GRID = (0, 150, 300, 450, 900, 1800)
FIXTURE_SLOPE_GRID = (0.0, 0.06, 0.12, 0.18, 0.24)


@dataclass(frozen=True)
class Row:
    """One player before one gameweek: what the model may know, and what really happened."""

    player_id: int
    element_type: int
    minutes: float
    xg: float
    xa: float
    xgc: float
    points_so_far: float
    played_gws: int  # gameweeks that were played BEFORE this one
    difficulties: tuple[int, ...]  # this gameweek's fixtures of his club (empty = blank)
    actual: float  # points he really scored in this gameweek


def _stat(item: dict, key: str) -> float:
    return analysis._num(item.get("stats", {}).get(key))


def build_rows(
    live_by_gw: dict[int, list[dict]],
    elements: list[dict],
    fixtures: list[dict],
    targets: list[int],
) -> dict[int, list[Row]]:
    """For every target gameweek, one Row per player who had already played some minutes.

    Rows only use data from earlier gameweeks (cumulative sums); the target gameweek provides
    just the fixtures (known in advance) and the actual points.
    """
    kind = {e["id"]: (e.get("element_type"), e.get("team")) for e in elements}
    diffs: dict[tuple[int, int], list[int]] = {}
    for fx in fixtures:
        event = fx.get("event")
        if event is None:
            continue
        diffs.setdefault((event, fx["team_h"]), []).append(fx.get("team_h_difficulty") or 3)
        diffs.setdefault((event, fx["team_a"]), []).append(fx.get("team_a_difficulty") or 3)

    cumulative: dict[int, dict[str, float]] = {}
    rows: dict[int, list[Row]] = {}
    for gw in sorted(live_by_gw):
        if gw in targets:
            played = sum(1 for earlier in live_by_gw if earlier < gw)
            out = []
            for item in live_by_gw[gw]:
                pid = item["id"]
                before = cumulative.get(pid)
                if before is None or before["minutes"] <= 0 or pid not in kind:
                    continue  # the model has nothing to go on for a player who never played
                element_type, team = kind[pid]
                out.append(
                    Row(
                        player_id=pid,
                        element_type=element_type,
                        minutes=before["minutes"],
                        xg=before["xg"],
                        xa=before["xa"],
                        xgc=before["xgc"],
                        points_so_far=before["points"],
                        played_gws=played,
                        difficulties=tuple(diffs.get((gw, team), ())),
                        actual=_stat(item, "total_points"),
                    )
                )
            rows[gw] = out
        for item in live_by_gw[gw]:  # only now add this gameweek to what is "known"
            c = cumulative.setdefault(item["id"], {"minutes": 0.0, "xg": 0.0, "xa": 0.0, "xgc": 0.0, "points": 0.0})
            c["minutes"] += _stat(item, "minutes")
            c["xg"] += _stat(item, "expected_goals")
            c["xa"] += _stat(item, "expected_assists")
            c["xgc"] += _stat(item, "expected_goals_conceded")
            c["points"] += _stat(item, "total_points")
    return rows


def model_prediction(
    row: Row,
    prior_minutes: float = analysis.PRIOR_MINUTES,
    slope: float = analysis.FIXTURE_SLOPE,
) -> float:
    """The app's expected points for the gameweek, using only what the Row knows."""
    element = {
        "element_type": row.element_type,
        "minutes": row.minutes,
        "expected_goals": row.xg,
        "expected_assists": row.xa,
        "expected_goals_conceded": row.xgc,
    }
    per_match = analysis.expected_points_per_match(element, row.played_gws, prior_minutes)
    return per_match * sum(analysis.fixture_multiplier(d, slope) for d in row.difficulties)


def baseline_prediction(row: Row) -> float:
    """The naive guess to beat: average points per gameweek so far."""
    return row.points_so_far / row.played_gws if row.played_gws else 0.0


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Correlation -1..1 (None when it cannot be computed, e.g. all predictions equal)."""
    n = len(xs)
    if n < 2:
        return None
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def _top_actual(rows: list[Row], preds: list[float], n: int = TOP_N) -> float:
    """Average real points of the n players with the highest prediction."""
    order = sorted(range(len(rows)), key=lambda i: (-preds[i], rows[i].player_id))[:n]
    return sum(rows[i].actual for i in order) / len(order) if order else 0.0


def evaluate(rows_by_gw: dict[int, list[Row]], predict: Callable[[Row], float], per_gw: bool = False) -> dict:
    """Score one prediction rule over all target gameweeks.

    corr  = correlation of prediction and real points, over all (player, gameweek) pairs
    top10 = average real points of the 10 players it ranks highest (mean over gameweeks)
    mae   = average size of the error in points; bias = average prediction minus real points
    """
    all_pred: list[float] = []
    all_actual: list[float] = []
    tops: list[float] = []
    details = []
    for gw, rows in sorted(rows_by_gw.items()):
        if not rows:
            continue
        preds = [predict(r) for r in rows]
        actual = [r.actual for r in rows]
        all_pred += preds
        all_actual += actual
        top = _top_actual(rows, preds)
        tops.append(top)
        if per_gw:
            details.append({"gw": gw, "n": len(rows), "corr": pearson(preds, actual), "top10": top})
    n = len(all_pred)
    result = {
        "n": n,
        "corr": pearson(all_pred, all_actual),
        "top10": sum(tops) / len(tops) if tops else 0.0,
        "mae": sum(abs(p - a) for p, a in zip(all_pred, all_actual, strict=True)) / n if n else 0.0,
        "bias": sum(p - a for p, a in zip(all_pred, all_actual, strict=True)) / n if n else 0.0,
    }
    if per_gw:
        result["per_gw"] = details
    return result


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _clean(metrics: dict) -> dict:
    out = {k: (_round(v) if isinstance(v, float) or v is None else v) for k, v in metrics.items() if k != "per_gw"}
    return out


def run_backtest(rows_by_gw: dict[int, list[Row]]) -> dict:
    """Full report: model vs baseline, per gameweek, and a sweep of the tunable constants."""
    model = evaluate(rows_by_gw, model_prediction, per_gw=True)
    base = evaluate(rows_by_gw, baseline_prediction, per_gw=True)
    base_by_gw = {d["gw"]: d for d in base["per_gw"]}
    per_gw = [
        {
            "gw": d["gw"],
            "n": d["n"],
            "corr_model": _round(d["corr"]),
            "corr_baseline": _round(base_by_gw[d["gw"]]["corr"]),
            "top10_model": _round(d["top10"], 2),
            "top10_baseline": _round(base_by_gw[d["gw"]]["top10"], 2),
        }
        for d in model["per_gw"]
    ]

    def sweep(values, make_predict, current):
        rows = []
        for value in values:
            m = evaluate(rows_by_gw, make_predict(value))
            rows.append({"value": value, "corr": _round(m["corr"]), "top10": _round(m["top10"], 2),
                         "mae": _round(m["mae"], 2), "is_current": value == current})
        scored = [r for r in rows if r["corr"] is not None]
        best = max(scored, key=lambda r: r["corr"])["value"] if scored else None
        for r in rows:
            r["is_best"] = r["value"] == best
        return rows

    return {
        "summary": {"model": _clean(model), "baseline": _clean(base)},
        "per_gw": per_gw,
        "sweeps": {
            "prior_minutes": sweep(
                PRIOR_MINUTES_GRID, lambda v: lambda r: model_prediction(r, prior_minutes=v), analysis.PRIOR_MINUTES
            ),
            "fixture_slope": sweep(
                FIXTURE_SLOPE_GRID, lambda v: lambda r: model_prediction(r, slope=v), analysis.FIXTURE_SLOPE
            ),
        },
        "current": {"prior_minutes": analysis.PRIOR_MINUTES, "fixture_slope": analysis.FIXTURE_SLOPE},
    }
