from fastapi.testclient import TestClient

from interfaces.api.server.app import app


client = TestClient(app)


def test_health():
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "amiki-api",
    }
