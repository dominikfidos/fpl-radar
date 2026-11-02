import copy

from fastapi.testclient import TestClient

from app.collector import save_snapshot
from app.database import SessionLocal
from app.main import app

# A tiny imitation of what the FPL API returns.
SAMPLE = {
    "teams": [{"id": 1, "name": "Arsenal"}, {"id": 2, "name": "Chelsea"}],
    "elements": [
        {"id": 10, "web_name": "Saka", "team": 1, "element_type": 3, "now_cost": 100,
         "form": "7.5", "total_points": 50, "selected_by_percent": "40.2"},
        {"id": 11, "web_name": "Palmer", "team": 2, "element_type": 3, "now_cost": 110,
         "form": "6.0", "total_points": 55, "selected_by_percent": "35.0"},
        {"id": 12, "web_name": "Raya", "team": 1, "element_type": 1, "now_cost": 55,
         "form": "3.0", "total_points": 20, "selected_by_percent": "12.0"},
    ],
}


def store(data):
    with SessionLocal() as db:
        return save_snapshot(db, data)


def test_health():
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_empty_database_returns_empty_lists():
    with TestClient(app) as client:
        assert client.get("/api/players/top").json() == []
        assert client.get("/api/price-changes").json() == []


def test_collector_saves_every_player():
    assert store(SAMPLE) == 3


def test_top_players_sorted_by_form():
    store(SAMPLE)
    with TestClient(app) as client:
        players = client.get("/api/players/top").json()
        assert [p["name"] for p in players] == ["Saka", "Palmer", "Raya"]
        assert players[0]["price"] == 10.0
        assert players[0]["team"] == "Arsenal"


def test_top_players_position_filter_and_validation():
    store(SAMPLE)
    with TestClient(app) as client:
        goalkeepers = client.get("/api/players/top", params={"position": "GKP"}).json()
        assert [p["name"] for p in goalkeepers] == ["Raya"]
        assert client.get("/api/players/top", params={"position": "XXX"}).status_code == 422


def test_player_history():
    store(SAMPLE)
    store(SAMPLE)
    with TestClient(app) as client:
        history = client.get("/api/players/10/history").json()
        assert len(history) == 2
        assert client.get("/api/players/999/history").status_code == 404


def test_price_changes():
    store(SAMPLE)
    with TestClient(app) as client:
        assert client.get("/api/price-changes").json() == []  # only one snapshot so far

        later = copy.deepcopy(SAMPLE)
        later["elements"][0]["now_cost"] = 101  # Saka gets 0.1m more expensive
        store(later)

        changes = client.get("/api/price-changes").json()
        assert len(changes) == 1
        assert changes[0]["name"] == "Saka"
        assert changes[0]["change"] == 0.1
