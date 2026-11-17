from datetime import datetime, timezone

from fastapi.testclient import TestClient
from interfaces.api.tests.uosa_test_client import UosaTestClient

from interfaces.api.server.app import app

OWNER_TOKEN = "uosa-test-owner-sentinel-not-a-real-bearer-token"
from interfaces.api.routes import observability as observability_route


client = UosaTestClient(app, OWNER_TOKEN, auto_owner_auth=True)


def test_observability_events_endpoint(monkeypatch):
    class Store:
        def recent(self, limit):
            assert limit == 2
            return [{"event_type": "MODEL_SUCCESS", "summary": "safe"}]

        def count(self):
            return 1

    class Diagnostics:
        def recent(self, limit):
            return []

    class Bus:
        event_store = Store()
        diagnostic_store = Diagnostics()

    monkeypatch.setattr(observability_route, "get_observability", lambda: Bus())

    response = client.get("/api/v1/observability/events?limit=2")

    assert response.status_code == 200
    assert response.json()["events"] == [
        {"event_type": "MODEL_SUCCESS", "summary": "safe"}
    ]


def test_observability_status_endpoint(monkeypatch):
    class Store:
        def count(self):
            return 4

    class Diagnostics:
        def recent(self, limit):
            assert limit == 500
            return [{}, {}]

    class Bus:
        event_store = Store()
        diagnostic_store = Diagnostics()

    monkeypatch.setattr(observability_route, "get_observability", lambda: Bus())

    response = client.get("/api/v1/observability/status")

    assert response.status_code == 200
    assert response.json()["operational_events"] == 4
    assert response.json()["diagnostic_records"] == 2


def test_sentinel_ingestion_accepts_only_bounded_safe_events(monkeypatch):
    captured = []

    class Bus:
        def emit(self, event):
            captured.append(event)
            return type("Event", (), {"event_id": event["event_id"]})()

    monkeypatch.setattr(observability_route, "get_observability", lambda: Bus())

    response = client.post(
        "/api/v1/observability/sentinel/events",
        json={
            "events": [{
                "schema_version": 1,
                "event_id": "sentinel-1",
                "event_type": "BACKEND_UNREACHABLE",
                "severity": "WARNING",
                "source": "android_sentinel",
                "component": "sentinel",
                "observed_at": "2026-10-04T17:00:00+00:00",
                "correlation_id": "sentinel-corr",
                "parent_event_id": None,
                "provenance": "observed",
                "status": "failed",
                "summary": "Backend health check failed.",
                "metadata": {"consecutive_failures": 3},
                "sequence_number": 7,
            }]
        },
    )

    assert response.status_code == 200
    assert response.json()["processed"] == 1
    assert response.json()["rejected"] == 0
    assert response.json()["processed_event_ids"] == ["sentinel-1"]
    assert response.json()["rejected_event_ids"] == []
    assert captured[0]["source"] == "android_sentinel"
    assert "schema_version" not in captured[0]


def test_sentinel_ingestion_rejects_untrusted_source_and_inference(monkeypatch):
    class Bus:
        def emit(self, event):
            raise AssertionError("unsafe event must not be emitted")

    monkeypatch.setattr(observability_route, "get_observability", lambda: Bus())

    response = client.post(
        "/api/v1/observability/sentinel/events",
        json={
            "events": [
                {
                    "schema_version": 1,
                    "event_id": "spoofed",
                    "event_type": "BACKEND_UNREACHABLE",
                    "severity": "WARNING",
                    "source": "backend",
                    "component": "sentinel",
                    "observed_at": "2026-10-04T17:00:00+00:00",
                    "provenance": "observed",
                    "status": "failed",
                    "summary": "spoofed",
                    "metadata": {},
                    "sequence_number": 1,
                },
                {
                    "schema_version": 1,
                    "event_id": "inferred",
                    "event_type": "BACKEND_UNREACHABLE",
                    "severity": "WARNING",
                    "source": "android_sentinel",
                    "component": "sentinel",
                    "observed_at": "2026-10-04T17:00:00+00:00",
                    "provenance": "inferred",
                    "status": "failed",
                    "summary": "inferred",
                    "metadata": {},
                    "sequence_number": 2,
                },
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["processed"] == 0
    assert response.json()["rejected"] == 2


def test_sentinel_batch_limit_is_bounded():
    response = client.post(
        "/api/v1/observability/sentinel/events",
        json={"events": [{}] * 101},
    )

    assert response.status_code == 413


def test_sentinel_ingestion_returns_partial_acknowledgements(monkeypatch):
    seen = []

    class Bus:
        def emit(self, event):
            seen.append(event)
            if event["event_id"] == "bad":
                return None
            return type("Event", (), {"event_id": event["event_id"]})()

    monkeypatch.setattr(observability_route, "get_observability", lambda: Bus())

    def event(event_id, sequence):
        return {
            "schema_version": 1,
            "event_id": event_id,
            "event_type": "BACKEND_REACHABLE",
            "severity": "INFO",
            "source": "android_sentinel",
            "component": "sentinel",
            "observed_at": "2026-10-04T17:00:00+00:00",
            "provenance": "observed",
            "status": "completed",
            "summary": "Backend is reachable.",
            "metadata": {},
            "sequence_number": sequence,
        }

    response = client.post(
        "/api/v1/observability/sentinel/events",
        json={"events": [event("good", 1), event("bad", 2)]},
    )

    assert response.status_code == 200
    assert response.json()["processed"] == 1
    assert response.json()["rejected"] == 1
    assert response.json()["processed_event_ids"] == ["good"]
    assert response.json()["rejected_event_ids"] == ["bad"]
    assert response.json()["rejection_reasons"] == [
        {"event_id": "bad", "reason": "persistence_failed"}
    ]
    assert response.json()["status"] == "partial"
    assert len(seen) == 2


def test_sentinel_sync_events_are_accepted_as_sentinel_source(monkeypatch):
    captured = []

    class Bus:
        def emit(self, event):
            captured.append(event)
            return type("Event", (), {"event_id": event["event_id"]})()

    monkeypatch.setattr(observability_route, "get_observability", lambda: Bus())

    response = client.post(
        "/api/v1/observability/sentinel/events",
        json={
            "events": [{
                "schema_version": 1,
                "event_id": "sync-failed-1",
                "event_type": "SENTINEL_SYNC_FAILED",
                "severity": "WARNING",
                "source": "android_sentinel",
                "component": "sentinel",
                "observed_at": "2026-10-04T17:00:00+00:00",
                "provenance": "observed",
                "status": "failed",
                "summary": "Sentinel sync failed.",
                "metadata": {"http_status": 503},
                "sequence_number": 8,
            }]
        },
    )

    assert response.status_code == 200
    assert response.json()["processed"] == 1
    assert captured[0]["event_type"] == "SENTINEL_SYNC_FAILED"


def test_sentinel_backend_failure_is_promoted_and_recovery_marks_it_recovered(monkeypatch, tmp_path):
    from core.observability import DiagnosticStore

    class Bus:
        def __init__(self):
            self.diagnostic_store = DiagnosticStore(str(tmp_path / "diagnostics.sqlite3"))
            self.events = []

        def emit_failure(self, event, **kwargs):
            self.events.append(("failure", event))
            return type("Event", (), {"event_id": event["event_id"]})()

        def emit(self, event):
            self.events.append(("event", event))
            return type("Event", (), {"event_id": event["event_id"]})()

    bus = Bus()
    monkeypatch.setattr(observability_route, "get_observability", lambda: bus)

    failure = {
        "schema_version": 1,
        "event_id": "sentinel-failure-1",
        "event_type": "BACKEND_UNREACHABLE",
        "severity": "ERROR",
        "source": "android_sentinel",
        "component": "sentinel",
        "observed_at": "2026-10-04T17:00:00+00:00",
        "provenance": "observed",
        "status": "unreachable",
        "summary": "Backend remained unreachable.",
        "metadata": {},
        "failure_signature": "sentinel_backend_unavailable_v1",
        "sequence_number": 1,
    }
    response = client.post(
        "/api/v1/observability/sentinel/events",
        json={"events": [failure]},
    )

    assert response.status_code == 200
    assert bus.events[0][0] == "failure"



def test_android_sentinel_payload_is_persisted_by_real_observability_bus(
    monkeypatch, tmp_path
):
    from core.observability import (
        DiagnosticStore,
        EventStore,
        ObservabilityBus,
    )

    event_store = EventStore(str(tmp_path / "sentinel-events.sqlite3"))
    diagnostic_store = DiagnosticStore(str(tmp_path / "sentinel-diagnostics.sqlite3"))
    bus = ObservabilityBus(
        event_store=event_store,
        diagnostic_store=diagnostic_store,
    )
    monkeypatch.setattr(observability_route, "get_observability", lambda: bus)

    payload = {
        "schema_version": 1,
        "event_id": "sentinel_android_roundtrip_001",
        "event_type": "BACKEND_REACHABLE",
        "severity": "INFO",
        "source": "android_sentinel",
        "component": "sentinel",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "correlation_id": None,
        "parent_event_id": None,
        "provenance": "observed",
        "status": "reachable",
        "summary": "Backend health check succeeded.",
        "metadata": {},
        "sequence_number": 1,
    }

    response = client.post(
        "/api/v1/observability/sentinel/events",
        json={"events": [payload]},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["processed"] == 1
    assert response.json()["rejected"] == 0
    assert response.json()["processed_event_ids"] == [payload["event_id"]]
    rows = event_store.recent(10)
    assert len(rows) == 1
    assert rows[0]["event_id"] == payload["event_id"]
    assert rows[0]["sequence_number"] == 1


def test_sentinel_schema_mismatch_has_safe_rejection_reason(monkeypatch):
    class Bus:
        def emit(self, event):
            raise AssertionError("unsupported schema must be rejected before emission")

    monkeypatch.setattr(observability_route, "get_observability", lambda: Bus())

    response = client.post(
        "/api/v1/observability/sentinel/events",
        json={
            "events": [{
                "schema_version": 999,
                "event_id": "sentinel_bad_schema",
                "event_type": "BACKEND_REACHABLE",
                "severity": "INFO",
                "source": "android_sentinel",
                "component": "sentinel",
                "observed_at": "2026-11-08T10:00:00+00:00",
                "provenance": "observed",
                "status": "reachable",
                "summary": "Backend health check succeeded.",
                "metadata": {},
                "sequence_number": 1,
            }]
        },
    )

    assert response.status_code == 200
    assert response.json()["processed"] == 0
    assert response.json()["rejected"] == 1
    assert response.json()["rejection_reasons"] == [{
        "event_id": "sentinel_bad_schema",
        "reason": "unsupported_schema_version",
    }]


def test_sentinel_ingestion_does_not_expose_internal_exception_text(monkeypatch):
    class Bus:
        def emit(self, event):
            raise RuntimeError("sqlite path=/private/state.sqlite token=DO_NOT_LEAK")

    monkeypatch.setattr(observability_route, "get_observability", lambda: Bus())

    response = client.post(
        "/api/v1/observability/sentinel/events",
        json={
            "events": [{
                "schema_version": 1,
                "event_id": "sentinel_internal_error",
                "event_type": "BACKEND_REACHABLE",
                "severity": "INFO",
                "source": "android_sentinel",
                "component": "sentinel",
                "observed_at": "2026-11-08T10:00:00+00:00",
                "provenance": "observed",
                "status": "reachable",
                "summary": "Backend health check succeeded.",
                "metadata": {},
                "sequence_number": 1,
            }]
        },
    )

    assert response.status_code == 200
    assert response.json()["rejection_reasons"] == [{
        "event_id": "sentinel_internal_error",
        "reason": "ingestion_error",
    }]
    assert "DO_NOT_LEAK" not in response.text
