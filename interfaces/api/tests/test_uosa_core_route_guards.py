import pytest
from fastapi.testclient import TestClient

from interfaces.api.server.app import app


client = TestClient(app)


@pytest.fixture
def isolated_uosa_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AMIKI_UOSA_DB_PATH", str(tmp_path / "uosa.sqlite3"))
    monkeypatch.setenv("AMIKI_UOSA_SETUP_CODE_FILE", str(tmp_path / "uosa-setup-code"))
    monkeypatch.delenv("AMIKI_OWNER_REVIEW_TOKEN", raising=False)
    yield


def test_health_and_static_capabilities_remain_public(isolated_uosa_state):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/capabilities").status_code == 200


def test_system_state_requires_an_enrolled_uosa_session(isolated_uosa_state):
    response = client.get("/api/v1/system-state")

    assert response.status_code == 401


def test_chat_requires_an_enrolled_uosa_session(isolated_uosa_state):
    response = client.post(
        "/api/v1/chat",
        json={"message": "UOSA route guard test"},
    )

    assert response.status_code == 401
