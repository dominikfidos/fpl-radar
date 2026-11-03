import httpx
import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.league import analysis
from app.league.cache import DbCache
from app.league.client import FplApiError, FplClient
from app.league.router import get_fpl_client
from app.league.service import ReportError, build_live, build_report, build_rivals
from app.main import app


# --------------------------------------------------------------------------
# Fake FPL data (tiny imitation of the real API)
# --------------------------------------------------------------------------
def element(pid, name, team, etype, cost, *, minutes=180, xg=0.1, xa=0.1, xgc=1.2,
            status="a", chance=None):
    return {
        "id": pid, "web_name": name, "team": team, "element_type": etype, "now_cost": cost,
        "minutes": minutes, "expected_goals_per_90": str(xg), "expected_assists_per_90": str(xa),
        "expected_goals_conceded_per_90": str(xgc), "status": status,
        "chance_of_playing_next_round": chance,
    }


BOOTSTRAP = {
    "events": [
        {"id": 1, "finished": True, "is_current": False, "is_next": False},
        {"id": 2, "finished": True, "is_current": True, "is_next": False},
        {"id": 3, "finished": False, "is_current": False, "is_next": True},
    ],
    "teams": [
        {"id": 1, "name": "Arsenal", "short_name": "ARS"},
        {"id": 2, "name": "Chelsea", "short_name": "CHE"},
        {"id": 3, "name": "Spurs", "short_name": "TOT"},
        {"id": 4, "name": "Liverpool", "short_name": "LIV"},
    ],
    "elements": [
        element(1, "Template", 1, 3, 100, xg=0.5, xa=0.3),
        element(2, "Meh", 2, 3, 50, xg=0.05, xa=0.05),
        element(3, "Gem", 3, 3, 70, xg=0.5, xa=0.2, xgc=1.0),
        element(4, "Star", 4, 3, 75, xg=0.7, xa=0.3, xgc=0.9),
        element(5, "Keeper", 1, 1, 50, xg=0.0, xa=0.0, xgc=1.0),
        element(6, "Defender", 2, 2, 45, xg=0.05, xa=0.1, xgc=1.1),
        element(7, "Pricey", 3, 3, 140, xg=0.9, xa=0.4),
        element(8, "Injured", 3, 3, 60, xg=0.9, xa=0.4, status="i"),
        element(99, "BenchGuy", 4, 2, 40, minutes=0),
    ],
}

FIXTURES = [
    {"event": 3, "team_h": 1, "team_a": 2, "team_h_difficulty": 3, "team_a_difficulty": 3},
    {"event": 3, "team_h": 3, "team_a": 4, "team_h_difficulty": 2, "team_a_difficulty": 4},
    {"event": 9, "team_h": 3, "team_a": 1, "team_h_difficulty": 1, "team_a_difficulty": 5},
]


def picks(*elements, bank=25):
    return {
        "entry_history": {"bank": bank},
        "picks": [{"element": e, "position": i + 1} for i, e in enumerate(elements)],
    }


STANDINGS = [
    {"entry": 200, "entry_name": "Leaders FC", "player_name": "A", "rank": 1, "total": 100},
    {"entry": 300, "entry_name": "Second FC", "player_name": "B", "rank": 2, "total": 90},
    {"entry": 100, "entry_name": "BANDZIORY", "player_name": "Me", "rank": 3, "total": 80},
    {"entry": 400, "entry_name": "Below FC", "player_name": "C", "rank": 4, "total": 70},
]


class FakeClient:
    def __init__(self, standings=None):
        self.standings = standings or STANDINGS
        rival_a = picks(1, 4, 5)
        rival_a["picks"].append({"element": 99, "position": 13})  # bench: must be ignored
        rival_a["picks"][1]["is_captain"] = True  # rival A captains Star (element 4)
        self.picks = {100: picks(1, 2, 5, 6), 200: rival_a, 300: picks(1, 4, 6), 400: picks(3)}

    def bootstrap(self):
        return BOOTSTRAP

    def fixtures(self):
        return FIXTURES

    def league_standings(self, league_id, max_pages=10):
        return self.standings

    def entry_picks(self, entry_id, event, ttl=900):
        return self.picks[entry_id]


# --------------------------------------------------------------------------
# analysis.py
# --------------------------------------------------------------------------
def test_local_ownership_counts_share_of_squads():
    assert analysis.local_ownership({1: [10, 11], 2: [10, 12]}) == {10: 1.0, 11: 0.5, 12: 0.5}
    assert analysis.local_ownership({}) == {}


def test_starting_xi_ignores_bench():
    response = {"picks": [{"element": 1, "position": 11}, {"element": 2, "position": 12}]}
    assert analysis.starting_xi(response) == [1]


def test_differential_score_penalises_ownership():
    assert analysis.league_differential_score(10, 0.0, 0.5) == 10
    assert analysis.league_differential_score(10, 1.0, 0.5) == 5
    assert analysis.league_differential_score(10, 1.0, 1.0) == 0
    assert analysis.league_differential_score(10, 1.0, 0.0) == 10


def test_easier_fixture_gives_more_points_and_double_gameweek_doubles_them():
    el = BOOTSTRAP["elements"][0]
    easy = analysis.expected_points(el, [2], played_gws=2)
    hard = analysis.expected_points(el, [5], played_gws=2)
    assert easy > hard
    assert analysis.expected_points(el, [3, 3], 2) == pytest.approx(2 * analysis.expected_points(el, [3], 2))
    assert analysis.expected_points(el, [], 2) == 0  # blank gameweek


def test_unavailable_or_non_playing_players_have_zero_points():
    injured = BOOTSTRAP["elements"][7]
    bench = BOOTSTRAP["elements"][8]
    assert analysis.expected_points(injured, [3], 2) == 0
    assert analysis.expected_points(bench, [3], 2) == 0


def test_doubtful_player_is_scaled_by_chance_of_playing():
    full = element(1, "A", 1, 3, 100)
    half = element(1, "A", 1, 3, 100, status="d", chance=50)
    assert analysis.expected_points_per_match(half, 2) == pytest.approx(
        0.5 * analysis.expected_points_per_match(full, 2)
    )


def test_upcoming_difficulties_uses_only_the_window():
    result = analysis.upcoming_difficulties(FIXTURES, first_event=3, horizon=3)
    assert result == {1: [3], 2: [3], 3: [2], 4: [4]}  # the event 9 fixture is ignored


def test_suggestions_respect_budget_position_and_club_limit():
    # Gem costs 7.0. Selling Meh (5.0) with 1.0 in the bank is not enough ...
    views = analysis.build_player_views(
        BOOTSTRAP["elements"], BOOTSTRAP["teams"], {1: [3], 2: [3], 3: [2], 4: [4]}, {}, 2, 0.5
    )
    poor = analysis.suggest_transfers([2], views, bank=1.0)
    assert all(s.into.name not in {"Gem", "Pricey"} for s in poor)
    # ... but with 2.5 in the bank it is.
    rich = analysis.suggest_transfers([2], views, bank=2.5)
    assert rich[0].into.name == "Gem"
    assert all(s.into.position == "MID" for s in rich)

    # Club limit: already 3 Spurs players, so another Spurs player is not allowed.
    spurs = [element(10 + i, f"S{i}", 3, 3, 50) for i in range(3)]
    elements = spurs + [element(20, "Swap", 2, 3, 50, xg=0.0, xa=0.0),
                        element(21, "Spur4", 3, 3, 50, xg=0.8, xa=0.4)]
    views = analysis.build_player_views(elements, BOOTSTRAP["teams"], {2: [3], 3: [3]}, {}, 2, 0.5)
    result = analysis.suggest_transfers([10, 11, 12, 20], views, bank=0)
    assert all(s.into.name != "Spur4" or s.out.team == "TOT" for s in result)


def test_every_target_is_suggested_only_once_and_bench_swaps_count_less():
    views = analysis.build_player_views(
        BOOTSTRAP["elements"], BOOTSTRAP["teams"], {1: [3], 2: [3], 3: [2], 4: [4]}, {}, 2, 0.5
    )
    result = analysis.suggest_transfers([1, 2], views, bank=5.0)  # two midfielders
    targets = [s.into.id for s in result]
    assert len(targets) == len(set(targets))

    full = analysis.suggest_transfers([2], views, bank=2.5)[0]
    bench = analysis.suggest_transfers([2], views, bank=2.5, bench_ids={2})[0]
    assert bench.into.id == full.into.id
    assert bench.gain == pytest.approx(full.gain * analysis.BENCH_WEIGHT)


def view(pid, team, position, price, ep):
    """A ready-made PlayerView (score = ep, i.e. nobody owns the player)."""
    return analysis.PlayerView(pid, f"P{pid}", team, f"T{team}", position, price, 1, ep, 0.0, ep)


def test_planner_shares_the_budget_between_two_transfers():
    views = {v.id: v for v in [
        view(1, 1, "MID", 5.0, 1), view(2, 2, "MID", 5.0, 1),          # my squad
        view(10, 3, "MID", 7.0, 6), view(11, 4, "MID", 3.0, 5),        # targets
        view(12, 5, "GKP", 4.0, 50),                                    # wrong position: never used
    ]}
    plans = analysis.plan_transfers([1, 2], views, bank=0, free_transfers=1)
    singles = [p for p in plans if len(p.swaps) == 1]
    pairs = [p for p in plans if len(p.swaps) == 2]
    # Player 10 costs 7.0: alone he is unaffordable (5.0 + 0), together with a cheap
    # swap the budget 10.0 covers 7.0 + 3.0.
    assert all(s.swaps[0][1].id != 10 for s in singles)
    best = pairs[0]
    assert {i.id for _, i in best.swaps} == {10, 11}
    assert best.bank_after == 0.0
    assert best.ep_gain == pytest.approx(9)
    assert best.hit == 4 and best.net_ep == pytest.approx(5)
    assert best.net_score == pytest.approx(5) and best.to_dict()["worth_it"] is True
    assert all(i.position == "MID" for p in plans for _, i in p.swaps)
    # With two free transfers the same plan costs nothing.
    free = analysis.plan_transfers([1, 2], views, bank=0, free_transfers=2)
    assert [p for p in free if len(p.swaps) == 2][0].hit == 0


def test_planner_says_when_the_extra_transfer_is_not_worth_four_points():
    views = {v.id: v for v in [
        view(1, 1, "MID", 5.0, 1), view(2, 2, "MID", 5.0, 1),
        view(10, 3, "MID", 5.0, 2.5), view(11, 4, "MID", 5.0, 2.5),
    ]}
    plans = analysis.plan_transfers([1, 2], views, bank=0, free_transfers=1)
    pair = next(p for p in plans if len(p.swaps) == 2)
    single = next(p for p in plans if len(p.swaps) == 1)
    assert pair.ep_gain == pytest.approx(3) and pair.net_score == pytest.approx(-1)
    assert pair.to_dict()["worth_it"] is False
    assert single.hit == 0 and single.to_dict()["worth_it"] is True


def test_price_status_thresholds():
    assert analysis.price_status(1.0) == "rise" and analysis.price_status(5) == "rise"
    assert analysis.price_status(0.6) == "watch_rise"
    assert analysis.price_status(0.1) is None and analysis.price_status(-0.1) is None
    assert analysis.price_status(-0.6) == "watch_fall" and analysis.price_status(-1.0) == "fall"


def test_price_watch_ranks_pressure_and_flags_my_players():
    # 1,000,000 managers -> 1% = 10,000 net transfers.
    def player(pid, tin, tout, **extra):
        return {**element(pid, f"P{pid}", 1, 3, 50), "transfers_in_event": tin,
                "transfers_out_event": tout, "selected_by_percent": "12.5", **extra}

    elements = [
        player(1, 20_000, 8_000),                      # +12,000 -> pressure 1.2: rise
        player(2, 6_000, 0, cost_change_event=1),      # +6,000 -> watch, already moved
        player(3, 1_000, 16_000),                      # -15,000 -> fall
        player(4, 500, 100),                           # tiny -> not listed
        player(5, 0, 7_000),                           # -7,000 -> watch_fall
    ]
    views = {e["id"]: view(e["id"], 1, "MID", 5.0, 3) for e in elements}
    watch = analysis.price_watch(elements, views, 1_000_000, my_squad={3})
    assert [r["id"] for r in watch["risers"]] == [1, 2]
    assert watch["risers"][0]["pressure"] == 1.2 and watch["risers"][0]["status"] == "rise"
    assert watch["risers"][1]["status"] == "watch_rise" and watch["risers"][1]["changed_this_gw"]
    assert [r["id"] for r in watch["fallers"]] == [3, 5]
    assert [r["id"] for r in watch["my_falling"]] == [3] and watch["my_rising"] == []
    assert watch["risers"][0]["owned_percent"] == 12.5


def test_planner_separates_points_verdict_from_differential_verdict():
    # Star owned by every rival (score halved) -> cheap unowned player: slightly fewer
    # points, but a clear differential gain.
    star = analysis.PlayerView(1, "Star", 1, "T1", "FWD", 15.0, 1, 10.0, 1.0, 5.0)
    cheap = analysis.PlayerView(2, "Cheap", 2, "T2", "FWD", 6.0, 1, 9.5, 0.0, 9.5)
    plan = analysis.plan_transfers([1], {1: star, 2: cheap}, bank=0)[0].to_dict()
    assert plan["ep_gain"] == pytest.approx(-0.5) and plan["worth_it"] is False
    assert plan["score_gain"] == pytest.approx(4.5) and plan["differential"] is True


def test_planner_enforces_three_per_club_across_both_transfers():
    # I already own two Spurs players; buying two more Spurs players would make four.
    views = {v.id: v for v in [
        view(1, 3, "MID", 5.0, 5), view(2, 3, "MID", 5.0, 5),          # my Spurs
        view(3, 1, "MID", 5.0, 0), view(4, 2, "MID", 5.0, 0),          # my others
        view(10, 3, "MID", 5.0, 9), view(11, 3, "MID", 5.0, 8),        # two Spurs targets
    ]}
    plans = analysis.plan_transfers([1, 2, 3, 4], views, bank=0, free_transfers=2)
    for p in plans:
        spurs_in = sum(1 for _, i in p.swaps if i.team_id == 3)
        spurs_out = sum(1 for o, _ in p.swaps if o.team_id == 3)
        assert 2 + spurs_in - spurs_out <= 3
    assert any(len(p.swaps) == 1 for p in plans)  # one Spurs target is fine


def test_planner_counts_bench_swaps_less():
    views = {v.id: v for v in [view(1, 1, "MID", 5.0, 1), view(10, 2, "MID", 5.0, 5)]}
    full = analysis.plan_transfers([1], views, bank=0)[0]
    bench = analysis.plan_transfers([1], views, bank=0, bench_ids={1})[0]
    assert bench.ep_gain == pytest.approx(full.ep_gain * analysis.BENCH_WEIGHT)


def test_few_minutes_pull_rates_towards_the_typical_value():
    hot_rookie = element(50, "Rookie", 1, 4, 50, minutes=90, xg=1.5, xa=0.0)
    hot_regular = element(51, "Regular", 1, 4, 50, minutes=1800, xg=1.5, xa=0.0)
    rookie = analysis.expected_points_per_match(hot_rookie, played_gws=1)
    regular = analysis.expected_points_per_match(hot_regular, played_gws=20)
    assert rookie < regular  # same xG per 90, but the small sample is trusted less


# --------------------------------------------------------------------------
# service.py (whole report, no network)
# --------------------------------------------------------------------------
def test_team_fixtures_and_blank_and_double_gameweeks():
    fixtures = [
        {"event": 3, "team_h": 1, "team_a": 2, "team_h_difficulty": 2, "team_a_difficulty": 4},
        {"event": 3, "team_h": 3, "team_a": 4, "team_h_difficulty": 3, "team_a_difficulty": 3},
        # gameweek 4: team 1 plays twice (double), team 2 not at all (blank)
        {"event": 4, "team_h": 1, "team_a": 3, "team_h_difficulty": 2, "team_a_difficulty": 3},
        {"event": 4, "team_h": 4, "team_a": 1, "team_h_difficulty": 3, "team_a_difficulty": 5},
        {"event": 6, "team_h": 1, "team_a": 2, "team_h_difficulty": 2, "team_a_difficulty": 4},  # outside window
        {"event": None, "team_h": 2, "team_a": 3},  # not scheduled yet
    ]
    by_team = analysis.team_fixtures(fixtures, first_event=3, horizon=3)
    assert by_team[1][3] == [{"opponent": 2, "home": True, "difficulty": 2}]
    assert [m["home"] for m in by_team[1][4]] == [True, False]
    assert 5 not in by_team[1] and 6 not in by_team[1]

    special = analysis.special_gameweeks([1, 2, 3, 4], by_team, [3, 4, 5])
    assert special["double"] == {4: [1]}
    assert special["blank"] == {4: [2]}  # gameweek 5 has no fixtures at all -> not scheduled, not blank


def test_fixture_ticker_lists_each_club_once_easiest_first():
    views = analysis.build_player_views(
        BOOTSTRAP["elements"], BOOTSTRAP["teams"], {}, {}, 2, 0.5
    )
    by_team = analysis.team_fixtures(FIXTURES, first_event=3, horizon=2)
    names = {1: "ARS", 2: "CHE", 3: "TOT", 4: "LIV"}
    rows = analysis.fixture_ticker([views[1], views[2], views[5]], by_team, names, [3, 4])
    assert [r["team"] for r in rows] == ["ARS", "CHE"]  # difficulty 3 and 3, then by name
    ars = rows[0]
    assert ars["players"] == ["Keeper", "Template"]
    assert ars["gameweeks"][0]["matches"] == [{"opponent": "CHE", "home": True, "difficulty": 3}]
    assert ars["gameweeks"][1]["matches"] == []  # blank gameweek


def test_captain_options_safe_vs_differential():
    views = analysis.build_player_views(
        BOOTSTRAP["elements"], BOOTSTRAP["teams"], {1: [3], 3: [2], 4: [4]}, {}, 2, 0.5
    )
    squad = [1, 3, 4, 8, 99]  # Injured (8) and BenchGuy (99) cannot be captains
    plain = analysis.captain_options(squad, views, {}, uniqueness=0.5)
    assert [o.player.id for o in plain][0] == max(plain, key=lambda o: o.player.expected_points).player.id
    assert {o.player.id for o in plain} == {1, 3, 4}

    # Everybody captains the best player: his score drops, but never below zero.
    best = plain[0].player.id
    crowded = analysis.captain_options(squad, views, {best: 1.0}, uniqueness=1.0)
    assert crowded[0].player.id != best or len(crowded) == 1
    assert all(o.score >= 0 for o in crowded)
    assert crowded[[o.player.id for o in crowded].index(best)].rival_share == 1.0


def test_captain_share_counts_rival_captains():
    squads = [{"picks": [{"element": 4, "is_captain": True}]}, {"picks": [{"element": 4}]}]
    assert analysis.captain_of(squads[0]) == 4 and analysis.captain_of(squads[1]) is None
    assert analysis.captain_share([analysis.captain_of(s) for s in squads]) == {4: 0.5}
    assert analysis.captain_share([None, None]) == {}



def test_report_end_to_end():
    report = build_report(FakeClient(), league_id=1, entry_id=100, rivals=5)
    meta = report["meta"]
    assert meta["my_rank"] == 3
    assert [r["rank"] for r in meta["rivals"]] == [1, 2]  # only managers above me
    assert meta["squad_as_of_gw"] == 2
    assert meta["horizon_gws"] == [3, 4, 5]
    assert meta["bank"] == 2.5

    top = report["suggestions"][0]
    assert top["out"]["name"] in {"Meh", "Template"}  # weak or over-owned
    assert top["in"]["name"] == "Gem"  # good AND not owned by the rivals
    targets = [s["in"]["name"] for s in report["suggestions"]]
    assert len(targets) == len(set(targets))  # every target player only once
    names_in = {s["in"]["name"] for s in report["suggestions"]}
    assert "Pricey" not in names_in and "Injured" not in names_in  # budget / availability

    coverage = {row["name"]: row for row in report["coverage"]}
    assert coverage["Template"]["rivals_owning"] == 2 and coverage["Template"]["mine"]
    assert coverage["Star"]["share"] == 1.0 and not coverage["Star"]["mine"]
    assert "BenchGuy" not in coverage  # rival's bench player is not counted

    captaincy = report["captaincy"]
    assert captaincy["gameweek"] == 3
    assert {o["name"] for o in captaincy["options"]} <= {"Template", "Meh", "Keeper", "Defender"}
    assert captaincy["safe"] is not None and captaincy["differential"] is not None
    assert captaincy["safe"]["captain_points"] == pytest.approx(
        2 * captaincy["safe"]["expected_points"], abs=0.02  # both values are rounded
    )
    fixtures = report["fixtures"]
    assert fixtures["gameweeks"] == [3, 4, 5]
    assert {row["team"] for row in fixtures["ticker"]} == {"ARS", "CHE"}  # clubs of my squad
    assert fixtures["blank"] == {} and fixtures["double"] == {}

    gem = next(d for d in report["differentials"] if d["name"] == "Gem")
    assert gem["ownership"] == 0.0
    assert all(d["name"] != "Star" for d in report["differentials"])  # too widely owned


def test_report_mode_above_picks_the_managers_directly_above():
    report = build_report(FakeClient(), 1, 100, rivals=1, mode="above")
    assert [r["rank"] for r in report["meta"]["rivals"]] == [2]


def test_report_when_leading_and_when_entry_missing():
    leading = [{"entry": 100, "entry_name": "Me", "player_name": "Me", "rank": 1, "total": 99}]
    report = build_report(FakeClient(standings=leading), 1, 100)
    assert report["meta"]["rivals"] == []
    assert any("already first" in note for note in report["meta"]["notes"])

    with pytest.raises(ReportError):
        build_report(FakeClient(), 1, 999)


# --------------------------------------------------------------------------
# client.py (HTTP mocked with httpx.MockTransport)
# --------------------------------------------------------------------------
def make_client(handler, cache=None):
    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://fantasy.premierleague.com/api/"
    )
    return FplClient(cache=cache, http=http, sleep=lambda seconds: None, retries=2)


def test_client_retries_on_server_error_then_succeeds():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={"ok": True})

    assert make_client(handler).bootstrap() == {"ok": True}
    assert len(calls) == 2


def test_client_gives_up_and_reports_404():
    with pytest.raises(FplApiError):
        make_client(lambda r: httpx.Response(503)).bootstrap()
    with pytest.raises(FplApiError, match="404"):
        make_client(lambda r: httpx.Response(404)).entry_picks(1, 5)


def test_client_collects_all_standings_pages():
    def handler(request):
        page = int(request.url.params["page_standings"])
        return httpx.Response(
            200, json={"standings": {"has_next": page < 2, "results": [{"entry": page}]}}
        )

    assert make_client(handler).league_standings(1) == [{"entry": 1}, {"entry": 2}]


def test_client_uses_cache_for_repeated_calls():
    hits = []

    def handler(request):
        hits.append(1)
        return httpx.Response(200, json={"events": []})

    client = make_client(handler, cache=DbCache(SessionLocal))
    client.bootstrap()
    client.bootstrap()
    assert len(hits) == 1


def test_db_cache_respects_ttl():
    cache = DbCache(SessionLocal)
    assert cache.get("k", 60) is None
    cache.set("k", {"a": 1})
    assert cache.get("k", 60) == {"a": 1}
    assert cache.get("k", 0) is None  # older than 0 seconds -> expired
    cache.set("k", [1, 2])  # lists work too (upsert)
    assert cache.get("k", 60) == [1, 2]


# --------------------------------------------------------------------------
# live table
# --------------------------------------------------------------------------
LIVE_STANDINGS = [
    {"entry": 200, "entry_name": "Leaders FC", "player_name": "A", "rank": 1, "total": 100, "event_total": 20},
    {"entry": 300, "entry_name": "Second FC", "player_name": "B", "rank": 2, "total": 90, "event_total": 10},
    {"entry": 100, "entry_name": "BANDZIORY", "player_name": "Me", "rank": 3, "total": 80, "event_total": 30},
    {"entry": 400, "entry_name": "Below FC", "player_name": "C", "rank": 4, "total": 70, "event_total": 5},
]


class LiveFakeClient(FakeClient):
    """Players score: 1 -> 10, 4 -> 6, 5 -> 2, 6 -> 1, 3 -> 5; everyone else 0."""

    def __init__(self):
        super().__init__(LIVE_STANDINGS)

    def live(self, event, ttl=60):
        points = {1: 10, 4: 6, 5: 2, 6: 1, 3: 5}
        return {"elements": [{"id": i, "stats": {"total_points": p}} for i, p in points.items()]}

    def entry_picks(self, entry_id, event, ttl=900):
        def squad(multipliers, cost=0, chip=None):
            return {
                "active_chip": chip,
                "entry_history": {"event_transfers_cost": cost},
                "picks": [{"element": e, "multiplier": m} for e, m in multipliers.items()],
            }

        return {
            100: squad({1: 2, 2: 1, 5: 1, 6: 1}, cost=4),  # captain Template, one -4 hit
            200: squad({1: 1, 4: 1, 5: 1, 99: 0}),  # bench player scores nothing
            300: squad({1: 1, 4: 1, 6: 1}, chip="3xc"),
            400: squad({3: 1}),
        }[entry_id]


def test_live_table_orders_by_live_total_and_applies_captain_and_transfer_cost():
    live = build_live(LiveFakeClient(), 1, 100, limit=2)
    assert [r["entry"] for r in live["rows"]] == [200, 300, 100]  # 400 is outside the top 2
    leader, second, me = live["rows"]
    assert me["gw_points"] == 19  # (10*2 + 0 + 2 + 1) - 4
    assert me["live_total"] == 69  # (80 - 30) + 19
    assert leader["live_total"] == 98 and leader["gap_to_me"] == 29
    assert second["chip"] == "3xc"
    assert me["is_me"] and me["live_rank"] == 3 and me["rank_change"] == 0
    assert live["gameweek"] == 2 and live["league_size"] == 4


def test_live_table_can_change_the_order():
    # A big captain haul for me flips places with the leader.
    class Hot(LiveFakeClient):
        def live(self, event, ttl=60):
            data = super().live(event, ttl)
            data["elements"][0]["stats"]["total_points"] = 40  # element 1 explodes
            return data

    rows = build_live(Hot(), 1, 100, limit=3)["rows"]
    assert rows[0]["entry"] == 100 and rows[0]["rank_change"] == 2


def test_live_table_errors():
    with pytest.raises(ReportError):
        build_live(LiveFakeClient(), 1, 999)


# --------------------------------------------------------------------------
# rivals view
# --------------------------------------------------------------------------
class RivalsFakeClient(FakeClient):
    def entry_history(self, entry_id, ttl=900):
        return {
            200: {
                "chips": [{"name": "wildcard", "event": 1}],
                "current": [{"event": 1, "event_transfers_cost": 8}, {"event": 2, "event_transfers_cost": 4}],
            },
        }.get(entry_id, {"chips": [], "current": []})

    def entry_transfers(self, entry_id, ttl=900):
        return {
            200: [
                {"event": 1, "element_in": 1, "element_out": 2, "time": "a"},  # too old for recent=1
                {"event": 2, "element_in": 4, "element_out": 2, "time": "b"},
            ],
            300: [{"event": 2, "element_in": 4, "element_out": 6, "time": "c"}],
        }.get(entry_id, [])


def test_rivals_view_shows_captain_chips_and_recent_transfers():
    view = build_rivals(RivalsFakeClient(), 1, 100, recent=1)
    by_entry = {r["entry"]: r for r in view["rivals"]}
    assert set(by_entry) == {200, 300}  # the two managers above me
    leader = by_entry[200]
    assert leader["captain"] == {"id": 4, "name": "Star"}
    assert by_entry[300]["captain"] is None  # no captain flag in the data
    assert leader["chips_used"] == [{"name": "wildcard", "event": 1}]
    assert leader["transfers"] == [{"event": 2, "in": "Star", "out": "Meh"}]  # GW1 is outside recent=1
    assert leader["hits"] == 4
    # Both rivals bought Star in the window, so he is the top "trending in" player.
    assert view["trending_in"][0] == {"name": "Star", "rivals": 2}
    assert {t["name"] for t in view["trending_out"]} == {"Meh", "Defender"}


def test_rivals_view_longer_window_and_errors():
    view = build_rivals(RivalsFakeClient(), 1, 100, recent=2)
    leader = next(r for r in view["rivals"] if r["entry"] == 200)
    assert len(leader["transfers"]) == 2 and leader["hits"] == 12
    assert leader["transfers"][0]["event"] == 2  # newest first
    with pytest.raises(ReportError):
        build_rivals(RivalsFakeClient(), 1, 999)


def test_rivals_view_skips_a_rival_it_cannot_read():
    class Broken(RivalsFakeClient):
        def entry_transfers(self, entry_id, ttl=900):
            if entry_id == 300:
                raise FplApiError("boom")
            return super().entry_transfers(entry_id, ttl)

    assert [r["entry"] for r in build_rivals(Broken(), 1, 100)["rivals"]] == [200]


# --------------------------------------------------------------------------
# router.py
# --------------------------------------------------------------------------
@pytest.fixture
def api():
    app.dependency_overrides[get_fpl_client] = lambda: FakeClient()
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_endpoint_returns_report(api):
    response = api.get("/api/league/report", params={"league_id": 1, "entry_id": 100})
    assert response.status_code == 200
    assert response.json()["suggestions"][0]["in"]["name"] == "Gem"
    assert {"risers", "fallers", "my_falling"} <= set(response.json()["prices"])
    planner = response.json()["planner"]
    assert planner["free_transfers"] == 1 and planner["hit_cost"] == 4
    too_many = {"league_id": 1, "entry_id": 100, "free_transfers": 9}
    assert api.get("/api/league/report", params=too_many).status_code == 422


def test_live_endpoint(api):
    app.dependency_overrides[get_fpl_client] = lambda: LiveFakeClient()
    response = api.get("/api/league/live", params={"league_id": 1, "entry_id": 100})
    assert response.status_code == 200
    assert response.json()["rows"][0]["entry"] == 200
    assert api.get("/api/league/live", params={"league_id": 1, "entry_id": 999}).status_code == 400
    assert api.get("/api/league/live", params={"league_id": 1, "entry_id": 100, "limit": 0}).status_code == 422


def test_rivals_endpoint(api):
    app.dependency_overrides[get_fpl_client] = lambda: RivalsFakeClient()
    response = api.get("/api/league/rivals", params={"league_id": 1, "entry_id": 100})
    assert response.status_code == 200
    assert len(response.json()["rivals"]) == 2
    assert api.get("/api/league/rivals", params={"league_id": 1, "entry_id": 999}).status_code == 400
    assert api.get("/api/league/rivals", params={"league_id": 1, "entry_id": 100, "recent": 9}).status_code == 422


def test_endpoint_uses_environment_defaults(api, monkeypatch):
    monkeypatch.setenv("FPL_LEAGUE_ID", "1")
    monkeypatch.setenv("FPL_ENTRY_ID", "100")
    assert api.get("/api/league/report").status_code == 200


def test_endpoint_errors(api, monkeypatch):
    monkeypatch.delenv("FPL_LEAGUE_ID", raising=False)
    monkeypatch.delenv("FPL_ENTRY_ID", raising=False)
    assert api.get("/api/league/report").status_code == 422  # ids missing
    assert api.get("/api/league/report", params={"league_id": 1, "entry_id": 999}).status_code == 400
    assert api.get("/api/league/report", params={"league_id": 1, "entry_id": 100, "uniqueness": 2}).status_code == 422
