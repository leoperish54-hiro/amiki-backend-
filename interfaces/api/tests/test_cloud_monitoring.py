from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from interfaces.api.tests.uosa_test_client import UosaTestClient

from interfaces.api.server.app import app

OWNER_TOKEN = "uosa-test-owner-sentinel-not-a-real-bearer-token"

TOKEN = "amiki-cloud-test-token-with-more-than-32-characters"


@pytest.fixture
def client() -> TestClient:
    return UosaTestClient(app, OWNER_TOKEN, auto_owner_auth=True)


def _response(payload: dict, status_code: int = 200) -> httpx.Response:
    request = httpx.Request("GET", "http://127.0.0.1:8001/api/v1/monitor/status")
    return httpx.Response(status_code, json=payload, request=request)


def test_cloud_monitor_status_is_authenticated_and_read_only(client, monkeypatch) -> None:
    monkeypatch.setenv("AMIKI_CLOUD_API_TOKEN", TOKEN)
    expected = {
        "running": True,
        "last_scan_at": "2026-11-05 10:00:00",
        "counts": {"open": 2, "acknowledged": 1, "resolved": 4},
    }
    mocked = Mock(return_value=_response(expected))
    monkeypatch.setattr(httpx, "request", mocked)

    result = client.get("/api/v1/cloud/monitor/status")

    assert result.status_code == 200
    assert result.json() == expected
    assert mocked.call_args.args[:2] == (
        "GET",
        "http://127.0.0.1:8001/api/v1/monitor/status",
    )
    assert mocked.call_args.kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_cloud_monitor_events_forward_only_bounded_filters(client, monkeypatch) -> None:
    monkeypatch.setenv("AMIKI_CLOUD_API_TOKEN", TOKEN)
    expected = {"items": [{"id": 1, "event_type": "job_queue_stalled", "state": "open"}]}
    mocked = Mock(return_value=_response(expected))
    monkeypatch.setattr(httpx, "request", mocked)

    result = client.get("/api/v1/cloud/monitor/events?state=all&limit=100")

    assert result.status_code == 200
    assert result.json() == expected
    assert mocked.call_args.args[:2] == (
        "GET",
        "http://127.0.0.1:8001/api/v1/monitor/events?state=all&limit=100",
    )
    assert mocked.call_args.kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_cloud_monitor_events_reject_invalid_filters_before_upstream_call(
    client, monkeypatch
) -> None:
    monkeypatch.setenv("AMIKI_CLOUD_API_TOKEN", TOKEN)
    mocked = Mock()
    monkeypatch.setattr(httpx, "request", mocked)

    assert client.get("/api/v1/cloud/monitor/events?state=private").status_code == 422
    assert client.get("/api/v1/cloud/monitor/events?limit=0").status_code == 422
    assert client.get("/api/v1/cloud/monitor/events?limit=101").status_code == 422

    mocked.assert_not_called()


def test_cloud_monitor_status_fails_closed_without_owner_token(
    client, monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("AMIKI_CLOUD_API_TOKEN", raising=False)
    monkeypatch.setenv("AMIKI_CLOUD_TOKEN_FILE", str(tmp_path / "missing-token"))
    mocked = Mock()
    monkeypatch.setattr(httpx, "request", mocked)

    result = client.get("/api/v1/cloud/monitor/status")

    assert result.status_code == 503
    assert result.json()["detail"] == "amiki_cloud_auth_not_configured"
    mocked.assert_not_called()


def test_cloud_autonomy_status_stop_and_resume_are_authenticated(client, monkeypatch) -> None:
    monkeypatch.setenv("AMIKI_CLOUD_API_TOKEN", TOKEN)
    expected = {"enabled": True}
    mocked = Mock(side_effect=[_response(expected), _response({"enabled": False}), _response(expected)])
    monkeypatch.setattr(httpx, "request", mocked)

    status_result = client.get("/api/v1/cloud/autonomy/status")
    stop_result = client.post("/api/v1/cloud/autonomy/stop")
    resume_result = client.post("/api/v1/cloud/autonomy/resume")

    assert status_result.status_code == 200
    assert status_result.json() == expected
    assert stop_result.status_code == 200
    assert stop_result.json() == {"enabled": False}
    assert resume_result.status_code == 200
    assert resume_result.json() == expected
    assert [call.args[0] for call in mocked.call_args_list] == ["GET", "POST", "POST"]
    assert [call.args[1] for call in mocked.call_args_list] == [
        "http://127.0.0.1:8001/api/v1/autonomy/status",
        "http://127.0.0.1:8001/api/v1/autonomy/stop",
        "http://127.0.0.1:8001/api/v1/autonomy/resume",
    ]
    for call in mocked.call_args_list:
        assert call.kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_cloud_autonomy_controls_fail_closed_without_owner_token(
    client, monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("AMIKI_CLOUD_API_TOKEN", raising=False)
    monkeypatch.setenv("AMIKI_CLOUD_TOKEN_FILE", str(tmp_path / "missing-token"))
    mocked = Mock()
    monkeypatch.setattr(httpx, "request", mocked)

    assert client.get("/api/v1/cloud/autonomy/status").status_code == 503
    assert client.post("/api/v1/cloud/autonomy/stop").status_code == 503
    assert client.post("/api/v1/cloud/autonomy/resume").status_code == 503
    mocked.assert_not_called()


def test_cloud_monitor_event_acknowledgement_is_authenticated(client, monkeypatch) -> None:
    monkeypatch.setenv("AMIKI_CLOUD_API_TOKEN", TOKEN)
    expected = {"id": 7, "state": "acknowledged"}
    mocked = Mock(return_value=_response(expected))
    monkeypatch.setattr(httpx, "request", mocked)

    result = client.post("/api/v1/cloud/monitor/events/7/acknowledge")

    assert result.status_code == 200
    assert result.json() == expected
    assert mocked.call_args.args[:2] == (
        "POST",
        "http://127.0.0.1:8001/api/v1/monitor/events/7/acknowledge",
    )
    assert mocked.call_args.kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_cloud_monitor_event_acknowledgement_rejects_invalid_ids(client, monkeypatch) -> None:
    monkeypatch.setenv("AMIKI_CLOUD_API_TOKEN", TOKEN)
    mocked = Mock()
    monkeypatch.setattr(httpx, "request", mocked)

    assert client.post("/api/v1/cloud/monitor/events/0/acknowledge").status_code == 422
    assert client.post("/api/v1/cloud/monitor/events/not-an-id/acknowledge").status_code == 422
    mocked.assert_not_called()


def test_cloud_monitor_event_not_found_is_sanitized(client, monkeypatch) -> None:
    monkeypatch.setenv("AMIKI_CLOUD_API_TOKEN", TOKEN)
    mocked = Mock(return_value=_response({"detail": "private details"}, 404))
    monkeypatch.setattr(httpx, "request", mocked)

    result = client.post("/api/v1/cloud/monitor/events/999/acknowledge")

    assert result.status_code == 404
    assert result.json()["detail"] == "amiki_cloud_monitor_event_not_found"
    assert "private details" not in result.text


def test_cloud_autonomy_worker_status_uses_actual_thread_liveness(client) -> None:
    class FakeWorkerThread:
        def __init__(self, alive: bool) -> None:
            self.alive = alive

        def is_alive(self) -> bool:
            return self.alive

    client.app.state.cloud_autonomy_worker_enabled = True
    client.app.state.cloud_autonomy_worker_running = True
    client.app.state.cloud_autonomy_worker_supervisor = None
    client.app.state.cloud_autonomy_worker_thread = FakeWorkerThread(False)

    stopped = client.get("/api/v1/cloud/autonomy/worker/status")
    assert stopped.status_code == 200
    assert stopped.json() == {"enabled": True, "running": False}

    client.app.state.cloud_autonomy_worker_thread = FakeWorkerThread(True)
    running = client.get("/api/v1/cloud/autonomy/worker/status")
    assert running.status_code == 200
    assert running.json() == {"enabled": True, "running": True}
