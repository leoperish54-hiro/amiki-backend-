"""Read-only bridge from Amiki's local API to the independent Cipher service."""

from __future__ import annotations

import os
import re

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from core.integrations.cipher_client import CipherClient, CipherIntegrationError

router = APIRouter(prefix="/cipher", tags=["cipher"])

DEFAULT_CIPHER_URL = "http://127.0.0.1:8765"
_HTTP_ERROR = re.compile(r"HTTP ([0-9]{3})")


class CipherInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
    )


def _client() -> CipherClient:
    """Build a loopback-only client; the dedicated Cipher credential stays server-side."""
    # Prefer the documented least-privilege Amiki principal; retain the legacy token only for compatibility.
    token = os.getenv("AMIKI_READ_TOKEN", "").strip() or os.getenv("CIPHER_API_TOKEN", "").strip()
    if len(token) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="cipher_auth_not_configured",
        )
    base_url = os.getenv("CIPHER_API_URL", DEFAULT_CIPHER_URL).strip()
    try:
        return CipherClient(base_url, token, timeout=15.0)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="cipher_configuration_invalid",
        ) from None


def _upstream_error(exc: CipherIntegrationError) -> HTTPException:
    """Translate upstream failures to stable, non-sensitive API error codes."""
    match = _HTTP_ERROR.search(str(exc))
    upstream_status = int(match.group(1)) if match else 0
    if upstream_status == 401:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="cipher_authorization_failed",
        )
    if upstream_status == 403:
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cipher_request_denied",
        )
    if upstream_status == 400:
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cipher_request_rejected",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="cipher_upstream_unavailable",
    )


@router.get("/status")
def cipher_status() -> dict:
    """Return verified Cipher liveness and audit-chain metadata, never a security verdict."""
    client = _client()
    try:
        health = client.health()
    except CipherIntegrationError as exc:
        raise _upstream_error(exc) from None

    audit_valid = health.get("audit_chain_valid")
    event_count = health.get("audit_event_count")
    if (
        not isinstance(audit_valid, bool)
        or isinstance(event_count, bool)
        or not isinstance(event_count, int)
        or event_count < 0
        or health.get("mode") != "loopback_read_only_prototype"
        or health.get("action_execution_enabled") is not False
    ):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="cipher_health_contract_invalid",
        )

    return {
        "service": "cipher",
        "status": health["status"],
        "connected": True,
        "mode": "loopback_read_only_prototype",
        "action_execution_enabled": False,
        "audit_chain_valid": audit_valid,
        "audit_event_count": event_count,
    }


@router.post("/inspect")
def cipher_inspect(request: CipherInspectRequest) -> dict:
    """Request bounded inspection of a Cipher-registered root by identifier only."""
    client = _client()
    try:
        report = client.inspect(request.root_id)
    except CipherIntegrationError as exc:
        raise _upstream_error(exc) from None

    complete = report["complete"]
    return {
        **report,
        "status": "complete" if complete else "partial",
        "execution_performed": False,
        "detected_values_included": False,
    }


__all__ = ["router"]
