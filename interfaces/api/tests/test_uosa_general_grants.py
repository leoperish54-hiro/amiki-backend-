from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from core import uosa_security
from interfaces.api.server.app import app


client = TestClient(app)


@pytest.fixture
def uosa_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AMIKI_UOSA_DB_PATH", str(tmp_path / "uosa.sqlite3"))
    monkeypatch.setenv(
        "AMIKI_UOSA_SETUP_CODE_FILE", str(tmp_path / "uosa-setup-code")
    )
    monkeypatch.delenv("AMIKI_OWNER_REVIEW_TOKEN", raising=False)
    yield


def _keypair():
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, base64.b64encode(public_der).decode("ascii")


def _sign(private_key, challenge):
    signature = private_key.sign(
        challenge["message"].encode("utf-8"),
        ec.ECDSA(hashes.SHA256()),
    )
    return base64.b64encode(signature).decode("ascii")


def _enroll(private_key, public_key, device_id="grant-test-device"):
    setup_code = uosa_security.create_setup_code()["setup_code"]
    challenge = client.post(
        "/api/v1/security/setup/enrollment-challenges",
        json={
            "setup_code": setup_code,
            "device_id": device_id,
            "label": "grant pytest device",
            "public_key": public_key,
        },
    )
    assert challenge.status_code == 200, challenge.text
    completed = client.post(
        "/api/v1/security/setup/enroll",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(private_key, challenge.json()),
        },
    )
    assert completed.status_code == 200, completed.text
    return completed.json()


def _issue_general_grant(owner_private_key, continuity_public_key):
    request = {
        "device_id": "grant-test-device",
        "audience": "amiki-core",
        "scopes": [
            "chat.use",
            "agents.run",
            "device.read",
            "memory.read",
            "settings.write",
            "uosa.device.read",
            "uosa.security.audit.read",
        ],
        "continuity_public_key": continuity_public_key,
    }
    challenge = client.post(
        "/api/v1/security/general-grants/challenges",
        json=request,
    )
    assert challenge.status_code == 200, challenge.text
    completed = client.post(
        "/api/v1/security/general-grants",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(owner_private_key, challenge.json()),
        },
    )
    assert completed.status_code == 200, completed.text
    return completed.json()


def test_general_grant_is_persistent_refreshable_and_revocable(uosa_db):
    owner_private, owner_public = _keypair()
    continuity_private, continuity_public = _keypair()
    _enroll(owner_private, owner_public)

    issued = _issue_general_grant(owner_private, continuity_public)
    assert issued["session_class"] == "general"
    assert issued["grant_persistent"] is True
    assert issued["expires_in_seconds"] == uosa_security.GENERAL_SESSION_TTL_SECONDS
    assert len(issued["grant_id"]) >= 32
    assert issued["access_token"]

    headers = {"Authorization": f"Bearer {issued['access_token']}"}
    current = client.get("/api/v1/security/sessions/current", headers=headers)
    assert current.status_code == 200
    assert current.json()["session_class"] == "general"
    assert current.json()["grant_persistent"] is True
    assert current.json()["auth_level"] == "L1"

    refresh_challenge = client.post(
        f"/api/v1/security/general-grants/{issued['grant_id']}/refresh-challenges",
        json={},
    )
    assert refresh_challenge.status_code == 200, refresh_challenge.text
    refreshed = client.post(
        "/api/v1/security/general-grants/refresh",
        json={
            "challenge_id": refresh_challenge.json()["challenge_id"],
            "signature": _sign(continuity_private, refresh_challenge.json()),
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    renewed = refreshed.json()
    assert renewed["grant_id"] == issued["grant_id"]
    assert renewed["session_class"] == "general"
    assert renewed["grant_persistent"] is True
    assert renewed["expires_in_seconds"] == uosa_security.GENERAL_SESSION_TTL_SECONDS
    assert renewed["access_token"] != issued["access_token"]

    renewed_headers = {"Authorization": f"Bearer {renewed['access_token']}"}
    revoked = client.delete(
        "/api/v1/security/general-grants/current",
        headers=renewed_headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json() == {
        "status": "revoked",
        "session_class": "general",
        "grant_persistent": False,
    }

    assert client.get(
        "/api/v1/security/sessions/current",
        headers=renewed_headers,
    ).status_code == 401
    assert client.post(
        f"/api/v1/security/general-grants/{issued['grant_id']}/refresh-challenges",
        json={},
    ).status_code == 401

    with uosa_security._connect() as connection:
        row = connection.execute(
            "SELECT grant_id_hash, revoked_at FROM uosa_general_grants"
        ).fetchone()
    assert row is not None
    assert row["revoked_at"] is not None
    assert row["grant_id_hash"] != issued["grant_id"]


def test_general_grant_refresh_requires_distinct_continuity_signature_and_is_one_use(
    uosa_db,
):
    owner_private, owner_public = _keypair()
    continuity_private, continuity_public = _keypair()
    _enroll(owner_private, owner_public)
    issued = _issue_general_grant(owner_private, continuity_public)

    challenge = client.post(
        f"/api/v1/security/general-grants/{issued['grant_id']}/refresh-challenges",
        json={},
    )
    assert challenge.status_code == 200

    # The enrolled biometric key cannot substitute for the separate continuity key.
    wrong_key = client.post(
        "/api/v1/security/general-grants/refresh",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(owner_private, challenge.json()),
        },
    )
    assert wrong_key.status_code == 401

    replay = client.post(
        "/api/v1/security/general-grants/refresh",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(continuity_private, challenge.json()),
        },
    )
    assert replay.status_code == 401


def test_general_grant_rejects_cipher_ale_and_cloud_scopes(uosa_db):
    owner_private, owner_public = _keypair()
    _, continuity_public = _keypair()
    _enroll(owner_private, owner_public)

    response = client.post(
        "/api/v1/security/general-grants/challenges",
        json={
            "device_id": "grant-test-device",
            "audience": "amiki-core",
            "scopes": ["chat.use", "cipher.inspect"],
            "continuity_public_key": continuity_public,
        },
    )
    assert response.status_code == 403
    assert "separate privileged session" in response.json()["detail"]


def test_privileged_session_contract_does_not_bypass_l2(uosa_db):
    owner_private, owner_public = _keypair()
    _enroll(owner_private, owner_public)

    challenge = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "grant-test-device",
            "audience": "amiki-core",
            "scopes": [
                "cipher.inspect",
                "ale.review.read",
                "ale.review.approve",
                "cloud.monitor.read",
                "cloud.jobs.create",
            ],
        },
    )
    assert challenge.status_code == 200, challenge.text
    issued = client.post(
        "/api/v1/security/sessions",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(owner_private, challenge.json()),
        },
    )
    assert issued.status_code == 200, issued.text

    session = issued.json()
    assert session["session_class"] == "privileged"
    assert session["expires_in_seconds"] == 900
    assert session["auth_level"] == "L1"
    assert session["scopes"] == sorted(
        [
            "cipher.inspect",
            "ale.review.read",
            "ale.review.approve",
            "cloud.monitor.read",
            "cloud.jobs.create",
        ]
    )

    # The privileged capability session is a 15-minute L1 session. It must not
    # replace the separate transaction-bound L2 biometric authorization.
    denied = client.post(
        "/api/v1/learning/review-queue/record-123/approve",
        headers={"Authorization": f"Bearer {session['access_token']}"},
    )
    assert denied.status_code == 403
    assert "requires L2" in denied.json()["detail"]


def test_mixed_general_and_privileged_scopes_are_not_classified_as_privileged(uosa_db):
    owner_private, owner_public = _keypair()
    _enroll(owner_private, owner_public)

    challenge = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "grant-test-device",
            "audience": "amiki-core",
            "scopes": ["cipher.inspect", "chat.use"],
        },
    )
    assert challenge.status_code == 200
    issued = client.post(
        "/api/v1/security/sessions",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(owner_private, challenge.json()),
        },
    )
    assert issued.status_code == 200
    assert issued.json()["session_class"] == "standard"


def test_general_grant_is_invalidated_when_device_generation_changes(uosa_db):
    owner_private, owner_public = _keypair()
    _, continuity_public = _keypair()
    _enroll(owner_private, owner_public)
    issued = _issue_general_grant(owner_private, continuity_public)

    with uosa_security._connect() as connection:
        uosa_security._initialize(connection)
        connection.execute(
            "UPDATE uosa_devices SET enrollment_generation = enrollment_generation + 1 "
            "WHERE device_id = ?",
            ("grant-test-device",),
        )

    assert uosa_security.get_session(issued["access_token"]) is None
    refresh = client.post(
        f"/api/v1/security/general-grants/{issued['grant_id']}/refresh-challenges",
        json={},
    )
    assert refresh.status_code == 401
