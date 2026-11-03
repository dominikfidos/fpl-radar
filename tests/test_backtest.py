import pytest
from fastapi.testclient import TestClient

from app.league import analysis, backtest
from app.league.router import get_fpl_client
from app.league.service import ReportError, build_backtest
from app.main import app


def item(pid, minutes, points, xg=0.0, xa=0.0, xgc=0.0):
    return {"id": pid, "stats": {"minutes": minutes, "total_points": points, "expected_goals": str(xg),
                                 "expected_assists": str(xa), "expected_goals_conceded": str(xgc)}}


ELEMENTS = [
    {"id": 1, "element_type": 4, "team": 1},
    {"id": 2, "element_type": 2, "team": 2},
    {"id": 3, "element_type": 3, "team": 3},  # never plays before gameweek 4
]
FIXTURES = [
    {"event": gw, "team_h": 1, "team_a": 2, "team_h_difficulty": 2, "team_a_difficulty": 4} for gw in (1, 2, 3)
] + [{"event": 4, "team_h": 1, "team_a": 3, "team_h_difficulty": 2, "team_a_difficulty": 4}]  # team 2 has a blank


def live_data():
    return {
        1: [item(1, 90, 6, xg=0.5), item(2, 90, 2, xgc=1.0), item(3, 0, 0)],
        2: [item(1, 90, 2, xg=0.3), item(2, 90, 6, xgc=0.5), item(3, 0, 0)],
        3: [item(1, 90, 100, xg=0.4), item(2, 0, 0), item(3, 0, 0)],  # a huge score that must stay hidden
        4: [item(1, 90, 5), item(2, 90, 1), item(3, 90, 3)],
    }


def test_rows_never_see_the_gameweek_they_predict():
    rows = backtest.build_rows(live_data(), ELEMENTS, FIXTURES, targets=[3, 4])
    gw3 = {r.player_id: r for r in rows[3]}
    assert gw3[1].points_so_far == 8 and gw3[1].minutes == 180  # gameweeks 1 and 2 only
    assert gw3[1].xg == pytest.approx(0.8) and gw3[1].actual == 100  # the real score is only the answer key
    assert gw3[1].played_gws == 2
    gw4 = {r.player_id: r for r in rows[4]}
    assert gw4[1].points_so_far == 108 and gw4[1].played_gws == 3  # now gameweek 3 is allowed


def test_players_without_earlier_minutes_are_left_out_and_fixtures_come_from_the_target_week():
    rows = backtest.build_rows(live_data(), ELEMENTS, FIXTURES, targets=[4])
    ids = {r.player_id: r for r in rows[4]}
    assert 3 not in ids  # never played, nothing to predict from
    assert ids[1].difficulties == (2,)
    assert ids[2].difficulties == ()  # team 2 has a blank in gameweek 4
    assert backtest.model_prediction(ids[2]) == 0.0
    assert backtest.baseline_prediction(ids[2]) > 0  # the naive guess ignores the blank


def test_pearson():
    assert backtest.pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert backtest.pearson([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    assert backtest.pearson([1, 1, 1], [1, 2, 3]) is None  # no variation, no correlation
    assert backtest.pearson([1], [1]) is None


def make_rows(n=12, gw=5):
    return {gw: [
        backtest.Row(i, 3, 900, i * 0.5, 1.0, 10.0, points_so_far=i, played_gws=4, difficulties=(3,), actual=float(i))
        for i in range(1, n + 1)
    ]}


def test_a_perfect_predictor_scores_perfectly():
    rows = make_rows()
    result = backtest.evaluate(rows, lambda r: r.actual)
    assert result["corr"] == pytest.approx(1.0) and result["mae"] == 0 and result["bias"] == 0
    assert result["top10"] == pytest.approx(sum(range(3, 13)) / 10)  # the ten highest real scores
    worst = backtest.evaluate(rows, lambda r: -r.actual)
    assert worst["corr"] == pytest.approx(-1.0) and worst["top10"] == pytest.approx(sum(range(1, 11)) / 10)


def test_tunable_constants_really_change_the_prediction():
    rookie = backtest.Row(1, 4, 90, xg=1.5, xa=0.0, xgc=0.0, points_so_far=5, played_gws=1,
                          difficulties=(1,), actual=0)
    hard = backtest.Row(1, 4, 900, xg=3.0, xa=0.0, xgc=0.0, points_so_far=5, played_gws=10,
                        difficulties=(5,), actual=0)
    easy = backtest.Row(1, 4, 900, xg=3.0, xa=0.0, xgc=0.0, points_so_far=5, played_gws=10,
                        difficulties=(1,), actual=0)
    # Trusting a tiny sample (prior 0) predicts more for a hot rookie than shrinking towards typical.
    assert backtest.model_prediction(rookie, prior_minutes=0) > backtest.model_prediction(rookie, prior_minutes=1800)
    # Slope 0 ignores the fixture; the default does not.
    assert backtest.model_prediction(easy, slope=0) == pytest.approx(backtest.model_prediction(hard, slope=0))
    assert backtest.model_prediction(easy) > backtest.model_prediction(hard)
    # The defaults reproduce the app's own function exactly.
    el = {"element_type": 4, "minutes": 900, "expected_goals": 3.0, "expected_assists": 0.0,
          "expected_goals_conceded": 0.0}
    assert backtest.model_prediction(easy) == pytest.approx(analysis.expected_points(el, [1], 10))


def test_run_backtest_report_shape():
    report = backtest.run_backtest(make_rows())
    assert set(report) >= {"summary", "per_gw", "sweeps", "current"}
    assert report["per_gw"][0]["gw"] == 5 and report["per_gw"][0]["n"] == 12
    for name in ("prior_minutes", "fixture_slope"):
        rows = report["sweeps"][name]
        assert sum(r["is_current"] for r in rows) == 1  # exactly one row is the setting in use
        assert sum(r["is_best"] for r in rows) == 1
    assert report["current"] == {"prior_minutes": analysis.PRIOR_MINUTES, "fixture_slope": analysis.FIXTURE_SLOPE}


class BacktestClient:
    def __init__(self, finished=6):
        self.finished = finished

    def bootstrap(self):
        return {
            "events": [{"id": g, "finished": g <= self.finished} for g in range(1, 9)],
            "elements": [{"id": p, "element_type": 3, "team": 1 + p % 2} for p in range(1, 13)],
        }

    def live(self, event, ttl=60):
        return {"elements": [item(p, 90, (p * event) % 7, xg=p * 0.05, xa=0.1, xgc=1.0) for p in range(1, 13)]}

    def all_fixtures(self, ttl=3600):
        return [{"event": g, "team_h": 1, "team_a": 2, "team_h_difficulty": 2, "team_a_difficulty": 4}
                for g in range(1, 9)]


def test_build_backtest_runs_on_finished_gameweeks_only():
    report = build_backtest(BacktestClient(finished=6))
    assert report["gameweeks"] == [4, 5, 6] and report["warmup"] == 3  # first three weeks are warm-up
    assert report["summary"]["model"]["n"] == 36 and report["notes"]
    with pytest.raises(ReportError):
        build_backtest(BacktestClient(finished=3))  # nothing left to test after the warm-up


@pytest.fixture
def api():
    app.dependency_overrides[get_fpl_client] = lambda: BacktestClient()
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_backtest_endpoint(api):
    response = api.get("/api/league/backtest")
    assert response.status_code == 200 and response.json()["gameweeks"] == [4, 5, 6]
    app.dependency_overrides[get_fpl_client] = lambda: BacktestClient(finished=2)
    assert api.get("/api/league/backtest").status_code == 400
