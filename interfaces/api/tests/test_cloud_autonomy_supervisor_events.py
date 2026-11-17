import threading
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from interfaces.api.tests.uosa_test_client import UosaTestClient

from core.cloud_autonomy_outbox import CloudSupervisorEventOutbox
from core.cloud_autonomy_supervisor import CloudAutonomyWorkerSupervisor
from core.cloud_autonomy_worker import (
    report_supervisor_event,
    report_supervisor_outbox_heartbeat,
)
from interfaces.api.routes.cloud import cloud_autonomy_worker_status
from interfaces.api.server.app import app


OWNER_TOKEN = "test-owner-review-token-that-is-at-least-32-chars"


def _event() -> dict:
    return {
        "event_id": str(uuid4()),
        "supervisor_id": str(uuid4()),
        "component": "autonomy_worker",
        "event_type": "restart_scheduled",
        "attempt": 1,
        "delay_seconds": 1.0,
        "reason_code": "worker_exception",
    }


def test_report_supervisor_event_retries_same_id_and_whitelists_payload() -> None:
    event = {**_event(), "private_goal": "must not be transmitted"}
    calls = []

    def cloud_request(method, path, *, json_body=None):
        calls.append((method, path, dict(json_body or {})))
        if len(calls) == 1:
            raise RuntimeError("private transport detail")
        return {
            "accepted": True,
            "event_id": event["event_id"],
            "duplicate": True,
        }

    assert report_supervisor_event(event, cloud_request=cloud_request) is True
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0][0:2] == ("POST", "/worker/supervisor-events")
    assert set(calls[0][2]) == {
        "event_id",
        "supervisor_id",
        "component",
        "event_type",
        "attempt",
        "delay_seconds",
        "reason_code",
    }
    assert "private_goal" not in str(calls)


def test_report_supervisor_event_rejects_mismatched_cloud_acknowledgement() -> None:
    event = _event()

    def cloud_request(_method, _path, *, json_body=None):
        return {"accepted": True, "event_id": str(uuid4())}

    assert report_supervisor_event(event, cloud_request=cloud_request) is False


def test_report_supervisor_outbox_heartbeat_retries_with_the_same_instance() -> None:
    instance_id = str(uuid4())
    calls = []

    def cloud_request(method, path, *, json_body=None):
        calls.append((method, path, dict(json_body or {})))
        if len(calls) == 1:
            raise RuntimeError("private transport detail")
        return {
            "accepted": True,
            "worker_name": "amiki-core-supervisor-outbox",
            "instance_id": instance_id,
            "state": "running",
        }

    assert report_supervisor_outbox_heartbeat(
        instance_id,
        "running",
        cloud_request=cloud_request,
    ) is True
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0][0:2] == ("POST", "/worker/outbox-heartbeat")
    assert calls[0][2] == {"instance_id": instance_id, "state": "running"}


def test_report_supervisor_outbox_heartbeat_rejects_mismatched_acknowledgement() -> None:
    instance_id = str(uuid4())

    def cloud_request(_method, _path, *, json_body=None):
        return {
            "accepted": True,
            "worker_name": "amiki-core-autonomy",
            "instance_id": instance_id,
            "state": "running",
        }

    assert report_supervisor_outbox_heartbeat(
        instance_id,
        "running",
        cloud_request=cloud_request,
    ) is False


def test_supervisor_reports_scheduled_and_started_events_without_exception_text() -> None:
    application_stop = threading.Event()
    replacement_started = threading.Event()
    restart_started = threading.Event()
    events = []
    attempts = 0

    def worker(worker_stop: threading.Event) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("private goal and provider details")
        replacement_started.set()
        worker_stop.wait()

    def report(event: dict) -> bool:
        events.append(dict(event))
        if event["event_type"] == "restart_started":
            restart_started.set()
        return True

    supervisor = CloudAutonomyWorkerSupervisor(
        worker,
        initial_backoff_seconds=0.01,
        max_backoff_seconds=0.02,
        poll_interval_seconds=0.01,
        event_reporter=report,
    )
    thread = threading.Thread(target=supervisor.run, args=(application_stop,), daemon=True)
    thread.start()

    assert replacement_started.wait(timeout=2.0)
    assert restart_started.wait(timeout=2.0)
    application_stop.set()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert [event["event_type"] for event in events] == [
        "restart_scheduled",
        "restart_started",
    ]
    assert [event["reason_code"] for event in events] == [
        "worker_exception",
        "worker_started",
    ]
    assert all(event["attempt"] == 1 for event in events)
    assert all(event["component"] == "autonomy_worker" for event in events)
    assert all(event["supervisor_id"] == supervisor.snapshot()["supervisor_id"] for event in events)
    assert all(str(UUID(event["event_id"])) == event["event_id"] for event in events)
    assert "private goal and provider details" not in str(events)


def test_supervisor_reports_restart_budget_exhaustion() -> None:
    events = []

    def crashing_worker(_worker_stop: threading.Event) -> None:
        raise RuntimeError("private exception")

    supervisor = CloudAutonomyWorkerSupervisor(
        crashing_worker,
        max_restarts=0,
        event_reporter=lambda event: events.append(dict(event)) or True,
    )
    supervisor.run(threading.Event())

    assert supervisor.snapshot()["state"] == "exhausted"
    assert len(events) == 1
    assert events[0]["event_type"] == "restart_exhausted"
    assert events[0]["attempt"] == 1
    assert events[0]["delay_seconds"] == 0.0
    assert events[0]["reason_code"] == "restart_budget_exhausted"
    assert events[0]["component"] == "autonomy_worker"


def test_supervisor_reports_backoff_cancellation_on_application_shutdown() -> None:
    application_stop = threading.Event()
    scheduled = threading.Event()
    events = []
    attempts = 0

    def crashing_worker(_worker_stop: threading.Event) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("private exception")

    def report(event: dict) -> bool:
        events.append(dict(event))
        if event["event_type"] == "restart_scheduled":
            scheduled.set()
        return True

    supervisor = CloudAutonomyWorkerSupervisor(
        crashing_worker,
        initial_backoff_seconds=1.0,
        event_reporter=report,
    )
    thread = threading.Thread(target=supervisor.run, args=(application_stop,), daemon=True)
    thread.start()

    assert scheduled.wait(timeout=2.0)
    application_stop.set()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert attempts == 1
    assert [event["event_type"] for event in events] == [
        "restart_scheduled",
        "restart_cancelled",
    ]
    assert events[-1]["reason_code"] == "application_shutdown"


def test_supervisor_recovery_continues_when_audit_delivery_fails() -> None:
    application_stop = threading.Event()
    replacement_started = threading.Event()
    attempts = 0

    def worker(worker_stop: threading.Event) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("private exception")
        replacement_started.set()
        worker_stop.wait()

    def unavailable_reporter(_event: dict) -> bool:
        raise RuntimeError("private transport detail")

    supervisor = CloudAutonomyWorkerSupervisor(
        worker,
        initial_backoff_seconds=0.01,
        max_backoff_seconds=0.02,
        poll_interval_seconds=0.01,
        event_reporter=unavailable_reporter,
    )
    thread = threading.Thread(target=supervisor.run, args=(application_stop,), daemon=True)
    thread.start()

    assert replacement_started.wait(timeout=2.0)
    application_stop.set()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert attempts == 2
    assert supervisor.snapshot()["restart_count"] == 1


def test_application_wires_supervisor_events_through_durable_outbox(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AMIKI_CLOUD_AUTONOMY_WORKER_ENABLED", "true")
    monkeypatch.setenv(
        "AMIKI_CLOUD_SUPERVISOR_OUTBOX_PATH",
        str(tmp_path / "supervisor-events.sqlite3"),
    )
    cloud_events = []
    heartbeats = []
    restart_started = threading.Event()

    def cloud_request(_method, path, *, json_body=None):
        if path == "/worker/outbox-heartbeat":
            heartbeat = dict(json_body or {})
            heartbeats.append(heartbeat)
            return {
                "accepted": True,
                "worker_name": "amiki-core-supervisor-outbox",
                **heartbeat,
            }
        if path != "/worker/supervisor-events":
            return None
        event = dict(json_body or {})
        cloud_events.append(event)
        if event["event_type"] == "restart_started":
            restart_started.set()
        return {"accepted": True, "event_id": event["event_id"]}

    monkeypatch.setattr("core.cloud_autonomy_worker._cloud_request", cloud_request)

    attempts = 0
    replacement_started = threading.Event()

    def worker(worker_stop: threading.Event) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("private exception")
        replacement_started.set()
        worker_stop.wait()

    monkeypatch.setattr("interfaces.api.server.app.run_worker", worker)

    with UosaTestClient(app, OWNER_TOKEN, auto_owner_auth=True) as client:
        assert replacement_started.wait(timeout=2.0)
        assert restart_started.wait(timeout=2.0)
        response = client.get("/api/v1/cloud/autonomy/worker/status")
        for _ in range(100):
            outbox_status = response.json().get("event_outbox", {})
            if outbox_status.get("pending") == 0 and outbox_status.get("delivered") == 2:
                break
            threading.Event().wait(0.01)
            response = client.get("/api/v1/cloud/autonomy/worker/status")

        assert response.status_code == 200
        assert response.json()["supervisor"]["state"] == "running"
        assert response.json()["event_outbox"]["available"] is True
        assert response.json()["event_outbox"]["pending"] == 0
        assert response.json()["event_outbox"]["delivered"] == 2
        assert response.json()["event_outbox"]["delivery_running"] is True
        assert response.json()["event_outbox"]["delivery_degraded"] is False
        assert response.json()["event_outbox"]["delivery_supervisor"]["state"] == "running"
        assert response.json()["event_outbox"]["delivery_supervisor"]["restart_count"] == 0

    assert [event["event_type"] for event in cloud_events] == [
        "restart_scheduled",
        "restart_started",
    ]
    assert all(event["supervisor_id"] == cloud_events[0]["supervisor_id"] for event in cloud_events)
    assert all(event["component"] == "autonomy_worker" for event in cloud_events)
    assert [heartbeat["state"] for heartbeat in heartbeats] == ["running", "stopped"]
    assert len({heartbeat["instance_id"] for heartbeat in heartbeats}) == 1


def test_application_keeps_worker_running_when_outbox_is_unavailable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AMIKI_CLOUD_AUTONOMY_WORKER_ENABLED", "true")
    invalid_parent = tmp_path / "not-a-directory"
    invalid_parent.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv(
        "AMIKI_CLOUD_SUPERVISOR_OUTBOX_PATH",
        str(invalid_parent / "supervisor-events.sqlite3"),
    )
    worker_started = threading.Event()

    def fake_worker(worker_stop: threading.Event) -> None:
        worker_started.set()
        worker_stop.wait()

    monkeypatch.setattr("interfaces.api.server.app.run_worker", fake_worker)

    with UosaTestClient(app, OWNER_TOKEN, auto_owner_auth=True) as client:
        assert worker_started.wait(timeout=2.0)
        response = client.get("/api/v1/cloud/autonomy/worker/status")

        assert response.status_code == 200
        assert response.json()["enabled"] is True
        assert response.json()["running"] is True
        assert response.json()["event_outbox"] == {
            "available": False,
            "delivery_running": False,
            "delivery_degraded": True,
            "delivery_supervisor": None,
        }



def test_worker_status_degrades_when_outbox_diagnostics_fail() -> None:
    class Supervisor:
        @staticmethod
        def snapshot() -> dict:
            return {"worker_running": True}

    class BrokenOutbox:
        @staticmethod
        def snapshot() -> dict:
            raise OSError("private database path and operating-system detail")

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                cloud_autonomy_worker_enabled=True,
                cloud_autonomy_worker_supervisor=Supervisor(),
                cloud_autonomy_supervisor_event_outbox=BrokenOutbox(),
            )
        )
    )

    response = cloud_autonomy_worker_status(request)

    assert response["enabled"] is True
    assert response["running"] is True
    assert response["event_outbox"] == {
        "available": False,
        "delivery_running": False,
        "delivery_degraded": True,
        "delivery_supervisor": None,
    }
    assert "private database path" not in str(response)


def test_worker_status_reports_outbox_delivery_thread_liveness() -> None:
    class Supervisor:
        @staticmethod
        def snapshot() -> dict:
            return {"worker_running": True}

    class Outbox:
        @staticmethod
        def snapshot() -> dict:
            return {
                "available": True,
                "pending": 3,
                "delivered": 8,
                "dead_lettered": 1,
                "oldest_pending_age_seconds": 12.5,
                "max_attempts": 8,
            }

    class StoppedDeliveryThread:
        @staticmethod
        def is_alive() -> bool:
            return False

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                cloud_autonomy_worker_enabled=True,
                cloud_autonomy_worker_supervisor=Supervisor(),
                cloud_autonomy_supervisor_event_outbox=Outbox(),
                cloud_autonomy_supervisor_event_outbox_thread=StoppedDeliveryThread(),
            )
        )
    )

    response = cloud_autonomy_worker_status(request)

    assert response["running"] is True
    assert response["event_outbox"]["available"] is True
    assert response["event_outbox"]["pending"] == 3
    assert response["event_outbox"]["delivery_running"] is False
    assert response["event_outbox"]["delivery_degraded"] is True
    assert response["event_outbox"]["delivery_supervisor"] is None

def test_outbox_dead_letter_inspection_requires_owner_and_loopback(
    monkeypatch,
    tmp_path,
) -> None:
    outbox = CloudSupervisorEventOutbox(
        tmp_path / "supervisor-events.sqlite3",
        max_attempts=1,
    )
    event = _event()
    outbox.enqueue(event)
    assert outbox.deliver_one(lambda _payload: False) is True

    with UosaTestClient(app, OWNER_TOKEN, client=("127.0.0.1", 12345)) as client:
        app.state.cloud_autonomy_supervisor_event_outbox = outbox
        assert client.get(
            "/api/v1/cloud/autonomy/worker/outbox/dead-letters"
        ).status_code == 401
        assert client.get(
            "/api/v1/cloud/autonomy/worker/outbox/dead-letters",
            headers={"Authorization": "Bearer wrong-owner-token"},
        ).status_code == 401

        response = client.get(
            "/api/v1/cloud/autonomy/worker/outbox/dead-letters",
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        )

    assert response.status_code == 200
    assert response.json() == {"event_ids": [event["event_id"]], "count": 1}
    assert "private" not in str(response.json())


def test_outbox_dead_letter_inspection_rejects_non_loopback_clients(
    monkeypatch,
    tmp_path,
) -> None:
    outbox = CloudSupervisorEventOutbox(tmp_path / "supervisor-events.sqlite3")

    with UosaTestClient(app, OWNER_TOKEN, client=("10.0.0.8", 12345)) as client:
        app.state.cloud_autonomy_supervisor_event_outbox = outbox
        response = client.get(
            "/api/v1/cloud/autonomy/worker/outbox/dead-letters",
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "supervisor_outbox_inspection_loopback_only"


def test_outbox_dead_letter_inspection_requires_uosa_session(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AMIKI_OWNER_REVIEW_TOKEN", raising=False)

    with UosaTestClient(app, OWNER_TOKEN, client=("127.0.0.1", 12345)) as client:
        response = client.get("/api/v1/cloud/autonomy/worker/outbox/dead-letters")

    assert response.status_code == 401
    assert response.json()["detail"] == "A valid UOSA owner session is required."


def test_outbox_retry_api_requeues_only_after_owner_authorization(
    monkeypatch,
    tmp_path,
) -> None:
    outbox = CloudSupervisorEventOutbox(
        tmp_path / "supervisor-events.sqlite3",
        max_attempts=1,
    )
    event = _event()
    outbox.enqueue(event)
    assert outbox.deliver_one(lambda _payload: False) is True

    with UosaTestClient(app, OWNER_TOKEN, client=("127.0.0.1", 12345)) as client:
        app.state.cloud_autonomy_supervisor_event_outbox = outbox
        payload = {"event_ids": [event["event_id"]]}
        assert client.post(
            "/api/v1/cloud/autonomy/worker/outbox/retry",
            json=payload,
        ).status_code == 401

        response = client.post(
            "/api/v1/cloud/autonomy/worker/outbox/retry",
            json=payload,
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "requeued": 1,
        "not_retryable": 0,
        "not_found": 0,
        "replay_limit_reached": 0,
    }
    assert outbox.snapshot()["pending"] == 1


def test_outbox_retry_api_enforces_loopback_even_with_uosa_session(
    monkeypatch,
    tmp_path,
) -> None:
    outbox = CloudSupervisorEventOutbox(tmp_path / "supervisor-events.sqlite3")

    with UosaTestClient(app, OWNER_TOKEN, client=("10.0.0.8", 12345)) as client:
        app.state.cloud_autonomy_supervisor_event_outbox = outbox
        response = client.post(
            "/api/v1/cloud/autonomy/worker/outbox/retry",
            json={"event_ids": [str(uuid4())]},
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "supervisor_outbox_retry_loopback_only"


def test_outbox_retry_api_maps_capacity_conflict_without_leaking_details(
    monkeypatch,
) -> None:

    class FullOutbox:
        @staticmethod
        def retry_dead_letters(_event_ids: list[str]) -> dict:
            raise RuntimeError("supervisor_outbox_capacity_reached")

    with UosaTestClient(app, OWNER_TOKEN, client=("127.0.0.1", 12345)) as client:
        app.state.cloud_autonomy_supervisor_event_outbox = FullOutbox()
        response = client.post(
            "/api/v1/cloud/autonomy/worker/outbox/retry",
            json={"event_ids": [str(uuid4())]},
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "supervisor_outbox_capacity_reached"
    assert "capacity" not in str(response.json()).replace(
        "supervisor_outbox_capacity_reached",
        "",
    )


def test_outbox_retry_api_reports_unavailable_outbox(
    monkeypatch,
) -> None:

    with UosaTestClient(app, OWNER_TOKEN, client=("127.0.0.1", 12345)) as client:
        response = client.post(
            "/api/v1/cloud/autonomy/worker/outbox/retry",
            json={"event_ids": [str(uuid4())]},
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "supervisor_event_outbox_unavailable"

def test_outbox_delivery_supervisor_restarts_a_crashed_delivery_thread(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AMIKI_CLOUD_AUTONOMY_WORKER_ENABLED", "true")
    monkeypatch.setenv(
        "AMIKI_CLOUD_SUPERVISOR_OUTBOX_PATH",
        str(tmp_path / "supervisor-events.sqlite3"),
    )

    real_supervisor = CloudAutonomyWorkerSupervisor

    def fast_supervisor(worker, **kwargs):
        return real_supervisor(
            worker,
            initial_backoff_seconds=0.01,
            max_backoff_seconds=0.02,
            poll_interval_seconds=0.01,
            **kwargs,
        )

    monkeypatch.setattr(
        "interfaces.api.server.app.CloudAutonomyWorkerSupervisor",
        fast_supervisor,
    )

    real_run = CloudSupervisorEventOutbox.run
    attempts = [0]
    replacement_started = threading.Event()

    def crash_once(self, worker_stop, deliver, *, heartbeat=None):
        attempts[0] += 1
        if attempts[0] == 1:
            raise RuntimeError("private outbox delivery failure")
        replacement_started.set()
        return real_run(self, worker_stop, deliver, heartbeat=heartbeat)

    monkeypatch.setattr(CloudSupervisorEventOutbox, "run", crash_once)

    cloud_events = []
    heartbeats = []

    def cloud_request(_method, path, *, json_body=None):
        payload = dict(json_body or {})
        if path == "/worker/outbox-heartbeat":
            heartbeats.append(payload)
            return {
                "accepted": True,
                "worker_name": "amiki-core-supervisor-outbox",
                **payload,
            }
        if path == "/worker/supervisor-events":
            cloud_events.append(payload)
            return {"accepted": True, "event_id": payload["event_id"]}
        return None

    monkeypatch.setattr("core.cloud_autonomy_worker._cloud_request", cloud_request)

    worker_started = threading.Event()

    def main_worker(worker_stop: threading.Event) -> None:
        worker_started.set()
        worker_stop.wait()

    monkeypatch.setattr("interfaces.api.server.app.run_worker", main_worker)

    with UosaTestClient(app, OWNER_TOKEN, auto_owner_auth=True) as client:
        assert worker_started.wait(timeout=2.0)
        assert replacement_started.wait(timeout=2.0)

        response = client.get("/api/v1/cloud/autonomy/worker/status")
        for _ in range(200):
            outbox_status = response.json().get("event_outbox", {})
            supervisor_status = outbox_status.get("delivery_supervisor", {})
            if (
                outbox_status.get("delivered") == 2
                and outbox_status.get("delivery_running") is True
                and supervisor_status.get("restart_count") == 1
                and len(cloud_events) == 2
            ):
                break
            threading.Event().wait(0.01)
            response = client.get("/api/v1/cloud/autonomy/worker/status")

        assert response.status_code == 200
        outbox_status = response.json()["event_outbox"]
        assert outbox_status["delivery_running"] is True
        assert outbox_status["delivery_degraded"] is False
        assert outbox_status["delivery_supervisor"]["state"] == "running"
        assert outbox_status["delivery_supervisor"]["restart_count"] == 1

    assert attempts[0] >= 2
    assert [event["event_type"] for event in cloud_events] == [
        "restart_scheduled",
        "restart_started",
    ]
    assert all(event["component"] == "supervisor_outbox_delivery" for event in cloud_events)
    assert [heartbeat["state"] for heartbeat in heartbeats] == ["running", "stopped"]


def test_owner_can_restart_an_exhausted_outbox_supervisor_and_audit_recovery(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AMIKI_CLOUD_AUTONOMY_WORKER_ENABLED", "true")
    monkeypatch.setenv(
        "AMIKI_CLOUD_SUPERVISOR_OUTBOX_PATH",
        str(tmp_path / "supervisor-events.sqlite3"),
    )

    real_supervisor = CloudAutonomyWorkerSupervisor

    def no_automatic_restarts(worker, **kwargs):
        return real_supervisor(
            worker,
            max_restarts=0,
            initial_backoff_seconds=0.01,
            max_backoff_seconds=0.02,
            poll_interval_seconds=0.01,
            **kwargs,
        )

    monkeypatch.setattr(
        "interfaces.api.server.app.CloudAutonomyWorkerSupervisor",
        no_automatic_restarts,
    )

    real_run = CloudSupervisorEventOutbox.run
    attempts = [0]
    first_crash = threading.Event()
    replacement_started = threading.Event()

    def crash_once(self, worker_stop, deliver, *, heartbeat=None):
        attempts[0] += 1
        if attempts[0] == 1:
            first_crash.set()
            raise RuntimeError("private outbox delivery failure")
        replacement_started.set()
        return real_run(self, worker_stop, deliver, heartbeat=heartbeat)

    monkeypatch.setattr(CloudSupervisorEventOutbox, "run", crash_once)

    cloud_events = []
    heartbeats = []

    def cloud_request(_method, path, *, json_body=None):
        payload = dict(json_body or {})
        if path == "/worker/outbox-heartbeat":
            heartbeats.append(payload)
            return {
                "accepted": True,
                "worker_name": "amiki-core-supervisor-outbox",
                **payload,
            }
        if path == "/worker/supervisor-events":
            cloud_events.append(payload)
            return {"accepted": True, "event_id": payload["event_id"]}
        return None

    monkeypatch.setattr("core.cloud_autonomy_worker._cloud_request", cloud_request)

    main_worker_started = threading.Event()

    def main_worker(worker_stop: threading.Event) -> None:
        main_worker_started.set()
        worker_stop.wait()

    monkeypatch.setattr("interfaces.api.server.app.run_worker", main_worker)

    endpoint = "/api/v1/cloud/autonomy/worker/outbox/supervisor/restart"
    with UosaTestClient(
        app,
        OWNER_TOKEN,
        auto_owner_auth=True,
        client=("127.0.0.1", 12345),
    ) as client:
        assert main_worker_started.wait(timeout=2.0)
        assert first_crash.wait(timeout=2.0)

        response = client.get("/api/v1/cloud/autonomy/worker/status")
        for _ in range(200):
            supervisor_status = response.json()["event_outbox"]["delivery_supervisor"]
            current_thread = app.state.cloud_autonomy_supervisor_event_outbox_thread
            if supervisor_status["state"] == "exhausted" and not current_thread.is_alive():
                break
            threading.Event().wait(0.01)
            response = client.get("/api/v1/cloud/autonomy/worker/status")

        assert response.status_code == 200
        assert response.json()["event_outbox"]["delivery_supervisor"]["state"] == "exhausted"
        exhausted_supervisor = app.state.cloud_autonomy_supervisor_event_outbox_supervisor
        assert exhausted_supervisor.start(
            app.state.cloud_autonomy_supervisor_event_outbox_stop_event,
            thread_name="amiki-cloud-supervisor-event-outbox-supervisor",
        ) is False
        assert client.post(
            endpoint,
            headers={"Authorization": "Bearer invalid-test-token"},
        ).status_code == 401

        restarted = client.post(
            endpoint,
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        )
        assert restarted.status_code == 202
        assert restarted.json() == {
            "restarted": True,
            "component": "supervisor_outbox_delivery",
            "state": "starting",
        }
        assert replacement_started.wait(timeout=2.0)

        response = client.get("/api/v1/cloud/autonomy/worker/status")
        for _ in range(200):
            event_outbox = response.json()["event_outbox"]
            delivery_supervisor = event_outbox["delivery_supervisor"]
            if (
                event_outbox["delivery_running"] is True
                and delivery_supervisor["state"] == "running"
                and any(
                    event["reason_code"] == "owner_requested_recovery"
                    for event in cloud_events
                )
                and any(
                    event["event_type"] == "restart_started"
                    and event["component"] == "supervisor_outbox_delivery"
                    for event in cloud_events
                )
            ):
                break
            threading.Event().wait(0.01)
            response = client.get("/api/v1/cloud/autonomy/worker/status")

        assert response.status_code == 200
        assert response.json()["event_outbox"]["delivery_running"] is True
        assert response.json()["event_outbox"]["delivery_supervisor"]["state"] == "running"

    assert attempts[0] >= 2
    recovery_events = [
        event
        for event in cloud_events
        if event["reason_code"] == "owner_requested_recovery"
    ]
    restart_started_events = [
        event
        for event in cloud_events
        if event["event_type"] == "restart_started"
        and event["component"] == "supervisor_outbox_delivery"
    ]
    assert len(recovery_events) == 1
    assert len(restart_started_events) == 1
    assert recovery_events[0]["event_type"] == "restart_scheduled"
    assert recovery_events[0]["component"] == "supervisor_outbox_delivery"
    assert recovery_events[0]["attempt"] == restart_started_events[0]["attempt"] == 1
    assert cloud_events.index(recovery_events[0]) < cloud_events.index(restart_started_events[0])
    supervisor_snapshot = app.state.cloud_autonomy_supervisor_event_outbox_supervisor.snapshot()
    assert supervisor_snapshot["restart_count"] == 1
    assert supervisor_snapshot["history"][-1]["reason"] == "owner_requested_recovery"
    assert heartbeats[0]["state"] == "running"
    assert heartbeats[-1]["state"] == "stopped"


def test_outbox_supervisor_restart_rejects_non_loopback_clients(monkeypatch) -> None:
    monkeypatch.setenv("AMIKI_CLOUD_AUTONOMY_WORKER_ENABLED", "false")

    with UosaTestClient(app, OWNER_TOKEN, client=("10.0.0.8", 12345)) as client:
        response = client.post(
            "/api/v1/cloud/autonomy/worker/outbox/supervisor/restart",
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "supervisor_outbox_restart_loopback_only"

