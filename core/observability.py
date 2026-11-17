"""Bounded observability foundation for Amiki.

This module is intentionally standalone: normal Amiki behavior must never depend
on event collection succeeding. It provides validated event contracts, privacy
filtering, bounded seven-day operational storage, failure deduplication, and a
small permanent diagnostic record store. Component integration is deliberately
deferred to a later phase.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Mapping, Sequence


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "database", "observability.sqlite3")

SCHEMA_VERSION = 1
RETENTION_DAYS = 7
MAX_EVENT_ROWS = 5000
MAX_EVENT_METADATA_KEYS = 32
MAX_EVENT_STRING_CHARS = 500
MAX_SUMMARY_CHARS = 500
MAX_DIAGNOSTIC_LIST_ITEMS = 20
MAX_DIAGNOSTIC_STRING_CHARS = 1000
MAX_DIAGNOSTIC_ROWS = 1000

EVENT_TYPES = frozenset({
    "SYSTEM_STARTED",
    "SYSTEM_STOPPED",
    "SYSTEM_DEGRADED",
    "SYSTEM_RECOVERED",
    "API_STARTED",
    "API_STOPPED",
    "API_HEALTH_CHANGED",
    "API_REQUEST_STARTED",
    "API_REQUEST_COMPLETED",
    "API_REQUEST_FAILED",
    "MODEL_REQUEST",
    "MODEL_SUCCESS",
    "MODEL_FAILURE",
    "FALLBACK_TRIGGERED",
    "FALLBACK_SUCCESS",
    "FALLBACK_FAILURE",
    "AGENT_STARTED",
    "AGENT_COMPLETED",
    "AGENT_FAILED",
    "AGENT_HANDOFF",
    "TOOL_STARTED",
    "TOOL_COMPLETED",
    "TOOL_FAILED",
    "MEMORY_READ",
    "MEMORY_WRITE",
    "MEMORY_CLEAR",
    "MEMORY_FAILURE",
    "ATTACHMENT_RECEIVED",
    "ATTACHMENT_PROCESSED",
    "ATTACHMENT_REJECTED",
    "ATTACHMENT_FAILURE",
    "BACKEND_REACHABLE",
    "BACKEND_UNREACHABLE",
    "BACKEND_RECOVERED",
    "SENTINEL_STARTED",
    "SENTINEL_STOPPED",
    "SENTINEL_SYNC_SUCCESS",
    "SENTINEL_SYNC_FAILED",
    "RECOVERY_STARTED",
    "RECOVERY_SUCCESS",
    "RECOVERY_FAILED",
    "DIAGNOSTIC_RUN",
    "DIAGNOSTIC_FINDING",
    "DIAGNOSTIC_PROMOTED",
})

SEVERITIES = frozenset({
    "DEBUG",
    "INFO",
    "WARNING",
    "ERROR",
    "CRITICAL",
    "RECOVERY",
    "SECURITY",
})

PROVENANCE = frozenset({
    "observed",
    "derived",
    "reported",
    "inferred",
})

DIAGNOSTIC_STATUSES = frozenset({
    "NEW",
    "ACTIVE",
    "RECOVERED",
    "RESOLVED",
    "RECURRING",
    "SUPERSEDED",
})

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "prompt",
    "conversation",
    "attachment",
    "payload",
    "headers",
    "raw_content",
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)bearer\s+[a-z0-9._-]{12,}"),
    re.compile(r"(?i)(?:password|passwd|secret)\s*[:=]\s*[^\s,;]+"),
)


class ObservabilityContractError(ValueError):
    """Raised when an event or diagnostic record violates the V1 contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return _utc_now()
    candidate = value.strip()
    if len(candidate) > 80:
        raise ObservabilityContractError("timestamp is too long")
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        raise ObservabilityContractError("timestamp must be ISO-8601") from None
    if parsed.tzinfo is None:
        raise ObservabilityContractError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _redact_string(value: str, *, limit: int = MAX_EVENT_STRING_CHARS) -> str:
    text = value.strip()
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:limit]


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _sanitize_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[REDACTED]"

    if isinstance(value, str):
        return _redact_string(value)

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= MAX_EVENT_METADATA_KEYS:
                break
            if not isinstance(raw_key, str):
                raise ObservabilityContractError("metadata keys must be strings")
            key = raw_key.strip()[:80]
            if not key:
                raise ObservabilityContractError("metadata keys must be non-empty")
            clean[key] = (
                "[REDACTED]"
                if _is_sensitive_key(key)
                else _sanitize_value(raw_value, depth=depth + 1)
            )
        return clean

    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, depth=depth + 1) for item in value[:MAX_DIAGNOSTIC_LIST_ITEMS]]

    raise ObservabilityContractError("metadata contains an unsupported value")


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raise ObservabilityContractError("metadata must be JSON serializable") from None


def _bounded_text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ObservabilityContractError(f"{name} must be a non-empty string")
    return _redact_string(value, limit=limit)


@dataclass(frozen=True)
class ObservationEvent:
    schema_version: int
    event_id: str
    event_type: str
    severity: str
    source: str
    component: str
    observed_at: str
    correlation_id: str | None
    parent_event_id: str | None
    provenance: str
    status: str
    summary: str
    metadata: dict[str, Any]
    failure_signature: str | None = None
    sequence_number: int | None = None
    def __post_init__(self) -> None:
        """Enforce the event contract even when instantiated directly."""
        if self.schema_version != SCHEMA_VERSION:
            raise ObservabilityContractError("unsupported event schema version")
        if self.event_type not in EVENT_TYPES:
            raise ObservabilityContractError("event_type is not in the V1 taxonomy")
        if self.severity not in SEVERITIES:
            raise ObservabilityContractError("severity is not in the V1 taxonomy")
        if self.provenance not in PROVENANCE:
            raise ObservabilityContractError("invalid provenance")

        for name, value, limit in (
            ("event_id", self.event_id, 128),
            ("source", self.source, 80),
            ("component", self.component, 120),
            ("status", self.status, 40),
            ("summary", self.summary, MAX_SUMMARY_CHARS),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ObservabilityContractError(f"{name} is invalid")

        for name, value in (
            ("correlation_id", self.correlation_id),
            ("parent_event_id", self.parent_event_id),
        ):
            if value is not None and (
                not isinstance(value, str) or not value.strip() or len(value) > 128
            ):
                raise ObservabilityContractError(f"{name} is invalid")

        if self.failure_signature is not None and (
            not isinstance(self.failure_signature, str)
            or not self.failure_signature.strip()
            or len(self.failure_signature) > 160
        ):
            raise ObservabilityContractError("failure_signature is invalid")
        if self.sequence_number is not None and (
            not isinstance(self.sequence_number, int)
            or isinstance(self.sequence_number, bool)
            or self.sequence_number < 0
        ):
            raise ObservabilityContractError("sequence_number must be a non-negative integer")
        normalized_timestamp = _normalize_timestamp(self.observed_at)
        clean_metadata = _sanitize_value(self.metadata)
        if not isinstance(clean_metadata, dict):
            raise ObservabilityContractError("metadata must be a mapping")

        object.__setattr__(self, "source", _redact_string(self.source, limit=80))
        object.__setattr__(self, "component", _redact_string(self.component, limit=120))
        object.__setattr__(self, "status", _redact_string(self.status, limit=40))
        object.__setattr__(self, "summary", _redact_string(self.summary, limit=MAX_SUMMARY_CHARS))
        object.__setattr__(self, "observed_at", normalized_timestamp)
        object.__setattr__(self, "metadata", clean_metadata)

    @classmethod
    def create(
        cls,
        *,
        event_type: str,
        severity: str,
        source: str,
        component: str,
        summary: str,
        status: str = "completed",
        provenance: str = "observed",
        correlation_id: str | None = None,
        parent_event_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        failure_signature: str | None = None,
        sequence_number: int | None = None,
        observed_at: str | None = None,
        event_id: str | None = None,
    ) -> "ObservationEvent":
        if event_type not in EVENT_TYPES:
            raise ObservabilityContractError("event_type is not in the V1 taxonomy")
        if severity not in SEVERITIES:
            raise ObservabilityContractError("severity is not in the V1 taxonomy")
        if provenance not in PROVENANCE:
            raise ObservabilityContractError("invalid provenance")
        source = _bounded_text(source, "source", 80)
        component = _bounded_text(component, "component", 120)
        status = _bounded_text(status, "status", 40)
        summary = _bounded_text(summary, "summary", MAX_SUMMARY_CHARS)

        for name, identifier in (
            ("event_id", event_id),
            ("correlation_id", correlation_id),
            ("parent_event_id", parent_event_id),
        ):
            if identifier is not None:
                if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128:
                    raise ObservabilityContractError(f"{name} is invalid")

        if failure_signature is not None:
            failure_signature = _bounded_text(failure_signature, "failure_signature", 160)

        if sequence_number is not None and (
            not isinstance(sequence_number, int) or isinstance(sequence_number, bool) or sequence_number < 0
        ):
            raise ObservabilityContractError("sequence_number must be a non-negative integer")

        clean_metadata = _sanitize_value(dict(metadata or {}))
        if not isinstance(clean_metadata, dict):
            raise ObservabilityContractError("metadata must be a mapping")

        return cls(
            schema_version=SCHEMA_VERSION,
            event_id=(event_id or f"evt_{uuid.uuid4().hex}"),
            event_type=event_type,
            severity=severity,
            source=source,
            component=component,
            observed_at=_normalize_timestamp(observed_at),
            correlation_id=correlation_id,
            parent_event_id=parent_event_id,
            provenance=provenance,
            status=status,
            summary=summary,
            metadata=clean_metadata,
            failure_signature=failure_signature,
            sequence_number=sequence_number,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "severity": self.severity,
            "source": self.source,
            "component": self.component,
            "observed_at": self.observed_at,
            "correlation_id": self.correlation_id,
            "parent_event_id": self.parent_event_id,
            "provenance": self.provenance,
            "status": self.status,
            "summary": self.summary,
            "metadata": dict(self.metadata),
            "failure_signature": self.failure_signature,
            "sequence_number": self.sequence_number,
        }


def build_failure_signature(
    component: str,
    event_type: str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Create a deterministic, secret-free failure signature."""
    if not isinstance(component, str) or not component.strip():
        raise ObservabilityContractError("component is required for a failure signature")
    if not isinstance(event_type, str) or not event_type.strip():
        raise ObservabilityContractError("event_type is required for a failure signature")

    clean = _sanitize_value(dict(metadata or {}))
    if not isinstance(clean, dict):
        clean = {}

    relevant = {
        key: clean[key]
        for key in ("error_class", "http_status", "status", "reason")
        if key in clean and isinstance(clean[key], (str, int, float, bool))
    }
    material = json.dumps(
        {
            "component": component.strip(),
            "event_type": event_type.strip(),
            "relevant": relevant,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return f"diag_{digest}"


class EventStore:
    """SQLite-backed bounded operational event store."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        *,
        retention_days: int = RETENTION_DAYS,
        max_rows: int = MAX_EVENT_ROWS,
    ) -> None:
        if not isinstance(retention_days, int) or retention_days < 1:
            raise ValueError("retention_days must be a positive integer")
        if not isinstance(max_rows, int) or max_rows < 1:
            raise ValueError("max_rows must be a positive integer")
        self.db_path = os.path.abspath(db_path)
        self.retention_days = retention_days
        self.max_rows = max_rows
        self._lock = RLock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 2000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS observability_events (
                    event_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    source TEXT NOT NULL,
                    component TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    correlation_id TEXT,
                    parent_event_id TEXT,
                    provenance TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    failure_signature TEXT,
                    sequence_number INTEGER
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_observability_events_observed_at "
                "ON observability_events(observed_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_observability_events_correlation "
                "ON observability_events(correlation_id)"
            )
            connection.commit()

    def append(self, event: ObservationEvent) -> None:
        if not isinstance(event, ObservationEvent):
            raise ObservabilityContractError("append expects an ObservationEvent")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO observability_events (
                    event_id, schema_version, event_type, severity, source,
                    component, observed_at, correlation_id, parent_event_id,
                    provenance, status, summary, metadata_json,
                    failure_signature, sequence_number
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.schema_version,
                    event.event_type,
                    event.severity,
                    event.source,
                    event.component,
                    event.observed_at,
                    event.correlation_id,
                    event.parent_event_id,
                    event.provenance,
                    event.status,
                    event.summary,
                    _safe_json(event.metadata),
                    event.failure_signature,
                    event.sequence_number,
                ),
            )
            connection.commit()

    def prune(self, *, now: str | None = None) -> int:
        normalized_now = _normalize_timestamp(now)
        cutoff = datetime.fromisoformat(normalized_now) - timedelta(days=self.retention_days)
        deleted = 0
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM observability_events WHERE observed_at < ?",
                (cutoff.isoformat(),),
            )
            deleted += cursor.rowcount

            row = connection.execute(
                "SELECT COUNT(*) AS count FROM observability_events"
            ).fetchone()
            count = int(row["count"])
            overflow = max(0, count - self.max_rows)
            if overflow:
                cursor = connection.execute(
                    """
                    DELETE FROM observability_events
                    WHERE event_id IN (
                        SELECT event_id
                        FROM observability_events
                        ORDER BY observed_at ASC, event_id ASC
                        LIMIT ?
                    )
                    """,
                    (overflow,),
                )
                deleted += cursor.rowcount

            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return deleted

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, 500)
        # Enforce the rolling retention contract even during quiet periods.
        # A read must never surface operational events that expired while no
        # new event was emitted.
        self.prune()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM observability_events
                ORDER BY observed_at DESC, event_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            {
                "schema_version": row["schema_version"],
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "severity": row["severity"],
                "source": row["source"],
                "component": row["component"],
                "observed_at": row["observed_at"],
                "correlation_id": row["correlation_id"],
                "parent_event_id": row["parent_event_id"],
                "provenance": row["provenance"],
                "status": row["status"],
                "summary": row["summary"],
                "metadata": json.loads(row["metadata_json"]),
                "failure_signature": row["failure_signature"],
                "sequence_number": row["sequence_number"],
            }
            for row in rows
        ]

    def count(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM observability_events"
            ).fetchone()
        return int(row["count"])


class DiagnosticStore:
    """Permanent, compact memory of meaningful recurring failures."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = os.path.abspath(db_path)
        self._lock = RLock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 2000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS diagnostic_records (
                    diagnostic_id TEXT PRIMARY KEY,
                    failure_signature TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    symptoms_json TEXT NOT NULL,
                    confirmed_evidence_json TEXT NOT NULL,
                    hypotheses_json TEXT NOT NULL,
                    known_resolution TEXT,
                    related_event_ids_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_diagnostics_last_seen "
                "ON diagnostic_records(last_seen)"
            )
            connection.commit()

    def promote(
        self,
        event: ObservationEvent,
        *,
        category: str,
        confirmed_evidence: Sequence[str],
        hypotheses: Sequence[str] = (),
        known_resolution: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(event, ObservationEvent):
            raise ObservabilityContractError("event must be an ObservationEvent")
        if event.severity not in {"WARNING", "ERROR", "CRITICAL"}:
            raise ObservabilityContractError("only actionable severities can be promoted")
        category = _bounded_text(category, "category", 80)
        if event.provenance == "inferred":
            raise ObservabilityContractError("inferred events cannot establish permanent diagnostics")

        signature = event.failure_signature or build_failure_signature(
            event.component,
            event.event_type,
            metadata=event.metadata,
        )
        evidence = [
            _bounded_text(item, "confirmed evidence", MAX_DIAGNOSTIC_STRING_CHARS)
            for item in confirmed_evidence[:MAX_DIAGNOSTIC_LIST_ITEMS]
            if isinstance(item, str) and item.strip()
        ]
        inferred = [
            _bounded_text(item, "hypothesis", MAX_DIAGNOSTIC_STRING_CHARS)
            for item in hypotheses[:MAX_DIAGNOSTIC_LIST_ITEMS]
            if isinstance(item, str) and item.strip()
        ]
        resolution = (
            _bounded_text(known_resolution, "known_resolution", MAX_DIAGNOSTIC_STRING_CHARS)
            if known_resolution is not None
            else None
        )

        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM diagnostic_records WHERE failure_signature = ?",
                (signature,),
            ).fetchone()

            if existing is None:
                diagnostic_id = f"diag_{uuid.uuid4().hex}"
                status = "NEW"
                connection.execute(
                    """
                    INSERT INTO diagnostic_records (
                        diagnostic_id, failure_signature, category, severity,
                        first_seen, last_seen, occurrence_count, status,
                        provenance, symptoms_json, confirmed_evidence_json,
                        hypotheses_json, known_resolution, related_event_ids_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        diagnostic_id,
                        signature,
                        category,
                        event.severity,
                        event.observed_at,
                        event.observed_at,
                        1,
                        status,
                        "observed" if event.provenance in {"observed", "derived"} else event.provenance,
                        _safe_json([event.summary]),
                        _safe_json(evidence),
                        _safe_json(inferred),
                        resolution,
                        _safe_json([event.event_id]),
                    ),
                )
            else:
                previous_count = int(existing["occurrence_count"])
                status = "RECURRING" if previous_count >= 1 else existing["status"]
                symptoms = json.loads(existing["symptoms_json"])
                evidence_history = json.loads(existing["confirmed_evidence_json"])
                hypothesis_history = json.loads(existing["hypotheses_json"])
                event_ids = json.loads(existing["related_event_ids_json"])

                if event.summary not in symptoms:
                    symptoms.append(event.summary)
                for item in evidence:
                    if item not in evidence_history:
                        evidence_history.append(item)
                for item in inferred:
                    if item not in hypothesis_history:
                        hypothesis_history.append(item)
                if event.event_id not in event_ids:
                    event_ids.append(event.event_id)

                connection.execute(
                    """
                    UPDATE diagnostic_records
                    SET category = ?,
                        severity = ?,
                        last_seen = ?,
                        occurrence_count = ?,
                        status = ?,
                        symptoms_json = ?,
                        confirmed_evidence_json = ?,
                        hypotheses_json = ?,
                        known_resolution = COALESCE(?, known_resolution),
                        related_event_ids_json = ?
                    WHERE failure_signature = ?
                    """,
                    (
                        category,
                        event.severity,
                        event.observed_at,
                        previous_count + 1,
                        status,
                        _safe_json(symptoms[-MAX_DIAGNOSTIC_LIST_ITEMS:]),
                        _safe_json(evidence_history[-MAX_DIAGNOSTIC_LIST_ITEMS:]),
                        _safe_json(hypothesis_history[-MAX_DIAGNOSTIC_LIST_ITEMS:]),
                        resolution,
                        _safe_json(event_ids[-MAX_DIAGNOSTIC_LIST_ITEMS:]),
                        signature,
                    ),
                )

            row = connection.execute(
                "SELECT COUNT(*) AS count FROM diagnostic_records"
            ).fetchone()
            overflow = max(0, int(row["count"]) - MAX_DIAGNOSTIC_ROWS)
            if overflow:
                connection.execute(
                    """
                    DELETE FROM diagnostic_records
                    WHERE diagnostic_id IN (
                        SELECT diagnostic_id
                        FROM diagnostic_records
                        WHERE status IN ('RESOLVED', 'SUPERSEDED', 'RECOVERED')
                        ORDER BY last_seen ASC, diagnostic_id ASC
                        LIMIT ?
                    )
                    """,
                    (overflow,),
                )

            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        return self.get(signature)


    def mark_recovered(
        self,
        failure_signature: str,
        *,
        resolution: str | None = None,
        observed_at: str | None = None,
    ) -> dict[str, Any] | None:
        failure_signature = _bounded_text(
            failure_signature,
            "failure_signature",
            160,
        )
        clean_resolution = (
            _bounded_text(resolution, "resolution", MAX_DIAGNOSTIC_STRING_CHARS)
            if resolution is not None
            else None
        )
        timestamp = _normalize_timestamp(observed_at)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE diagnostic_records
                SET status = 'RECOVERED',
                    last_seen = ?,
                    known_resolution = COALESCE(?, known_resolution)
                WHERE failure_signature = ?
                """,
                (timestamp, clean_resolution, failure_signature),
            )
            connection.commit()

        return self.get(failure_signature)

    def get(self, failure_signature: str) -> dict[str, Any] | None:
        failure_signature = _bounded_text(
            failure_signature,
            "failure_signature",
            160,
        )
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM diagnostic_records WHERE failure_signature = ?",
                (failure_signature,),
            ).fetchone()

        if row is None:
            return None

        return {
            "diagnostic_id": row["diagnostic_id"],
            "failure_signature": row["failure_signature"],
            "category": row["category"],
            "severity": row["severity"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "occurrence_count": row["occurrence_count"],
            "status": row["status"],
            "provenance": row["provenance"],
            "symptoms": json.loads(row["symptoms_json"]),
            "confirmed_evidence": json.loads(row["confirmed_evidence_json"]),
            "hypotheses": json.loads(row["hypotheses_json"]),
            "known_resolution": row["known_resolution"],
            "related_event_ids": json.loads(row["related_event_ids_json"]),
        }

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, 500)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT failure_signature
                FROM diagnostic_records
                ORDER BY last_seen DESC, diagnostic_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            record
            for record in (
                self.get(row["failure_signature"])
                for row in rows
            )
            if record is not None
        ]


class ObservabilityBus:
    """Best-effort emission facade that never blocks the primary application."""

    def __init__(
        self,
        event_store: EventStore | None = None,
        diagnostic_store: DiagnosticStore | None = None,
    ) -> None:
        self.event_store = event_store or EventStore()
        self.diagnostic_store = diagnostic_store or DiagnosticStore()

    def emit(self, event: ObservationEvent | Mapping[str, Any]) -> ObservationEvent | None:
        try:
            if isinstance(event, ObservationEvent):
                candidate = event
            elif isinstance(event, Mapping):
                candidate = ObservationEvent.create(**dict(event))
            else:
                return None
            self.event_store.append(candidate)
            self.event_store.prune()
            return candidate
        except Exception:
            # Observability is intentionally non-blocking. A broken logger,
            # database, or malformed optional event must never break Amiki.
            return None

    def emit_failure(
        self,
        event: ObservationEvent | Mapping[str, Any],
        *,
        category: str,
        confirmed_evidence: Sequence[str],
        hypotheses: Sequence[str] = (),
        known_resolution: str | None = None,
    ) -> ObservationEvent | None:
        """Emit an actionable failure and best-effort promote it to diagnostics."""
        try:
            if isinstance(event, ObservationEvent):
                candidate = event
            elif isinstance(event, Mapping):
                payload = dict(event)
                payload.setdefault("severity", "ERROR")
                candidate = ObservationEvent.create(**payload)
            else:
                return None

            if candidate.severity not in {"WARNING", "ERROR", "CRITICAL"}:
                return None

            if not candidate.failure_signature:
                candidate = ObservationEvent.create(
                    event_id=candidate.event_id,
                    event_type=candidate.event_type,
                    severity=candidate.severity,
                    source=candidate.source,
                    component=candidate.component,
                    observed_at=candidate.observed_at,
                    correlation_id=candidate.correlation_id,
                    parent_event_id=candidate.parent_event_id,
                    provenance=candidate.provenance,
                    status=candidate.status,
                    summary=candidate.summary,
                    metadata=candidate.metadata,
                    failure_signature=build_failure_signature(
                        candidate.component,
                        candidate.event_type,
                        metadata=candidate.metadata,
                    ),
                    sequence_number=candidate.sequence_number,
                )

            # Once the operational event is persisted, it is safe to ACK it
            # even if permanent diagnostic promotion fails.  Promotion is a
            # secondary observability path and must not cause Android Sentinel
            # to retry an event that is already stored.
            self.event_store.append(candidate)
            try:
                self.event_store.prune()
            except Exception:
                # Retention cleanup is best-effort; the event was already stored.
                pass

            try:
                self.promote(
                    candidate,
                    category=category,
                    confirmed_evidence=confirmed_evidence,
                    hypotheses=hypotheses,
                    known_resolution=known_resolution,
                )
            except Exception:
                # Diagnostic promotion is best-effort and must never invalidate
                # successful operational event ingestion.
                pass

            return candidate
        except Exception:
            return None

    def promote(
        self,
        event: ObservationEvent,
        *,
        category: str,
        confirmed_evidence: Sequence[str],
        hypotheses: Sequence[str] = (),
        known_resolution: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            return self.diagnostic_store.promote(
                event,
                category=category,
                confirmed_evidence=confirmed_evidence,
                hypotheses=hypotheses,
                known_resolution=known_resolution,
            )
        except Exception:
            return None


_DEFAULT_BUS: ObservabilityBus | None = None
_DEFAULT_LOCK = RLock()


def get_observability() -> ObservabilityBus:
    """Return the process-level best-effort observability facade."""
    global _DEFAULT_BUS
    with _DEFAULT_LOCK:
        if _DEFAULT_BUS is None:
            _DEFAULT_BUS = ObservabilityBus()
        return _DEFAULT_BUS


__all__ = [
    "DIAGNOSTIC_STATUSES",
    "EVENT_TYPES",
    "PROVENANCE",
    "SEVERITIES",
    "DiagnosticStore",
    "EventStore",
    "ObservationEvent",
    "ObservabilityBus",
    "ObservabilityContractError",
    "build_failure_signature",
    "get_observability",
    "RETENTION_DAYS",
    "MAX_EVENT_ROWS",
]
