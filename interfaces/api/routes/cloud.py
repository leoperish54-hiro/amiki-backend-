"""Safe loopback proxy for the local Amiki Cloud control plane."""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from core.goal_planner import GoalPlanningError, generate_goal_tasks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cloud", tags=["cloud"])
DEFAULT_CLOUD_URL = "http://127.0.0.1:8001"
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
_LOOPBACK_CLIENT_HOSTS = frozenset(_ALLOWED_HOSTS)


class CloudJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["echo", "text_stats", "json_validate", "sha256"] = "echo"
    message: str = Field(min_length=1, max_length=2000)


class CloudPlanTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=1000)
    priority: int = Field(default=3, ge=1, le=5)
    agent: (
        Literal["research", "tutor", "engineering", "analysis", "inspection", "security"]
        | None
    ) = None


class CloudGoalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=2000)
    tasks: list[CloudPlanTaskInput] = Field(min_length=1, max_length=20)


class CloudGoalPlanningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=2000)


class CloudGoalScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=2000)
    interval_days: int = Field(ge=1, le=365, strict=True)
    first_run_at: datetime


class CloudSupervisorOutboxRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_ids: list[UUID] = Field(min_length=1, max_length=50)


class CloudTaskStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "in_progress", "completed", "failed", "cancelled"]


def _cloud_base_url() -> str:
    raw_url = os.getenv("AMIKI_CLOUD_BASE_URL", DEFAULT_CLOUD_URL).strip()
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="amiki_cloud_url_must_be_loopback",
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="amiki_cloud_url_invalid_port",
        ) from exc
    if port is not None and not 1 <= port <= 65535:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="amiki_cloud_url_invalid_port",
        )
    return raw_url.rstrip("/")


def _configured_cloud_token() -> str:
    token = os.getenv("AMIKI_CLOUD_API_TOKEN", "").strip()
    if token:
        return token
    token_file = Path(
        os.getenv(
            "AMIKI_CLOUD_TOKEN_FILE",
            str(Path.home() / ".config" / "amiki-cloud" / "api-token"),
        )
    ).expanduser()
    try:
        return token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _cloud_token() -> str:
    token = _configured_cloud_token()
    if len(token) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="amiki_cloud_auth_not_configured",
        )
    return token


def _require_outbox_owner(authorization: str | None) -> None:
    """Require the separately configured owner token for local outbox recovery."""
    expected = os.getenv("AMIKI_OWNER_REVIEW_TOKEN", "").strip()
    if len(expected) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="supervisor_outbox_owner_auth_not_configured",
        )

    scheme, separator, supplied = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not supplied
        or not secrets.compare_digest(supplied, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="supervisor_outbox_owner_authorization_required",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _require_outbox_loopback(request: Request, action: str) -> None:
    client = request.client
    if client is None or client.host not in _LOOPBACK_CLIENT_HOSTS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"supervisor_outbox_{action}_loopback_only",
        )


def _request(
    method: str,
    path: str,
    *,
    authenticated: bool = False,
    timeout_seconds: float = 3.0,
    **kwargs,
) -> dict:
    """Make a bounded request to the configured loopback Cloud API."""
    url = f"{_cloud_base_url()}/api/v1{path}"
    headers = dict(kwargs.pop("headers", {}))
    if authenticated:
        headers["Authorization"] = f"Bearer {_cloud_token()}"
    if headers:
        kwargs["headers"] = headers
    try:
        response = httpx.request(method, url, timeout=timeout_seconds, **kwargs)
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="amiki_cloud_timeout",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="amiki_cloud_unavailable",
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="amiki_cloud_invalid_response",
        ) from exc

    if response.is_error:
        if response.status_code == status.HTTP_401_UNAUTHORIZED:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="amiki_cloud_authorization_failed",
            )
        if response.status_code in {404, 409, 422}:
            if response.status_code == 404:
                if path.startswith("/goals"):
                    detail = "amiki_cloud_goal_not_found"
                elif path.startswith("/goal-schedules"):
                    detail = "amiki_cloud_goal_schedule_not_found"
                elif path.startswith("/monitor/events/"):
                    detail = "amiki_cloud_monitor_event_not_found"
                else:
                    detail = "amiki_cloud_job_not_found"
            elif response.status_code == 409:
                if (
                    path.startswith("/goal-schedules")
                    and isinstance(payload, dict)
                    and payload.get("detail") == "goal_schedule_idempotency_conflict"
                ):
                    detail = "amiki_cloud_goal_schedule_idempotency_conflict"
                elif (
                    path.startswith("/goals")
                    and isinstance(payload, dict)
                    and payload.get("detail") == "idempotency_key_conflict"
                ):
                    detail = "amiki_cloud_idempotency_conflict"
                elif path.startswith("/goals"):
                    detail = "amiki_cloud_goal_transition_rejected"
                elif path.startswith("/goal-schedules"):
                    detail = "amiki_cloud_goal_schedule_transition_rejected"
                else:
                    detail = "amiki_cloud_job_state_conflict"
            else:
                detail = "amiki_cloud_request_rejected"
            raise HTTPException(status_code=response.status_code, detail=detail)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="amiki_cloud_request_failed",
        )
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="amiki_cloud_invalid_response",
        )
    return payload


@router.get("/autonomy/status")
def cloud_autonomy_status() -> dict:
    """Read the Cloud-wide execution stop state through the authenticated proxy."""
    return _request("GET", "/autonomy/status", authenticated=True)


@router.post("/autonomy/stop")
def stop_cloud_autonomy() -> dict:
    """Stop Cloud execution after an explicit owner action in the client."""
    return _request("POST", "/autonomy/stop", authenticated=True)


@router.post("/autonomy/resume")
def resume_cloud_autonomy() -> dict:
    """Resume Cloud execution after an explicit owner action in the client."""
    return _request("POST", "/autonomy/resume", authenticated=True)


@router.get("/autonomy/worker/status")
def cloud_autonomy_worker_status(request: Request) -> dict:
    """Report worker liveness and bounded supervisor recovery state."""
    enabled = bool(getattr(request.app.state, "cloud_autonomy_worker_enabled", False))
    supervisor = getattr(request.app.state, "cloud_autonomy_worker_supervisor", None)
    if supervisor is not None:
        snapshot = supervisor.snapshot()
        payload = {
            "enabled": enabled,
            "running": bool(snapshot.get("worker_running", False)),
            "supervisor": snapshot,
        }
        if enabled:
            outbox = getattr(
                request.app.state,
                "cloud_autonomy_supervisor_event_outbox",
                None,
            )
            outbox_supervisor = getattr(
                request.app.state,
                "cloud_autonomy_supervisor_event_outbox_supervisor",
                None,
            )
            if outbox_supervisor is not None:
                supervisor_status = outbox_supervisor.snapshot()
                delivery_running = bool(supervisor_status.get("worker_running", False))
                delivery_supervisor = {
                    "state": str(supervisor_status.get("state", "unknown")),
                    "supervisor_running": bool(
                        supervisor_status.get("supervisor_running", False)
                    ),
                    "restart_count": int(supervisor_status.get("restart_count", 0)),
                    "restart_limit": int(supervisor_status.get("restart_limit", 0)),
                }
            else:
                outbox_thread = getattr(
                    request.app.state,
                    "cloud_autonomy_supervisor_event_outbox_thread",
                    None,
                )
                delivery_running = bool(
                    outbox_thread is not None and outbox_thread.is_alive()
                )
                delivery_supervisor = None

            if outbox is None:
                payload["event_outbox"] = {
                    "available": False,
                    "delivery_running": False,
                    "delivery_degraded": True,
                    "delivery_supervisor": delivery_supervisor,
                }
            else:
                try:
                    outbox_status = outbox.snapshot()
                    outbox_status["delivery_running"] = delivery_running
                    outbox_status["delivery_degraded"] = not delivery_running
                    outbox_status["delivery_supervisor"] = delivery_supervisor
                    payload["event_outbox"] = outbox_status
                except Exception:
                    logger.warning("amiki_cloud_supervisor_event_outbox_status_unavailable")
                    payload["event_outbox"] = {
                        "available": False,
                        "delivery_running": delivery_running,
                        "delivery_degraded": True,
                        "delivery_supervisor": delivery_supervisor,
                    }
        return payload

    worker_thread = getattr(request.app.state, "cloud_autonomy_worker_thread", None)
    running = (
        worker_thread.is_alive()
        if worker_thread is not None
        else bool(getattr(request.app.state, "cloud_autonomy_worker_running", False))
    )
    return {"enabled": enabled, "running": running}


@router.get("/autonomy/worker/outbox/dead-letters")
def list_cloud_autonomy_outbox_dead_letters(
    request: Request,
    limit: int = Query(default=50, ge=1, le=50),
    authorization: str | None = Header(default=None),
) -> dict:
    """List bounded dead-letter IDs without exposing payloads or database paths."""
    _require_outbox_owner(authorization)
    _require_outbox_loopback(request, "inspection")

    outbox = getattr(
        request.app.state,
        "cloud_autonomy_supervisor_event_outbox",
        None,
    )
    if outbox is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="supervisor_event_outbox_unavailable",
        )

    try:
        event_ids = outbox.list_dead_letter_ids(limit=limit)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="supervisor_outbox_inspection_rejected",
        ) from exc
    except Exception as exc:
        logger.warning("amiki_cloud_supervisor_event_outbox_inspection_failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="supervisor_outbox_inspection_unavailable",
        ) from exc
    return {"event_ids": event_ids, "count": len(event_ids)}


@router.post("/autonomy/worker/outbox/retry")
def retry_cloud_autonomy_outbox(
    body: CloudSupervisorOutboxRetryRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """Retry selected dead letters after explicit owner authorization."""
    _require_outbox_owner(authorization)
    _require_outbox_loopback(request, "retry")

    outbox = getattr(
        request.app.state,
        "cloud_autonomy_supervisor_event_outbox",
        None,
    )
    if outbox is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="supervisor_event_outbox_unavailable",
        )

    try:
        result = outbox.retry_dead_letters([str(event_id) for event_id in body.event_ids])
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="supervisor_outbox_retry_rejected",
        ) from exc
    except RuntimeError as exc:
        if str(exc) == "supervisor_outbox_capacity_reached":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="supervisor_outbox_capacity_reached",
            ) from exc
        logger.warning("amiki_cloud_supervisor_event_outbox_retry_failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="supervisor_outbox_retry_unavailable",
        ) from exc
    except Exception as exc:
        logger.warning("amiki_cloud_supervisor_event_outbox_retry_failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="supervisor_outbox_retry_unavailable",
        ) from exc

    if result["requeued"]:
        logger.info("amiki_cloud_supervisor_event_outbox_dead_letters_requeued")
    return result


@router.post(
    "/autonomy/worker/outbox/supervisor/restart",
    status_code=status.HTTP_202_ACCEPTED,
)
def restart_cloud_autonomy_outbox_supervisor(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """Restart an exhausted outbox delivery supervisor after owner authorization."""
    _require_outbox_owner(authorization)
    _require_outbox_loopback(request, "restart")

    supervisor = getattr(
        request.app.state,
        "cloud_autonomy_supervisor_event_outbox_supervisor",
        None,
    )
    stop_event = getattr(
        request.app.state,
        "cloud_autonomy_supervisor_event_outbox_stop_event",
        None,
    )
    if supervisor is None or stop_event is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="supervisor_outbox_supervisor_unavailable",
        )
    if stop_event.is_set():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="supervisor_outbox_application_stopping",
        )
    if supervisor.snapshot().get("state") != "exhausted":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="supervisor_outbox_restart_not_exhausted",
        )
    if not supervisor.start(
        stop_event,
        thread_name="amiki-cloud-supervisor-event-outbox-supervisor",
        allow_exhausted_restart=True,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="supervisor_outbox_restart_unavailable",
        )

    request.app.state.cloud_autonomy_supervisor_event_outbox_thread = (
        supervisor.supervisor_thread
    )
    return {
        "restarted": True,
        "component": "supervisor_outbox_delivery",
        "state": "starting",
    }


@router.get("/status")
def cloud_status() -> dict:
    """Report liveness/readiness without exposing or requiring the Cloud token."""
    health = _request("GET", "/health")
    readiness = _request("GET", "/ready")
    token = _configured_cloud_token()
    jobs_authorized = False
    worker_status = None
    if len(token) >= 32:
        try:
            _request("GET", "/jobs?limit=1", authenticated=True)
            jobs_authorized = True
        except HTTPException:
            # Keep liveness useful when protected Cloud operations are unavailable.
            jobs_authorized = False
        if jobs_authorized:
            try:
                worker_status = _request("GET", "/worker/status", authenticated=True)
            except HTTPException:
                worker_status = None

    return {
        "service": "amiki-cloud",
        "health": health,
        "readiness": readiness,
        "jobs_authorized": jobs_authorized,
        "worker": worker_status,
    }


@router.get("/jobs")
def list_cloud_jobs(limit: int = Query(default=50, ge=1, le=100)) -> dict:
    """List recent Cloud jobs through the authenticated backend proxy."""
    return _request("GET", f"/jobs?limit={limit}", authenticated=True)


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
def create_cloud_job(body: CloudJobRequest) -> dict:
    """Create a registered built-in task through Amiki Cloud."""
    return _request(
        "POST",
        "/jobs",
        authenticated=True,
        json={"kind": body.kind, "message": body.message},
    )


def _normalize_job_id(job_id: str) -> str:
    try:
        return str(UUID(job_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_job_id",
        ) from exc


@router.get("/jobs/{job_id}")
def get_cloud_job(job_id: str) -> dict:
    """Read a cloud job by UUID."""
    return _request("GET", f"/jobs/{_normalize_job_id(job_id)}", authenticated=True)


@router.post("/jobs/{job_id}/run")
def run_cloud_job(job_id: str) -> dict:
    """Run a built-in task, or return its current state if already claimed."""
    return _request("POST", f"/jobs/{_normalize_job_id(job_id)}/run", authenticated=True)


@router.post("/jobs/{job_id}/cancel")
def cancel_cloud_job(job_id: str) -> dict:
    """Cancel a queued simulated job."""
    return _request("POST", f"/jobs/{_normalize_job_id(job_id)}/cancel", authenticated=True)



def _normalize_goal_schedule_id(schedule_id: str) -> str:
    try:
        return str(UUID(schedule_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_goal_schedule_id",
        ) from exc


@router.post("/goal-schedules", status_code=status.HTTP_201_CREATED)
def create_cloud_goal_schedule(
    body: CloudGoalScheduleRequest,
    idempotency_key: Annotated[UUID | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    """Create a recurring goal intent and forward its optional retry identity."""
    headers = (
        {"Idempotency-Key": str(idempotency_key)}
        if idempotency_key is not None
        else {}
    )
    return _request(
        "POST",
        "/goal-schedules",
        authenticated=True,
        headers=headers,
        json=body.model_dump(mode="json"),
    )


@router.get("/goal-schedules")
def list_cloud_goal_schedules(limit: int = Query(default=50, ge=1, le=100)) -> dict:
    """List recurring goal intents through the authenticated Cloud proxy."""
    return _request(
        "GET",
        f"/goal-schedules?limit={limit}",
        authenticated=True,
    )


@router.get("/goal-schedules/{schedule_id}/runs")
def list_cloud_goal_schedule_runs(
    schedule_id: str,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    """Read bounded occurrence outcomes without exposing lease credentials."""
    normalized_schedule_id = _normalize_goal_schedule_id(schedule_id)
    return _request(
        "GET",
        f"/goal-schedules/{normalized_schedule_id}/runs?limit={limit}",
        authenticated=True,
    )


@router.post("/goal-schedules/{schedule_id}/{action}")
def change_cloud_goal_schedule(
    schedule_id: str,
    action: Literal["pause", "resume", "cancel"],
) -> dict:
    """Pause, resume, or cancel one recurring goal intent."""
    normalized_schedule_id = _normalize_goal_schedule_id(schedule_id)
    return _request(
        "POST",
        f"/goal-schedules/{normalized_schedule_id}/{action}",
        authenticated=True,
    )


@router.get("/goals")
def list_cloud_goals(limit: int = Query(default=50, ge=1, le=100)) -> dict:
    """List durable autonomous goals through the authenticated Cloud proxy."""
    return _request("GET", f"/goals?limit={limit}", authenticated=True)


@router.get("/monitor/status")
def cloud_monitor_status() -> dict:
    """Read the authenticated Cloud monitor lifecycle and event counts."""
    return _request("GET", "/monitor/status", authenticated=True)


@router.get("/monitor/events")
def list_cloud_monitor_events(
    state: Literal["open", "acknowledged", "resolved", "all"] = "open",
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    """Read bounded Cloud monitor events without exposing source task or job content."""
    return _request(
        "GET",
        f"/monitor/events?state={state}&limit={limit}",
        authenticated=True,
    )


@router.post("/monitor/events/{event_id}/acknowledge")
def acknowledge_cloud_monitor_event(
    event_id: int = PathParam(ge=1),
) -> dict:
    """Acknowledge one monitor event without suppressing future detection."""
    return _request(
        "POST",
        f"/monitor/events/{event_id}/acknowledge",
        authenticated=True,
    )


@router.post("/goals", status_code=status.HTTP_201_CREATED)
def create_cloud_goal(
    body: CloudGoalRequest,
    idempotency_key: Annotated[UUID | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    """Persist a bounded ordered plan and forward optional retry identity."""
    headers = (
        {"Idempotency-Key": str(idempotency_key)}
        if idempotency_key is not None
        else {}
    )
    return _request(
        "POST",
        "/goals",
        authenticated=True,
        headers=headers,
        json={
            "title": body.title,
            "objective": body.objective,
            "tasks": [task.model_dump(exclude_none=True) for task in body.tasks],
        },
    )


@router.post("/goals/plan", status_code=status.HTTP_201_CREATED)
def create_planned_cloud_goal(
    body: CloudGoalPlanningRequest,
    idempotency_key: Annotated[UUID | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    """Generate a safe plan and forward the stable goal-submission identity."""
    try:
        outcome_memory = _request("GET", "/autonomy/outcomes", authenticated=True)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            outcome_memory = {}
        else:
            raise

    try:
        tasks = generate_goal_tasks(
            body.title,
            body.objective,
            outcome_memory=outcome_memory,
        )
    except GoalPlanningError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="amiki_goal_planning_unavailable",
        ) from exc

    headers = (
        {"Idempotency-Key": str(idempotency_key)}
        if idempotency_key is not None
        else {}
    )
    return _request(
        "POST",
        "/goals",
        authenticated=True,
        headers=headers,
        json={
            "title": body.title,
            "objective": body.objective,
            "tasks": tasks,
        },
    )


def _normalize_goal_id(goal_id: str) -> str:
    try:
        return str(UUID(goal_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_goal_id",
        ) from exc


def _normalize_task_id(task_id: str) -> str:
    try:
        return str(UUID(task_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_task_id",
        ) from exc


@router.get("/goals/{goal_id}")
def get_cloud_goal(goal_id: str) -> dict:
    """Retrieve one persisted goal and its ordered task plan."""
    return _request(
        "GET", f"/goals/{_normalize_goal_id(goal_id)}", authenticated=True
    )


@router.post("/goals/{goal_id}/pause")
def pause_cloud_goal(goal_id: str) -> dict:
    """Pause one Cloud goal and revoke its active specialist leases."""
    normalized_goal_id = _normalize_goal_id(goal_id)
    return _request(
        "POST",
        f"/goals/{normalized_goal_id}/pause",
        authenticated=True,
    )


@router.post("/goals/{goal_id}/resume")
def resume_cloud_goal(goal_id: str) -> dict:
    """Resume one paused Cloud goal without bypassing lease grace."""
    normalized_goal_id = _normalize_goal_id(goal_id)
    return _request(
        "POST",
        f"/goals/{normalized_goal_id}/resume",
        authenticated=True,
    )


@router.post("/goals/{goal_id}/cancel")
def cancel_cloud_goal(goal_id: str) -> dict:
    """Cancel one Cloud goal and revoke all remaining task leases."""
    normalized_goal_id = _normalize_goal_id(goal_id)
    return _request(
        "POST",
        f"/goals/{normalized_goal_id}/cancel",
        authenticated=True,
    )


@router.patch("/goals/{goal_id}/tasks/{task_id}")
def update_cloud_goal_task(
    goal_id: str, task_id: str, body: CloudTaskStatusRequest
) -> dict:
    """Update a task status; Cloud enforces sequential and terminal-state rules."""
    normalized_goal_id = _normalize_goal_id(goal_id)
    normalized_task_id = _normalize_task_id(task_id)
    return _request(
        "PATCH",
        f"/goals/{normalized_goal_id}/tasks/{normalized_task_id}",
        authenticated=True,
        json=body.model_dump(),
    )
