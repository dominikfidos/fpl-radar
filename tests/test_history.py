import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import SessionLocal
from app.league import history
from app.league.client import FplApiError
from app.league.router import get_fpl_client
from app.league.service import ReportError
from app.main import app
from app.models import RankHistory

STANDINGS = [
    {"entry": 200, "entry_name": "Leaders FC", "player_name": "A", "rank": 1, "total": 100},
    {"entry": 300, "entry_name": "Second FC", "player_name": "B", "rank": 2, "total": 90},
    {"entry": 100, "entry_name": "BANDZIORY", "player_name": "Me", "rank": 3, "total": 80},
    {"entry": 400, "entry_name": "Below FC", "player_name": "C", "rank": 4, "total": 70},
]

# total points after each gameweek; gameweek 3 is not finished yet and must be ignored
TOTALS = {
    200: {1: 50, 2: 100, 3: 130},
    300: {1: 60, 2: 90, 3: 120},
    100: {1: 40, 2: 80, 3: 110},
    400: {1: 30, 2: 70, 3: 100},
}


class HistoryClient:
    def __init__(self, broken=()):
        self.history_calls = 0
        self.broken = set(broken)

    def bootstrap(self):
        return {"events": [
            {"id": 1, "finished": True},
            {"id": 2, "finished": True},
            {"id": 3, "finished": False, "is_current": True},
        ]}

    def league_standings(self, league_id, max_pages=10):
        return STANDINGS

    def entry_history(self, entry_id, ttl=900):
        self.history_calls += 1
        if entry_id in self.broken:
            raise FplApiError("boom")
        return {"current": [
            {"event": g, "total_points": total, "points": 10} for g, total in TOTALS[entry_id].items()
        ]}


def stored_rows():
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(RankHistory))


def test_compute_ranks_gives_equal_points_the_same_rank():
    totals = {1: {1: 10, 2: 30}, 2: {1: 10}, 3: {1: 5, 2: 30}}
    ranks = history.compute_ranks(totals, [1, 2])
    assert ranks[1] == {1: 1, 2: 1, 3: 3}  # 1 and 2 tie, so the next place is 3rd
    assert ranks[2] == {1: 1, 3: 1}  # manager 2 has no data for gameweek 2


def test_record_stores_finished_gameweeks_with_correct_ranks():
    assert history.record_rank_history(SessionLocal, HistoryClient(), 1, 100) == 8  # 4 managers x 2 gameweeks
    with SessionLocal() as db:
        rows = {(r.event, r.entry_id): r for r in db.scalars(select(RankHistory))}
    assert {event for event, _ in rows} == {1, 2}  # the unfinished gameweek 3 is not stored
    assert [rows[(1, e)].rank for e in (300, 200, 100, 400)] == [1, 2, 3, 4]
    assert [rows[(2, e)].rank for e in (200, 300, 100, 400)] == [1, 2, 3, 4]
    assert rows[(2, 100)].total_points == 80 and rows[(2, 100)].team == "BANDZIORY"


def test_record_is_idempotent_and_skips_fpl_when_up_to_date():
    client = HistoryClient()
    history.record_rank_history(SessionLocal, client, 1, 100)
    calls = client.history_calls
    assert history.record_rank_history(SessionLocal, client, 1, 100) == 0
    assert client.history_calls == calls  # nothing was asked from FPL the second time
    assert stored_rows() == 8  # and no duplicates

    # A smaller tracked group is already covered by what is stored, so nothing changes.
    assert history.record_rank_history(SessionLocal, client, 1, 100, tracked=1) == 0
    assert stored_rows() == 8


def test_record_picks_up_a_newly_finished_gameweek():
    history.record_rank_history(SessionLocal, HistoryClient(), 1, 100)

    class Later(HistoryClient):
        def bootstrap(self):
            return {"events": [{"id": g, "finished": True} for g in (1, 2, 3)]}

    assert history.record_rank_history(SessionLocal, Later(), 1, 100) == 12  # everything is rebuilt
    assert stored_rows() == 12


def test_only_the_top_and_me_are_tracked():
    history.record_rank_history(SessionLocal, HistoryClient(), 1, 100, tracked=1)
    with SessionLocal() as db:
        assert set(db.scalars(select(RankHistory.entry_id))) == {200, 100}  # the leader and me


def test_unreadable_rival_is_skipped_but_unreadable_me_is_an_error():
    history.record_rank_history(SessionLocal, HistoryClient(broken={300}), 1, 100)
    with SessionLocal() as db:
        assert 300 not in set(db.scalars(select(RankHistory.entry_id)))
    with pytest.raises(FplApiError):
        history.record_rank_history(SessionLocal, HistoryClient(broken={100}), 2, 100)
    with pytest.raises(ReportError):
        history.record_rank_history(SessionLocal, HistoryClient(), 3, 999)


def test_load_shapes_one_series_per_manager():
    history.record_rank_history(SessionLocal, HistoryClient(), 1, 100)
    data = history.load_rank_history(SessionLocal, 1, 100, show=2)
    assert data["gameweeks"] == [1, 2] and data["tracked"] == 4
    assert [s["entry"] for s in data["series"]] == [200, 300, 100]  # best two now, plus me
    leader, _, me = data["series"]
    assert leader["ranks"] == [2, 1] and leader["totals"] == [50, 100]
    assert me["is_me"] and me["ranks"] == [3, 3] and not leader["is_me"]
    assert history.load_rank_history(SessionLocal, 999, 100)["series"] == []  # unknown league


@pytest.fixture
def api():
    app.dependency_overrides[get_fpl_client] = lambda: HistoryClient()
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_history_endpoint(api):
    response = api.get("/api/league/history", params={"league_id": 1, "entry_id": 100})
    assert response.status_code == 200
    body = response.json()
    assert body["gameweeks"] == [1, 2] and body["rows_written"] == 8
    assert api.get("/api/league/history", params={"league_id": 1, "entry_id": 100}).json()["rows_written"] == 0
    assert api.get("/api/league/history", params={"league_id": 1, "entry_id": 999}).status_code == 400
    assert api.get("/api/league/history", params={"league_id": 1, "entry_id": 100, "show": 0}).status_code == 422
