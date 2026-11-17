import pytest
from fastapi.testclient import TestClient

from interfaces.api.security import require_api_auth
from interfaces.api.server.app import app


client = TestClient(app)
TEST_TOKEN = "test-core-api-token-" + ("x" * 48)


def _enable_real_auth():
    app.dependency_overrides.pop(require_api_auth, None)


def test_health_is_the_only_public_api_route(monkeypatch):
    _enable_real_auth()
    monkeypatch.delenv("AMIKI_API_TOKEN", raising=False)

    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/capabilities").status_code == 503


def test_protected_route_rejects_missing_bearer_token(monkeypatch):
    _enable_real_auth()
    monkeypatch.setenv("AMIKI_API_TOKEN", TEST_TOKEN)

    response = client.get("/api/v1/capabilities")

    assert response.status_code == 401
    assert response.json()["detail"] == "api_auth_required"
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("authorization", [
    "Basic " + TEST_TOKEN,
    "Bearer wrong-token",
    "Bearer ",
    "bearer wrong-token",
])
def test_protected_route_rejects_malformed_or_invalid_token(monkeypatch, authorization):
    _enable_real_auth()
    monkeypatch.setenv("AMIKI_API_TOKEN", TEST_TOKEN)

    response = client.get(
        "/api/v1/capabilities",
        headers={"Authorization": authorization},
    )

    assert response.status_code == 401


def test_valid_bearer_token_reaches_protected_route(monkeypatch):
    _enable_real_auth()
    monkeypatch.setenv("AMIKI_API_TOKEN", TEST_TOKEN)

    response = client.get(
        "/api/v1/capabilities",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )

    assert response.status_code == 200


def test_approved_flag_does_not_replace_api_auth(monkeypatch):
    _enable_real_auth()
    monkeypatch.setenv("AMIKI_API_TOKEN", TEST_TOKEN)

    response = client.post(
        "/api/v1/workspace/execute",
        json={"path": "test_read_sample.txt", "approved": True},
    )

    assert response.status_code == 401


def test_short_server_token_fails_closed(monkeypatch):
    _enable_real_auth()
    monkeypatch.setenv("AMIKI_API_TOKEN", "too-short")

    response = client.get(
        "/api/v1/capabilities",
        headers={"Authorization": "Bearer too-short"},
    )

    assert response.status_code == 503
