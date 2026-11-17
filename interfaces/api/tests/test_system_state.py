from fastapi.testclient import TestClient
from interfaces.api.tests.uosa_test_client import UosaTestClient

from interfaces.api.server.app import app

OWNER_TOKEN = "uosa-test-owner-sentinel-not-a-real-bearer-token"


client = UosaTestClient(app, OWNER_TOKEN, auto_owner_auth=True)


def test_system_state():
    response = client.get("/api/v1/system-state")

    assert response.status_code == 200

    data = response.json()

    assert data["identity"]["name"] == "Amiki"
    assert "version" in data["identity"]
    assert "role" in data["identity"]

    assert data["runtime"]["api_status"] == "ok"
    assert data["runtime"]["state"] in {"idle", "processing", "error"}

    assert data["capabilities"] == {
        "analysis": True,
        "university": True,
        "workspace": True,
        "noctis_requiem": True,
        "web": True,
        "device": True,
    }

    assert "gemini" in data["providers"]
    assert "openrouter" in data["providers"]

    assert data["device"]["status"] in {"available", "partial", "unavailable"}
    assert data["device"]["adapter"] == "termux_api"
    assert isinstance(data["device"]["read_only_actions"], dict)
    assert isinstance(data["device"]["confirmed_actions"], dict)
    assert data["device"]["bridge_connection"] == "not_observed"

    for provider in data["providers"].values():
        assert "configured" in provider
        assert "available" in provider
        assert "active" in provider

    assert "components" in data["architecture"]
    assert "System Awareness" in data["architecture"]["components"]

    assert data["context"]["active_capability"] is None
    assert isinstance(data["limitations"], list)



def test_provider_availability_is_unknown_without_recorded_evidence():
    from core.system_state import _provider_observation

    assert _provider_observation({"last_request": None}, "gemini") == (
        None, None, None, None,
    )
    assert _provider_observation(
        {"last_request": {"provider_attempts": []}},
        "openrouter",
    ) == (None, None, None, None)


def test_provider_availability_uses_latest_matching_attempt_and_provenance():
    from core.system_state import _provider_observation

    runtime = {
        "last_request": {
            "request_id": "req-test-42",
            "started_at": "2026-10-17T00:00:00+00:00",
            "provider_attempts": [
                {"provider": "gemini", "status": "failed"},
                {"provider": "openrouter", "status": "success"},
                {"provider": "gemini", "status": "success"},
            ],
        }
    }

    assert _provider_observation(runtime, "gemini") == (
        True, "success", "2026-10-17T00:00:00+00:00", "req-test-42",
    )
    assert _provider_observation(runtime, "openrouter") == (
        True, "success", "2026-10-17T00:00:00+00:00", "req-test-42",
    )


def test_system_state_exposes_provider_observation_fields():
    response = client.get("/api/v1/system-state")
    assert response.status_code == 200

    providers = response.json()["providers"]
    for provider in providers.values():
        assert "last_observed_status" in provider
        assert "evidence_request_started_at" in provider
        assert "evidence_request_id" in provider
