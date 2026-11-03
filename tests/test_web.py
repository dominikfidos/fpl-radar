from fastapi.testclient import TestClient

from app import ratelimit
from app.main import app


def test_index_page_is_served():
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "FPL" in response.text and "/api/league/report" in response.text


def test_report_endpoint_is_rate_limited(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.delenv("FPL_LEAGUE_ID", raising=False)
    monkeypatch.delenv("FPL_ENTRY_ID", raising=False)
    ratelimit._limiters.clear()
    with TestClient(app) as client:
        # No ids given -> 422, but every call still counts towards the limit.
        assert client.get("/api/league/report").status_code == 422
        assert client.get("/api/league/report").status_code == 422
        blocked = client.get("/api/league/report")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    ratelimit._limiters.clear()
