from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from core.observability import (
    EVENT_TYPES,
    SCHEMA_VERSION,
    RETENTION_DAYS,
    MAX_EVENT_ROWS,
    get_observability,
)


router = APIRouter()

_SENTINEL_EVENT_TYPES = frozenset({
    "SENTINEL_STARTED",
    "SENTINEL_STOPPED",
    "BACKEND_REACHABLE",
    "BACKEND_UNREACHABLE",
    "BACKEND_RECOVERED",
    "RECOVERY_STARTED",
    "RECOVERY_SUCCESS",
    "RECOVERY_FAILED",
    "API_HEALTH_CHANGED",
    "SENTINEL_SYNC_SUCCESS",
    "SENTINEL_SYNC_FAILED",
})
_MAX_SENTINEL_BATCH = 100


class SentinelEventBatch(BaseModel):
    events: list[dict[str, Any]]


@router.get("/observability/events")
def observability_events(
    limit: int = Query(100, ge=1, le=500),
):
    """Return bounded, already-sanitized operational observability events."""
    bus = get_observability()
    return {
        "status": "ok",
        "events": bus.event_store.recent(limit),
        "retention_days": RETENTION_DAYS,
        "max_rows": MAX_EVENT_ROWS,
    }


@router.get("/observability/diagnostics")
def observability_diagnostics(
    limit: int = Query(100, ge=1, le=500),
):
    """Return compact permanent diagnostic records."""
    bus = get_observability()
    return {
        "status": "ok",
        "diagnostics": bus.diagnostic_store.recent(limit),
    }


@router.get("/observability/diagnostics/{failure_signature}")
def observability_diagnostic_detail(failure_signature: str):
    """Return one bounded permanent diagnostic record by stable signature."""
    bus = get_observability()
    record = bus.diagnostic_store.get(failure_signature)
    if record is None:
        raise HTTPException(status_code=404, detail="Diagnostic record not found.")
    return {
        "status": "ok",
        "diagnostic": record,
    }


@router.get("/observability/status")
def observability_status():
    """Return safe observability store status."""
    bus = get_observability()
    return {
        "status": "ok",
        "operational_events": bus.event_store.count(),
        "diagnostic_records": len(bus.diagnostic_store.recent(500)),
        "retention_days": RETENTION_DAYS,
        "max_operational_rows": MAX_EVENT_ROWS,
        "schema_version": 1,
    }


@router.post("/observability/sentinel/events")
def ingest_sentinel_events(batch: SentinelEventBatch):
    """Ingest bounded, versioned Sentinel events with per-event safe acknowledgements."""
    if len(batch.events) > _MAX_SENTINEL_BATCH:
        raise HTTPException(
            status_code=413,
            detail="Sentinel event batch is too large.",
        )

    bus = get_observability()
    processed = 0
    rejected = 0
    processed_event_ids: list[str] = []
    rejected_event_ids: list[str] = []
    rejection_reasons: list[dict[str, str | None]] = []

    for raw_event in batch.events:
        event_id = raw_event.get("event_id") if isinstance(raw_event, dict) else None
        reason = "ingestion_error"
        try:
            if not isinstance(raw_event, dict):
                reason = "invalid_event"
                raise ValueError("event must be an object")

            if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 128:
                reason = "invalid_event_id"
                raise ValueError("event_id is invalid")

            schema_version = raw_event.get("schema_version")
            if (
                not isinstance(schema_version, int)
                or isinstance(schema_version, bool)
                or schema_version != SCHEMA_VERSION
            ):
                reason = "unsupported_schema_version"
                raise ValueError("unsupported Sentinel schema version")

            if raw_event.get("source") != "android_sentinel":
                reason = "invalid_source"
                raise ValueError("invalid sentinel source")
            if raw_event.get("event_type") not in _SENTINEL_EVENT_TYPES:
                reason = "unsupported_event_type"
                raise ValueError("event type is not permitted for Sentinel")
            if raw_event.get("provenance") not in {"observed", "derived"}:
                reason = "invalid_provenance"
                raise ValueError("invalid Sentinel provenance")
            sequence_number = raw_event.get("sequence_number")
            if (
                not isinstance(sequence_number, int)
                or isinstance(sequence_number, bool)
                or sequence_number < 0
            ):
                reason = "invalid_sequence_number"
                raise ValueError("Sentinel sequence_number is required")

            # schema_version belongs to the wire envelope. ObservationEvent.create()
            # owns and validates the canonical schema version itself, so do not pass
            # the envelope field through as an unexpected keyword argument.
            event_payload = {
                key: value for key, value in raw_event.items()
                if key != "schema_version"
            }

            if (
                raw_event["event_type"] in {"BACKEND_UNREACHABLE", "RECOVERY_FAILED"}
                and raw_event.get("severity") in {"ERROR", "CRITICAL"}
            ):
                event = bus.emit_failure(
                    event_payload,
                    category="ANDROID_SENTINEL",
                    confirmed_evidence=[
                        raw_event.get("summary", "Sentinel failure was observed.")
                    ],
                )
            else:
                event = bus.emit(event_payload)

            if event is None:
                reason = "persistence_failed"
                raise ValueError("event could not be safely persisted")

            returned_event_id = getattr(event, "event_id", None)
            if returned_event_id != event_id:
                reason = "acknowledgement_mismatch"
                raise ValueError("persisted event identifier did not match request")

            # The operational event is already durable. Recovery bookkeeping is a
            # secondary diagnostic action and must never turn a persisted event
            # into a rejected acknowledgement.
            if raw_event["event_type"] == "BACKEND_RECOVERED":
                signature = raw_event.get("failure_signature")
                if isinstance(signature, str) and signature.strip():
                    try:
                        bus.diagnostic_store.mark_recovered(
                            signature,
                            resolution=raw_event.get(
                                "summary",
                                "Sentinel observed backend recovery.",
                            ),
                            observed_at=raw_event.get("observed_at"),
                        )
                    except Exception:
                        pass

            processed += 1
            processed_event_ids.append(event_id)
        except Exception:
            rejected += 1
            rejected_event_ids.append(event_id if isinstance(event_id, str) else "")
            rejection_reasons.append({
                "event_id": event_id if isinstance(event_id, str) else None,
                "reason": reason,
            })

    return {
        "status": "partial" if rejected else "ok",
        "processed": processed,
        "rejected": rejected,
        "processed_event_ids": processed_event_ids,
        "rejected_event_ids": rejected_event_ids,
        "rejection_reasons": rejection_reasons,
        "max_batch": _MAX_SENTINEL_BATCH,
    }
