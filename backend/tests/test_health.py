"""检查健康接口对外提供的行为。"""

from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_ok() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok"}


def test_health_rejects_post() -> None:
    with TestClient(app) as client:
        response = client.post("/health", json={"status": "changed"})

    assert response.status_code == 405
