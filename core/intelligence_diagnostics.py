"""Read-only, bounded diagnostics for Amiki-owned intelligence subsystems.

The module reports metadata and state only. It never returns credentials, raw
learning content, user conversations, task payloads, or performs mutations.
"""
from __future__ import annotations

import os
from typing import Any

from core.diagnostics import DiagnosticIssue


def _count(payload: Any, *keys: str) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def inspect_learning() -> dict[str, Any]:
    """Return ALE policy, aggregate counts and evidence-quality metadata."""
    from core import learning_engine

    status = learning_engine.get_learning_status()
    pending = learning_engine.get_pending_reviews(kind="all", limit=200)
    knowledge = [item for item in pending if item.get("collection") == "knowledge"]
    experiences = [item for item in pending if item.get("collection") == "experiences"]
    missing_source = sum(not bool(item.get("source")) for item in knowledge)
    missing_evidence = sum(not item.get("evidence") for item in knowledge)
    return {
        "enabled": status.get("enabled") is True,
        "store_file_present": os.path.isfile(learning_engine.STORE_FILE),
        "store_file_readable": os.path.isfile(learning_engine.STORE_FILE)
        and os.access(learning_engine.STORE_FILE, os.R_OK),
        "mode": status.get("mode"),
        "autonomy": status.get("autonomy"),
        "training": status.get("training"),
        "pending_knowledge": status.get("pending_knowledge", 0),
        "pending_experiences": status.get("pending_experiences", 0),
        "approved_knowledge": status.get("approved_knowledge", 0),
        "active_skills": status.get("active_skills", 0),
        "evaluation_count": status.get("evaluation_count", 0),
        "fine_tuning_examples": status.get("fine_tuning_examples", 0),
        "model_weights_updated": status.get("model_weights_updated", False),
        "review_queue_observed": True,
        "review_queue_sample_size": len(pending),
        "pending_knowledge_sampled": len(knowledge),
        "pending_experiences_sampled": len(experiences),
        "pending_knowledge_missing_source": missing_source,
        "pending_knowledge_missing_evidence": missing_evidence,
        "review_required_before_promotion": True,
    }


def inspect_cloud() -> dict[str, Any]:
    """Probe the configured loopback Cloud control plane using Core-held auth."""
    from fastapi import HTTPException
    from interfaces.api.routes.cloud import (
        _configured_cloud_token,
        _request,
    )

    try:
        health = _request("GET", "/health", timeout_seconds=0.6)
        readiness = _request("GET", "/ready", timeout_seconds=0.6)
    except HTTPException as exc:
        raise DiagnosticIssue(
            "Amiki Cloud health/readiness probe failed; Cloud is unavailable or not ready."
        ) from exc

    if health.get("status") not in {"ok", "healthy"}:
        raise DiagnosticIssue("Amiki Cloud returned an unhealthy health response.")
    if readiness.get("status") not in {"ok", "ready", "healthy"}:
        raise DiagnosticIssue("Amiki Cloud readiness is not confirmed.")

    token_configured = len(_configured_cloud_token()) >= 32
    result: dict[str, Any] = {
        "health_observed": True,
        "readiness_observed": True,
        "authenticated_access_configured": token_configured,
        "jobs_count": None,
        "goals_count": None,
        "schedules_count": None,
        "worker_status": "unknown",
        "worker_enabled": None,
        "queued_jobs": None,
        "running_jobs": None,
        "schedule_api_status": "unknown",
    }
    if not token_configured:
        raise DiagnosticIssue(
            "Cloud health is reachable, but Core has no valid Cloud bearer token; protected state cannot be inspected."
        )

    try:
        jobs = _request("GET", "/jobs?limit=100", authenticated=True, timeout_seconds=0.6)
        goals = _request("GET", "/goals?limit=100", authenticated=True, timeout_seconds=0.6)
        worker = _request("GET", "/worker/status", authenticated=True, timeout_seconds=0.6)
        result["jobs_count"] = _count(jobs, "items", "jobs", "count")
        result["goals_count"] = _count(goals, "items", "goals", "count")
        result["worker_status"] = (
            "running" if worker.get("running") is True
            else "stopped" if worker.get("running") is False
            else "unknown"
        )
        result["worker_enabled"] = worker.get("enabled")
        result["queued_jobs"] = worker.get("queued_jobs")
        result["running_jobs"] = worker.get("running_jobs")
    except HTTPException as exc:
        raise DiagnosticIssue(
            "Cloud health is reachable, but authenticated jobs/goals/worker inspection failed."
        ) from exc

    try:
        schedules = _request("GET", "/schedules?limit=100", authenticated=True, timeout_seconds=0.6)
        result["schedules_count"] = _count(schedules, "items", "schedules", "count")
        result["schedule_api_status"] = "observed"
    except HTTPException:
        result["schedule_api_status"] = "unavailable"

    return result


def inspect_core_worker_configuration() -> dict[str, Any]:
    """Report configured worker intent, not a claim that a thread is alive."""
    enabled = os.getenv("AMIKI_CLOUD_AUTONOMY_WORKER_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    return {
        "enabled_by_environment": enabled,
        "runtime_thread_state": "not_observed",
        "note": "Environment configuration does not prove the worker thread is running.",
    }
