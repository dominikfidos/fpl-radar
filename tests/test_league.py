import httpx
import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.league import analysis
from app.league.cache import DbCache
from app.league.client import FplApiError, FplClient
from app.league.router import get_fpl_client
from app.league.service import ReportError, build_report
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


def test_few_minutes_pull_rates_towards_the_typical_value():
    hot_rookie = element(50, "Rookie", 1, 4, 50, minutes=90, xg=1.5, xa=0.0)
    hot_regular = element(51, "Regular", 1, 4, 50, minutes=1800, xg=1.5, xa=0.0)
    rookie = analysis.expected_points_per_match(hot_rookie, played_gws=1)
    regular = analysis.expected_points_per_match(hot_regular, played_gws=20)
    assert rookie < regular  # same xG per 90, but the small sample is trusted less


# --------------------------------------------------------------------------
# service.py (whole report, no network)
# --------------------------------------------------------------------------
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
