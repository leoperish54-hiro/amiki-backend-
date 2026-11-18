"""UOSA owner device enrollment and scoped session endpoints."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from core import uosa_security

router = APIRouter()
session_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="UosaSessionBearer",
    description="Short-lived UOSA owner session credential.",
)


def require_uosa_scope(scope: str, *, minimum_auth_level: str | None = None):
    """Build a server-enforced UOSA scope and authentication-level dependency."""
    if scope not in uosa_security.ALLOWED_SCOPES:
        raise ValueError(f"Unknown UOSA scope: {scope}")

    async def dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(session_bearer),
    ) -> None:
        token = credentials.credentials if credentials else ""
        session = uosa_security.get_session(token)
        if session is None:
            raise HTTPException(
                status_code=401,
                detail="A valid UOSA owner session is required.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.uosa_session = session
        # An L2 token is single-use on the first attempted protected request,
        # including a wrong route, scope, or body. A failed substitution cannot
        # leave the elevated grant available for a later retry.
        if session["auth_level"] == "L2" and session["single_use"]:
            query = request.url.query
            bound_path = request.url.path + (f"?{query}" if query else "")
            transaction_hash = uosa_security.compute_transaction_binding(
                request.method, bound_path, await request.body()
            )
            if not uosa_security.consume_elevated_session(
                token, scope, transaction_hash
            ):
                raise HTTPException(
                    status_code=403,
                    detail="The L2 grant was consumed or does not match this transaction.",
                )
        if session["audience"] != uosa_security.OWNER_AUDIENCE:
            raise HTTPException(status_code=403, detail="The owner session audience is not permitted.")
        if scope not in session["scopes"]:
            raise HTTPException(status_code=403, detail="The owner session lacks the required scope.")
        required_level = minimum_auth_level or uosa_security.SCOPE_AUTH_LEVEL[scope]
        if required_level not in uosa_security.AUTH_LEVEL_RANK:
            raise RuntimeError("Invalid UOSA authentication-level policy.")
        if (
            uosa_security.AUTH_LEVEL_RANK.get(session["auth_level"], -1)
            < uosa_security.AUTH_LEVEL_RANK[required_level]
        ):
            raise HTTPException(
                status_code=403,
                detail=f"This operation requires {required_level} owner authorization.",
            )
        if required_level == "L2" and (
            session["auth_level"] != "L2" or not session["single_use"]
        ):
            raise HTTPException(
                status_code=403,
                detail="A fresh, transaction-bound L2 authorization is required.",
            )

    return dependency


def _raise_uosa_error(error: uosa_security.UosaError) -> None:
    headers = {"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None
    raise HTTPException(
        status_code=error.status_code,
        detail=str(error),
        headers=headers,
    ) from error


class EnrollmentChallengeInput(BaseModel):
    device_id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=80)
    public_key: str = Field(min_length=1, max_length=8192)


class SetupEnrollmentChallengeInput(BaseModel):
    setup_code: str = Field(min_length=32, max_length=100)
    device_id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=80)
    public_key: str = Field(min_length=1, max_length=8192)


class SignatureInput(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=100)
    signature: str = Field(min_length=1, max_length=8192)


class SessionChallengeInput(BaseModel):
    device_id: str = Field(min_length=1, max_length=80)
    audience: Literal["amiki-core"] = "amiki-core"
    scopes: list[str] = Field(min_length=1, max_length=64)


class GeneralGrantChallengeInput(BaseModel):
    device_id: str = Field(min_length=1, max_length=80)
    audience: Literal["amiki-core"] = "amiki-core"
    scopes: list[str] = Field(min_length=1, max_length=64)


class ElevationChallengeInput(BaseModel):
    auth_level: Literal["L2"] = "L2"
    scope: str = Field(min_length=1, max_length=100)
    transaction_hash: str = Field(min_length=64, max_length=64)


class RecoveryChallengeInput(BaseModel):
    recovery_code: str = Field(min_length=32, max_length=100)
    device_id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=80)
    public_key: str = Field(min_length=1, max_length=8192)


class KeyRotationChallengeInput(BaseModel):
    replacement_public_key: str = Field(min_length=1, max_length=8192)


class KeyRotationCompletionInput(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=128)
    old_signature: str = Field(min_length=1, max_length=512)
    new_signature: str = Field(min_length=1, max_length=512)


@router.post("/security/setup/enrollment-challenges")
def issue_initial_setup_challenge(payload: SetupEnrollmentChallengeInput):
    try:
        return uosa_security.create_setup_enrollment_challenge(
            setup_code=payload.setup_code,
            device_id=payload.device_id,
            label=payload.label,
            public_key_b64=payload.public_key,
        )
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)


@router.post("/security/setup/enroll")
def complete_initial_setup(payload: SignatureInput):
    try:
        return uosa_security.complete_setup_enrollment(
            challenge_id=payload.challenge_id,
            signature_b64=payload.signature,
        )
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)


@router.post(
    "/security/devices/enrollment-challenges",
    dependencies=[
        Depends(require_uosa_scope("uosa.device.enroll", minimum_auth_level="L1"))
    ],
)
def issue_device_enrollment_challenge(
    payload: EnrollmentChallengeInput,
    credentials: HTTPAuthorizationCredentials | None = Security(session_bearer),
):
    token = credentials.credentials if credentials else ""
    try:
        return uosa_security.create_enrollment_challenge(
            token=token,
            device_id=payload.device_id,
            label=payload.label,
            public_key_b64=payload.public_key,
        )
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)


@router.post(
    "/security/devices/enroll",
    dependencies=[Depends(require_uosa_scope("uosa.device.enroll"))],
)
def enroll_device(payload: SignatureInput, request: Request):
    session = getattr(request.state, "uosa_session", None)
    if session is None:
        raise HTTPException(status_code=401, detail="A valid UOSA owner session is required.")
    try:
        return uosa_security.complete_enrollment(
            challenge_id=payload.challenge_id,
            signature_b64=payload.signature,
            authorized_device_id=session["device_id"],
        )
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)


@router.post("/security/challenges")
def issue_owner_session_challenge(payload: SessionChallengeInput, request: Request):
    client_key = request.client.host if request.client is not None else "unknown"
    try:
        return uosa_security.create_session_challenge(
            device_id=payload.device_id,
            audience=payload.audience,
            scopes=payload.scopes,
            client_key=client_key,
        )
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)


@router.post("/security/sessions")
def create_owner_session(payload: SignatureInput):
    try:
        return uosa_security.complete_session(
            challenge_id=payload.challenge_id,
            signature_b64=payload.signature,
        )
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)




@router.post("/security/general-grants/challenges")
def issue_general_grant_challenge(
    payload: GeneralGrantChallengeInput,
    request: Request,
):
    client_key = request.client.host if request.client is not None else "unknown"
    try:
        return uosa_security.create_general_grant_challenge(
            device_id=payload.device_id,
            audience=payload.audience,
            scopes=payload.scopes,
            client_key=client_key,
        )
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)


@router.post("/security/general-grants")
def complete_general_grant(payload: SignatureInput):
    try:
        return uosa_security.complete_general_grant(
            challenge_id=payload.challenge_id,
            signature_b64=payload.signature,
        )
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)


@router.delete("/security/general-grants/current")
def revoke_current_general_grant(
    credentials: HTTPAuthorizationCredentials | None = Security(session_bearer),
):
    token = credentials.credentials if credentials else ""
    try:
        uosa_security.revoke_general_grant(token=token)
        return {
            "status": "revoked",
            "session_class": "general",
            "grant_persistent": False,
        }
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)


@router.post("/security/elevation-challenges")
def issue_owner_elevation_challenge(
    payload: ElevationChallengeInput,
    credentials: HTTPAuthorizationCredentials | None = Security(session_bearer),
):
    token = credentials.credentials if credentials else ""
    try:
        return uosa_security.create_elevation_challenge(
            token=token,
            auth_level=payload.auth_level,
            scope=payload.scope,
            transaction_hash=payload.transaction_hash,
        )
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)


@router.post("/security/elevated-sessions")
def create_elevated_owner_session(payload: SignatureInput):
    try:
        return uosa_security.complete_elevation(
            challenge_id=payload.challenge_id,
            signature_b64=payload.signature,
        )
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)



@router.post("/security/recovery/challenges")
def issue_owner_recovery_challenge(
    payload: RecoveryChallengeInput,
    request: Request,
):
    client_key = request.client.host if request.client is not None else "unknown"
    try:
        return uosa_security.create_recovery_challenge(
            recovery_code=payload.recovery_code,
            device_id=payload.device_id,
            label=payload.label,
            public_key_b64=payload.public_key,
            client_key=client_key,
        )
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)


@router.post("/security/recovery/complete")
def complete_owner_recovery(
    payload: SignatureInput,
    request: Request,
):
    client_key = request.client.host if request.client is not None else "unknown"
    try:
        return uosa_security.complete_recovery(
            challenge_id=payload.challenge_id,
            signature_b64=payload.signature,
            client_key=client_key,
        )
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)




@router.post(
    "/security/recovery/codes/rotate",
    dependencies=[Depends(require_uosa_scope("uosa.recovery.manage"))],
)
def rotate_owner_recovery_codes():
    try:
        return uosa_security.rotate_recovery_codes()
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)


@router.post(
    "/security/devices/{device_id}/rotation-challenges",
    dependencies=[
        Depends(require_uosa_scope("uosa.device.rotate", minimum_auth_level="L1"))
    ],
)
def issue_owner_key_rotation_challenge(
    device_id: str,
    payload: KeyRotationChallengeInput,
    credentials: HTTPAuthorizationCredentials | None = Security(session_bearer),
):
    token = credentials.credentials if credentials else ""
    try:
        return uosa_security.create_key_rotation_challenge(
            token=token,
            device_id=device_id,
            replacement_public_key_b64=payload.replacement_public_key,
        )
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)


@router.post(
    "/security/devices/{device_id}/rotate",
    dependencies=[Depends(require_uosa_scope("uosa.device.rotate"))],
)
def complete_owner_key_rotation(
    device_id: str,
    payload: KeyRotationCompletionInput,
    request: Request,
):
    session = getattr(request.state, "uosa_session", None)
    if session is None:
        raise HTTPException(status_code=401, detail="A valid UOSA owner session is required.")
    if session["device_id"] != device_id:
        raise HTTPException(
            status_code=403,
            detail="A device may rotate only its own enrolled key.",
        )
    try:
        return uosa_security.complete_key_rotation(
            device_id=device_id,
            challenge_id=payload.challenge_id,
            old_signature_b64=payload.old_signature,
            new_signature_b64=payload.new_signature,
        )
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)



@router.get(
    "/security/audit-events",
    dependencies=[Depends(require_uosa_scope("uosa.security.audit.read"))],
)
def read_owner_security_audit(limit: int = Query(default=100, ge=1, le=200)):
    try:
        return {
            "status": "ok",
            "items": uosa_security.list_security_events(limit=limit),
        }
    except uosa_security.UosaError as error:
        _raise_uosa_error(error)


@router.get(
    "/security/devices",
    dependencies=[Depends(require_uosa_scope("uosa.device.read"))],
)
def list_owner_devices():
    return {"status": "ok", "items": uosa_security.list_devices()}


@router.get("/security/sessions/current")
def inspect_owner_session(
    credentials: HTTPAuthorizationCredentials | None = Security(session_bearer),
):
    token = credentials.credentials if credentials else None
    session = uosa_security.get_session(token or "")
    if session is None:
        raise HTTPException(
            status_code=401,
            detail="A valid UOSA owner session is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"status": "active", **session}


@router.delete("/security/sessions/current")
def revoke_owner_session(
    credentials: HTTPAuthorizationCredentials | None = Security(session_bearer),
):
    token = credentials.credentials if credentials else None
    if not token or not uosa_security.revoke_session(token):
        raise HTTPException(status_code=401, detail="The owner session is not active.")
    return {"status": "revoked"}


@router.post(
    "/security/devices/{device_id}/revoke",
    dependencies=[Depends(require_uosa_scope("uosa.device.revoke"))],
)
def revoke_owner_device(device_id: str):
    if not uosa_security.revoke_device(device_id):
        raise HTTPException(status_code=404, detail="Active owner device was not found.")
    return {"status": "revoked", "device_id": device_id}
