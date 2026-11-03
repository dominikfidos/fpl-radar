from fastapi.testclient import TestClient

from app.main import app


def test_metrics_endpoint_exposes_request_counters(monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    with TestClient(app) as client:
        client.get("/health")
        client.get("/api/players/12345/history")  # unknown player -> 404
        body = client.get("/metrics").text
    assert 'http_requests_total{method="GET",path="/health",status="200"}' in body
    # The label is the route template, not the real URL (keeps the metric count small).
    assert 'path="/api/players/{player_id}/history"' in body
    assert "http_request_duration_seconds_bucket" in body
    assert "fpl_api_requests_total" in body or "rate_limit_rejections_total" in body


def test_metrics_can_be_protected_with_a_token(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "s3cret")
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
