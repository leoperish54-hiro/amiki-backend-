"""Bearer authentication for the local Amiki Core API.

The API token authenticates the Android/local client to Core. It is deliberately
separate from ALE owner-review, Cloud service, and Cipher service credentials.
"""
import os
import secrets

from fastapi import Header, HTTPException, status


API_TOKEN_ENV = "AMIKI_API_TOKEN"
MIN_API_TOKEN_LENGTH = 32


def api_auth_configured() -> bool:
    """Return whether a sufficiently strong API token is configured."""
    return len(os.getenv(API_TOKEN_ENV, "").strip()) >= MIN_API_TOKEN_LENGTH


def require_api_auth(authorization: str | None = Header(default=None)) -> None:
    """Reject every protected API request unless its bearer token matches."""
    expected = os.getenv(API_TOKEN_ENV, "").strip()
    if len(expected) < MIN_API_TOKEN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="api_auth_not_configured",
        )

    parts = (authorization or "").strip().split(None, 1)
    if len(parts) != 2 or parts[0].casefold() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="api_auth_required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    supplied = parts[1].strip()
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="api_auth_invalid",
            headers={"WWW-Authenticate": "Bearer"},
        )
