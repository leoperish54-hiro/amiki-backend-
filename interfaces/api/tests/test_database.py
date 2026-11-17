from fastapi.testclient import TestClient
from interfaces.api.tests.uosa_test_client import UosaTestClient

from interfaces.api.server.app import app
from interfaces.api.routes import database as database_route

OWNER_TOKEN = "uosa-test-owner-sentinel-not-a-real-bearer-token"

client = UosaTestClient(app, OWNER_TOKEN, auto_owner_auth=True)
anonymous_client = TestClient(app)


def test_database_status_requires_uosa_session():
    response = anonymous_client.get("/api/v1/database/status")

    assert response.status_code == 401


def test_database_status(monkeypatch):
    def fake_database_status():
        return {
            "overall_status": "healthy",
            "persistent_memory": {
                "enabled": True,
                "count": 3,
                "categories": 2,
                "status": "healthy",
            },
            "current_session": {
                "enabled": True,
                "active": True,
                "message_count": 4,
                "max_messages": 20,
                "status": "healthy",
            },
            "attachments": {
                "available": True,
                "persistent": False,
                "status": "pipeline_only",
            },
            "chat_logs": {
                "available": False,
                "persistent": False,
                "status": "not_configured",
            },
        }

    monkeypatch.setattr(
        database_route,
        "build_database_status",
        fake_database_status,
    )

    response = client.get("/api/v1/database/status")

    assert response.status_code == 200
    assert response.json() == {
        "overall_status": "healthy",
        "persistent_memory": {
            "enabled": True,
            "count": 3,
            "categories": 2,
            "status": "healthy",
        },
        "current_session": {
            "enabled": True,
            "active": True,
            "message_count": 4,
            "max_messages": 20,
            "status": "healthy",
        },
        "attachments": {
            "available": True,
            "persistent": False,
            "status": "pipeline_only",
        },
        "chat_logs": {
            "available": False,
            "persistent": False,
            "status": "not_configured",
        },
    }


def test_database_status_uses_real_store():
    response = client.get("/api/v1/database/status")

    assert response.status_code == 200

    data = response.json()

    assert data["overall_status"] in {
        "healthy",
        "degraded",
        "disabled",
    }

    assert data["persistent_memory"]["enabled"] is True
    assert data["persistent_memory"]["count"] >= 0
    assert data["persistent_memory"]["categories"] >= 0

    assert data["current_session"]["enabled"] is True
    assert data["current_session"]["message_count"] >= 0
    assert data["current_session"]["max_messages"] == 20

    assert data["attachments"] == {
        "available": True,
        "persistent": False,
        "status": "pipeline_only",
    }

    assert data["chat_logs"] == {
        "available": False,
        "persistent": False,
        "status": "not_configured",
    }
