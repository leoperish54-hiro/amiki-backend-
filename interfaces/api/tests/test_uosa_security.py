import base64
import hashlib
import json
import os

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from core import uosa_security
from interfaces.api.server.app import app


client = TestClient(app)
LEGACY_OWNER_TOKEN = "retired-owner-token-must-never-authorize"


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


def _enroll(private_key, public_key, device_id="test-device-1"):
    setup_code = uosa_security.create_setup_code()["setup_code"]
    challenge = client.post(
        "/api/v1/security/setup/enrollment-challenges",
        json={
            "setup_code": setup_code,
            "device_id": device_id,
            "label": "pytest device",
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


def test_initial_setup_rejects_missing_or_invalid_one_time_code(uosa_db):
    response = client.post(
        "/api/v1/security/setup/enrollment-challenges",
        json={
            "setup_code": "invalid-setup-code-that-is-not-valid",
            "device_id": "d1",
            "label": "phone",
            "public_key": _keypair()[1],
        },
    )
    assert response.status_code == 401


def test_enroll_and_issue_short_lived_scoped_session(uosa_db):
    private_key, public_key = _keypair()
    _enroll(private_key, public_key)

    challenge = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "test-device-1",
            "audience": "amiki-core",
            "scopes": ["ale.review.read", "ale.review.approve"],
        },
    )
    assert challenge.status_code == 200

    session = client.post(
        "/api/v1/security/sessions",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(private_key, challenge.json()),
        },
    )
    assert session.status_code == 200
    payload = session.json()
    assert payload["auth_level"] == "L1"
    assert payload["expires_in_seconds"] == uosa_security.SESSION_TTL_SECONDS
    assert set(payload["scopes"]) == {"ale.review.read", "ale.review.approve"}
    assert "access_token" in payload

    current = client.get(
        "/api/v1/security/sessions/current",
        headers={"Authorization": f"Bearer {payload['access_token']}"},
    )
    assert current.status_code == 200
    assert current.json()["status"] == "active"


def test_challenge_is_single_use_and_wrong_signature_is_rejected(uosa_db):
    private_key, public_key = _keypair()
    _enroll(private_key, public_key)
    challenge = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "test-device-1",
            "audience": "amiki-core",
            "scopes": ["ale.review.read"],
        },
    ).json()
    wrong_key, _ = _keypair()
    bad = client.post(
        "/api/v1/security/sessions",
        json={
            "challenge_id": challenge["challenge_id"],
            "signature": _sign(wrong_key, challenge),
        },
    )
    assert bad.status_code == 401
    replay = client.post(
        "/api/v1/security/sessions",
        json={
            "challenge_id": challenge["challenge_id"],
            "signature": _sign(private_key, challenge),
        },
    )
    assert replay.status_code == 401


def test_scope_audience_and_unknown_device_are_denied(uosa_db):
    private_key, public_key = _keypair()
    _enroll(private_key, public_key)
    unknown_scope = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "test-device-1",
            "audience": "amiki-core",
            "scopes": ["cipher.x.activate"],
        },
    )
    assert unknown_scope.status_code == 403
    unknown_device = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "unknown",
            "audience": "amiki-core",
            "scopes": ["ale.review.read"],
        },
    )
    assert unknown_device.status_code == 401


def test_session_and_device_revocation(uosa_db):
    private_key, public_key = _keypair()
    _enroll(private_key, public_key)
    challenge = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "test-device-1",
            "audience": "amiki-core",
            "scopes": ["ale.review.read"],
        },
    ).json()
    session = client.post(
        "/api/v1/security/sessions",
        json={
            "challenge_id": challenge["challenge_id"],
            "signature": _sign(private_key, challenge),
        },
    ).json()
    token = session["access_token"]

    logout = client.delete(
        "/api/v1/security/sessions/current",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert logout.status_code == 200
    assert client.get(
        "/api/v1/security/sessions/current",
        headers={"Authorization": f"Bearer {token}"},
    ).status_code == 401

    revoke_headers = _l2_headers(
        private_key,
        "test-device-1",
        "uosa.device.revoke",
        "POST",
        "/api/v1/security/devices/test-device-1/revoke",
    )
    assert client.post(
        "/api/v1/security/devices/test-device-1/revoke",
        headers=revoke_headers,
    ).status_code == 200
    assert client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "test-device-1",
            "audience": "amiki-core",
            "scopes": ["ale.review.read"],
        },
    ).status_code == 401


def test_enrollment_rejects_non_p256_keys(uosa_db):
    private_key = ec.generate_private_key(ec.SECP384R1())
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    setup_code = uosa_security.create_setup_code()["setup_code"]
    response = client.post(
        "/api/v1/security/setup/enrollment-challenges",
        json={
            "setup_code": setup_code,
            "device_id": "p384-device",
            "label": "wrong curve",
            "public_key": base64.b64encode(public_der).decode("ascii"),
        },
    )
    assert response.status_code == 422


def test_openapi_exposes_uosa_only_and_no_legacy_bootstrap_scheme(uosa_db):
    schema = app.openapi()
    schemes = schema["components"]["securitySchemes"]
    assert "UosaBootstrapBearer" not in schemes
    assert "UosaSessionBearer" in schemes

    paths = schema["paths"]
    assert "security" not in paths["/api/v1/security/setup/enrollment-challenges"]["post"]
    assert "security" not in paths["/api/v1/security/setup/enroll"]["post"]
    assert paths["/api/v1/security/devices/enrollment-challenges"]["post"][
        "security"
    ] == [{"UosaSessionBearer": []}]
    assert paths["/api/v1/security/devices/enroll"]["post"][
        "security"
    ] == [{"UosaSessionBearer": []}]
    assert paths["/api/v1/security/sessions/current"]["get"]["security"] == [
        {"UosaSessionBearer": []}
    ]
    assert "security" not in paths["/api/v1/security/challenges"]["post"]


def test_scope_denial_is_forbidden_and_invalid_key_is_unprocessable(uosa_db):
    private_key, public_key = _keypair()
    _enroll(private_key, public_key)

    denied = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "test-device-1",
            "audience": "amiki-core",
            "scopes": ["cipher.x.activate"],
        },
    )
    assert denied.status_code == 403

    base = _issue_session(
        private_key, "test-device-1", ["uosa.device.enroll"]
    )
    invalid_key = client.post(
        "/api/v1/security/devices/enrollment-challenges",
        headers={"Authorization": f"Bearer {base['access_token']}"},
        json={
            "device_id": "invalid-key-device",
            "label": "invalid key",
            "public_key": "not-base64",
        },
    )
    assert invalid_key.status_code == 422


def test_device_revocation_invalidates_pending_session_challenges(uosa_db):
    private_key, public_key = _keypair()
    enrolled = _enroll(private_key, public_key)
    recovery_code = enrolled["recovery_codes"][0]
    revoke_headers = _l2_headers(
        private_key,
        "test-device-1",
        "uosa.device.revoke",
        "POST",
        "/api/v1/security/devices/test-device-1/revoke",
    )

    challenge = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "test-device-1",
            "audience": "amiki-core",
            "scopes": ["ale.review.read"],
        },
    ).json()

    revoked = client.post(
        "/api/v1/security/devices/test-device-1/revoke",
        headers=revoke_headers,
    )
    assert revoked.status_code == 200

    completion = client.post(
        "/api/v1/security/sessions",
        json={
            "challenge_id": challenge["challenge_id"],
            "signature": _sign(private_key, challenge),
        },
    )
    assert completion.status_code == 401

def test_inflight_session_challenge_is_rejected_after_revoke_and_reenroll(
    uosa_db, monkeypatch
):
    private_key, public_key = _keypair()
    enrolled = _enroll(private_key, public_key)
    recovery_code = enrolled["recovery_codes"][0]
    revoke_headers = _l2_headers(
        private_key,
        "test-device-1",
        "uosa.device.revoke",
        "POST",
        "/api/v1/security/devices/test-device-1/revoke",
    )

    challenge = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "test-device-1",
            "audience": "amiki-core",
            "scopes": ["ale.review.read"],
        },
    ).json()

    original_verify = uosa_security._verify_signature
    transition_started = False

    def revoke_and_reenroll_during_verification(public_key_der, message, signature_b64):
        nonlocal transition_started
        if not transition_started:
            transition_started = True
            revoked = client.post(
                "/api/v1/security/devices/test-device-1/revoke",
                headers=revoke_headers,
            )
            assert revoked.status_code == 200
            _recover_owner_device(recovery_code, "test-device-1")
        return original_verify(public_key_der, message, signature_b64)

    monkeypatch.setattr(
        uosa_security, "_verify_signature", revoke_and_reenroll_during_verification
    )

    completion = client.post(
        "/api/v1/security/sessions",
        json={
            "challenge_id": challenge["challenge_id"],
            "signature": _sign(private_key, challenge),
        },
    )

    assert transition_started is True
    assert completion.status_code == 401
    assert "re-enrolled" in completion.json()["detail"]


def _issue_session(private_key, device_id, scopes):
    challenge = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": device_id,
            "audience": "amiki-core",
            "scopes": scopes,
        },
    )
    assert challenge.status_code == 200
    issued = client.post(
        "/api/v1/security/sessions",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(private_key, challenge.json()),
        },
    )
    assert issued.status_code == 200
    return issued.json()




def _l2_headers(private_key, device_id, scope, method, path, body=b""):
    base = _issue_session(private_key, device_id, [scope])
    binding = uosa_security.compute_transaction_binding(method, path, body)
    challenge = client.post(
        "/api/v1/security/elevation-challenges",
        headers={"Authorization": f"Bearer {base['access_token']}"},
        json={
            "auth_level": "L2",
            "scope": scope,
            "transaction_hash": binding,
        },
    )
    assert challenge.status_code == 200, challenge.text
    elevated = client.post(
        "/api/v1/security/elevated-sessions",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(private_key, challenge.json()),
        },
    )
    assert elevated.status_code == 200, elevated.text
    return {"Authorization": f"Bearer {elevated.json()['access_token']}"}



def _add_device(owner_private_key, owner_device_id, new_device_id, label="additional test device"):
    new_private, new_public = _keypair()
    base = _issue_session(
        owner_private_key, owner_device_id, ["uosa.device.enroll"]
    )
    challenge = client.post(
        "/api/v1/security/devices/enrollment-challenges",
        headers={"Authorization": f"Bearer {base['access_token']}"},
        json={
            "device_id": new_device_id,
            "label": label,
            "public_key": new_public,
        },
    )
    assert challenge.status_code == 200, challenge.text
    payload = {
        "challenge_id": challenge.json()["challenge_id"],
        "signature": _sign(new_private, challenge.json()),
    }
    body = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    endpoint = "/api/v1/security/devices/enroll"
    elevated_headers = _l2_headers(
        owner_private_key,
        owner_device_id,
        "uosa.device.enroll",
        "POST",
        endpoint,
        body,
    )
    enrolled = client.post(endpoint, headers=elevated_headers, json=payload)
    assert enrolled.status_code == 200, enrolled.text
    return new_private, new_public, enrolled.json()


def _recover_owner_device(recovery_code, device_id):
    new_private, new_public = _keypair()
    challenge = client.post(
        "/api/v1/security/recovery/challenges",
        json={
            "recovery_code": recovery_code,
            "device_id": device_id,
            "label": "recovered owner device",
            "public_key": new_public,
        },
    )
    assert challenge.status_code == 200, challenge.text
    recovered = client.post(
        "/api/v1/security/recovery/complete",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(new_private, challenge.json()),
        },
    )
    assert recovered.status_code == 200, recovered.text
    return new_private, new_public, recovered.json()


def _create_general_grant(owner_private, device_id, scopes, continuity_private=None):
    if continuity_private is None:
        continuity_private, continuity_public = _keypair()
    else:
        public_der = continuity_private.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        continuity_public = base64.b64encode(public_der).decode("ascii")
    challenge = client.post(
        "/api/v1/security/general-grants/challenges",
        json={
            "device_id": device_id,
            "audience": "amiki-core",
            "scopes": scopes,
            "continuity_public_key": continuity_public,
        },
    )
    assert challenge.status_code == 200, challenge.text
    completed = client.post(
        "/api/v1/security/general-grants",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(owner_private, challenge.json()),
        },
    )
    assert completed.status_code == 200, completed.text
    return continuity_private, completed.json()





def test_legacy_mixed_scope_sessions_fail_closed_after_session_class_rollout(uosa_db):
    owner_private, owner_public = _keypair()
    _enroll(owner_private, owner_public)
    legacy_token = "legacy-mixed-session-token-for-rollout-test"
    token_hash = hashlib.sha256(legacy_token.encode("utf-8")).hexdigest()
    now = uosa_security._now()
    with uosa_security._connection() as connection:
        uosa_security._initialize(connection)
        connection.execute(
            "INSERT INTO uosa_sessions "
            "(token_hash, device_id, audience, scopes_json, auth_level, created_at, "
            "expires_at, device_generation) VALUES (?, ?, ?, ?, 'L1', ?, ?, 1)",
            (
                token_hash,
                "test-device-1",
                uosa_security.OWNER_AUDIENCE,
                json.dumps(["chat.use", "ale.review.read"]),
                now,
                now + 900,
            ),
        )
    assert uosa_security.get_session(legacy_token) is None


def test_session_scope_classes_reject_mixed_capability_domains(uosa_db):
    owner_private, owner_public = _keypair()
    _enroll(owner_private, owner_public)
    mixed = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "test-device-1",
            "audience": "amiki-core",
            "scopes": ["chat.use", "ale.review.read"],
        },
    )
    assert mixed.status_code == 403
    assert "separate sessions" in mixed.json()["detail"]
    general = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "test-device-1",
            "audience": "amiki-core",
            "scopes": ["chat.use", "agents.run", "device.read"],
        },
    )
    assert general.status_code == 200
    privileged = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "test-device-1",
            "audience": "amiki-core",
            "scopes": ["cipher.inspect", "ale.review.read", "cloud.jobs.read"],
        },
    )
    assert privileged.status_code == 200


def test_general_access_sessions_are_short_and_privileged_sessions_remain_15_minutes(uosa_db):
    owner_private, owner_public = _keypair()
    _enroll(owner_private, owner_public)
    general_challenge = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "test-device-1",
            "audience": "amiki-core",
            "scopes": ["chat.use", "agents.run"],
        },
    ).json()
    general = client.post(
        "/api/v1/security/sessions",
        json={
            "challenge_id": general_challenge["challenge_id"],
            "signature": _sign(owner_private, general_challenge),
        },
    )
    assert general.status_code == 200
    assert general.json()["expires_in_seconds"] == uosa_security.GENERAL_ACCESS_TTL_SECONDS
    assert general.json()["session_class"] == "general"

    privileged_challenge = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "test-device-1",
            "audience": "amiki-core",
            "scopes": ["ale.review.read", "cloud.jobs.read", "cipher.inspect"],
        },
    ).json()
    privileged = client.post(
        "/api/v1/security/sessions",
        json={
            "challenge_id": privileged_challenge["challenge_id"],
            "signature": _sign(owner_private, privileged_challenge),
        },
    )
    assert privileged.status_code == 200
    assert privileged.json()["expires_in_seconds"] == 900
    assert privileged.json()["session_class"] == "privileged"


def test_general_grant_refresh_uses_continuity_key_and_can_be_revoked(uosa_db):
    owner_private, owner_public = _keypair()
    _enroll(owner_private, owner_public)
    continuity_private, grant = _create_general_grant(
        owner_private, "test-device-1", ["chat.use", "agents.run", "device.read"]
    )
    assert grant["session_class"] == "general"
    assert grant["grant_persistent"] is True
    assert grant["expires_in_seconds"] == uosa_security.GENERAL_ACCESS_TTL_SECONDS
    assert "ale.review.read" not in grant["scopes"]
    assert "cloud.jobs.read" not in grant["scopes"]

    # Prove the original bearer expires while the persistent grant remains renewable.
    with uosa_security._connect() as connection:
        connection.execute(
            "UPDATE uosa_sessions SET expires_at = ? WHERE grant_id = ?",
            (uosa_security._now() - 1, grant["grant_id"]),
        )
    assert uosa_security.get_session(grant["access_token"]) is None

    refresh = client.post(
        f"/api/v1/security/general-grants/{grant['grant_id']}/refresh-challenges"
    )
    assert refresh.status_code == 200
    refreshed = client.post(
        "/api/v1/security/general-grants/refresh",
        json={
            "challenge_id": refresh.json()["challenge_id"],
            "signature": _sign(continuity_private, refresh.json()),
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    refreshed_payload = refreshed.json()
    assert refreshed_payload["grant_id"] == grant["grant_id"]
    assert refreshed_payload["expires_in_seconds"] == uosa_security.GENERAL_ACCESS_TTL_SECONDS
    assert uosa_security.get_session(refreshed_payload["access_token"]) is not None

    replay = client.post(
        "/api/v1/security/general-grants/refresh",
        json={
            "challenge_id": refresh.json()["challenge_id"],
            "signature": _sign(continuity_private, refresh.json()),
        },
    )
    assert replay.status_code == 401

    revoked = client.delete(
        "/api/v1/security/general-grants/current",
        headers={"Authorization": f"Bearer {refreshed_payload['access_token']}"},
    )
    assert revoked.status_code == 200
    assert uosa_security.get_session(refreshed_payload["access_token"]) is None
    after_revoke = client.post(
        f"/api/v1/security/general-grants/{grant['grant_id']}/refresh-challenges"
    )
    assert after_revoke.status_code == 401


def test_general_grants_cannot_contain_privileged_scopes_and_agents_keep_l2(uosa_db):
    owner_private, owner_public = _keypair()
    _enroll(owner_private, owner_public)
    continuity_private, continuity_public = _keypair()
    denied = client.post(
        "/api/v1/security/general-grants/challenges",
        json={
            "device_id": "test-device-1",
            "audience": "amiki-core",
            "scopes": ["chat.use", "ale.review.read"],
            "continuity_public_key": continuity_public,
        },
    )
    assert denied.status_code == 403

    _, grant = _create_general_grant(
        owner_private, "test-device-1", ["chat.use", "agents.run"]
    )
    transaction_hash = "a" * 64
    elevation = client.post(
        "/api/v1/security/elevation-challenges",
        headers={"Authorization": f"Bearer {grant['access_token']}"},
        json={
            "auth_level": "L2",
            "scope": "agents.run",
            "transaction_hash": transaction_hash,
        },
    )
    assert elevation.status_code == 200
    assert elevation.json()["expires_in_seconds"] == uosa_security.CHALLENGE_TTL_SECONDS



def test_transaction_binding_uses_canonical_json_and_newline_separators():
    method = "POST"
    path = "/api/v1/device/ui-actions/authorize"
    body = b'{"task_id":"t-1","action_type":"tap"}'
    canonical = '{"action_type":"tap","task_id":"t-1"}'
    expected = hashlib.sha256(
        f"{method}\n{path}\n{canonical}".encode("utf-8")
    ).hexdigest()
    assert uosa_security.compute_transaction_binding(method, path, body) == expected


def test_l2_elevation_is_transaction_bound_and_single_use(uosa_db):
    private_key, public_key = _keypair()
    _enroll(private_key, public_key)
    base = _issue_session(private_key, "test-device-1", ["ale.review.approve"])
    path = "/api/v1/learning/review-queue/record-123/approve"
    binding = uosa_security.compute_transaction_binding("POST", path, b"")

    challenge = client.post(
        "/api/v1/security/elevation-challenges",
        headers={"Authorization": f"Bearer {base['access_token']}"},
        json={
            "auth_level": "L2",
            "scope": "ale.review.approve",
            "transaction_hash": binding,
        },
    )
    assert challenge.status_code == 200
    elevated = client.post(
        "/api/v1/security/elevated-sessions",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(private_key, challenge.json()),
        },
    )
    assert elevated.status_code == 200
    data = elevated.json()
    assert data["auth_level"] == "L2"
    assert data["single_use"] is True
    assert data["transaction_hash"] == binding
    assert data["expires_in_seconds"] == uosa_security.ELEVATED_SESSION_TTL_SECONDS

    token = data["access_token"]
    assert not uosa_security.consume_elevated_session(
        token, "ale.review.approve", "0" * 64
    )
    # A mismatched first attempt burns the elevated grant permanently.
    assert not uosa_security.consume_elevated_session(
        token, "ale.review.approve", binding
    )
    assert uosa_security.get_session(token) is None


def test_elevation_requires_pre_authorized_scope_and_rejects_l3(uosa_db):
    private_key, public_key = _keypair()
    _enroll(private_key, public_key)
    base = _issue_session(private_key, "test-device-1", ["ale.review.read"])
    binding = "a" * 64

    denied = client.post(
        "/api/v1/security/elevation-challenges",
        headers={"Authorization": f"Bearer {base['access_token']}"},
        json={
            "auth_level": "L2",
            "scope": "ale.review.approve",
            "transaction_hash": binding,
        },
    )
    assert denied.status_code == 403

    l3 = client.post(
        "/api/v1/security/elevation-challenges",
        headers={"Authorization": f"Bearer {base['access_token']}"},
        json={
            "auth_level": "L3",
            "scope": "cipherx.emergency.isolate",
            "transaction_hash": binding,
        },
    )
    assert l3.status_code == 422


def test_ale_owner_routes_reject_legacy_token_and_require_l2(uosa_db):
    private_key, public_key = _keypair()
    _enroll(private_key, public_key)
    base = _issue_session(private_key, "test-device-1", ["ale.review.approve"])
    url = "/api/v1/learning/review-queue/record-123/approve"

    legacy = client.post(
        url,
        headers={"Authorization": f"Bearer {LEGACY_OWNER_TOKEN}"},
    )
    assert legacy.status_code == 401

    l1 = client.post(
        url,
        headers={"Authorization": f"Bearer {base['access_token']}"},
    )
    assert l1.status_code == 403
    assert "requires L2" in l1.json()["detail"]


def test_elevated_route_rejects_mismatched_transaction_and_consumes_match(uosa_db):
    private_key, public_key = _keypair()
    _enroll(private_key, public_key)
    base = _issue_session(private_key, "test-device-1", ["ale.review.approve"])
    path = "/api/v1/learning/review-queue/record-123/approve"
    binding = uosa_security.compute_transaction_binding("POST", path, b"")
    challenge = client.post(
        "/api/v1/security/elevation-challenges",
        headers={"Authorization": f"Bearer {base['access_token']}"},
        json={
            "auth_level": "L2",
            "scope": "ale.review.approve",
            "transaction_hash": binding,
        },
    )
    assert challenge.status_code == 200
    elevated = client.post(
        "/api/v1/security/elevated-sessions",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(private_key, challenge.json()),
        },
    )
    assert elevated.status_code == 200
    headers = {"Authorization": f"Bearer {elevated.json()['access_token']}"}

    mismatch = client.post(
        "/api/v1/learning/review-queue/record-456/approve",
        headers=headers,
    )
    assert mismatch.status_code == 403

    # A failed transaction substitution consumes the authorization; even the
    # originally intended request must obtain a new biometric elevation.
    matched = client.post(path, headers=headers)
    assert matched.status_code == 401
    replay = client.post(path, headers=headers)
    assert replay.status_code == 401


def test_issued_session_is_invalid_after_device_generation_changes(uosa_db):
    private_key, public_key = _keypair()
    _enroll(private_key, public_key)
    session = _issue_session(
        private_key,
        "test-device-1",
        ["ale.review.read"],
    )
    assert uosa_security.get_session(session["access_token"]) is not None

    with uosa_security._connect() as connection:
        uosa_security._initialize(connection)
        connection.execute(
            "UPDATE uosa_devices SET enrollment_generation = enrollment_generation + 1 "
            "WHERE device_id = ?",
            ("test-device-1",),
        )

    assert uosa_security.get_session(session["access_token"]) is None


def test_session_schema_migrates_device_generation_for_existing_rows(
    tmp_path, monkeypatch
):
    import sqlite3

    path = tmp_path / "legacy-uosa.sqlite3"
    monkeypatch.setenv("AMIKI_UOSA_DB_PATH", str(path))
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE uosa_devices (
            device_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            public_key_der BLOB NOT NULL,
            key_fingerprint TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL,
            revoked_at INTEGER
        );
        CREATE TABLE uosa_challenges (
            challenge_id TEXT PRIMARY KEY,
            purpose TEXT NOT NULL,
            device_id TEXT NOT NULL,
            nonce TEXT NOT NULL,
            message TEXT NOT NULL,
            context_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            consumed_at INTEGER
        );
        CREATE TABLE uosa_sessions (
            token_hash TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            audience TEXT NOT NULL,
            scopes_json TEXT NOT NULL,
            auth_level TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked_at INTEGER
        );
        CREATE TABLE uosa_security_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            device_id TEXT,
            created_at INTEGER NOT NULL,
            details_json TEXT NOT NULL
        );
        INSERT INTO uosa_devices VALUES (
            'legacy-device', 'legacy', X'0102', 'fingerprint', 10, NULL
        );
        INSERT INTO uosa_sessions VALUES (
            'token-hash', 'legacy-device', 'amiki-core',
            '["ale.review.read"]', 'L1', 10, 9999999999, NULL
        );
        """
    )
    connection.commit()
    connection.close()

    with uosa_security._connect() as migrated:
        uosa_security._initialize(migrated)
        device_columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(uosa_devices)")
        }
        session_columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(uosa_sessions)")
        }
        session_generation = migrated.execute(
            "SELECT device_generation FROM uosa_sessions WHERE token_hash = 'token-hash'"
        ).fetchone()[0]
    assert "enrollment_generation" in device_columns
    assert "device_generation" in session_columns
    assert session_generation == 1


def test_initial_enrollment_issues_hashed_one_time_recovery_codes(uosa_db):
    private_key, public_key = _keypair()
    enrolled = _enroll(private_key, public_key)
    codes = enrolled["recovery_codes"]

    assert len(codes) == uosa_security.RECOVERY_CODE_COUNT
    assert len(set(codes)) == len(codes)
    assert enrolled["recovery_generation"] == 1

    with uosa_security._connect() as connection:
        rows = connection.execute(
            "SELECT code_hash FROM uosa_recovery_codes"
        ).fetchall()
    stored_hashes = {row["code_hash"] for row in rows}
    assert len(stored_hashes) == len(codes)
    assert all(uosa_security._recovery_code_hash(code) in stored_hashes for code in codes)
    assert all(code not in stored_hashes for code in codes)


def test_recovery_rotates_codes_and_revokes_all_previous_devices_and_sessions(uosa_db):
    old_private, old_public = _keypair()
    first_enrollment = _enroll(old_private, old_public, "old-device")
    recovery_code = first_enrollment["recovery_codes"][0]
    continuity_private, persistent_general = _create_general_grant(
        old_private, "old-device", ["chat.use", "agents.run"]
    )
    old_session = _issue_session(old_private, "old-device", ["ale.review.read"])

    second_private, second_public, _ = _add_device(
        old_private, "old-device", "second-old-device"
    )
    second_session = _issue_session(
        second_private, "second-old-device", ["ale.review.read"]
    )

    new_private, new_public = _keypair()
    challenge = client.post(
        "/api/v1/security/recovery/challenges",
        json={
            "recovery_code": recovery_code,
            "device_id": "replacement-device",
            "label": "replacement phone",
            "public_key": new_public,
        },
    )
    assert challenge.status_code == 200
    recovered = client.post(
        "/api/v1/security/recovery/complete",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(new_private, challenge.json()),
        },
    )
    assert recovered.status_code == 200
    payload = recovered.json()
    assert payload["status"] == "recovered"
    assert payload["revoked_previous_devices"] is True
    assert payload["recovery_generation"] == 2
    assert len(payload["recovery_codes"]) == uosa_security.RECOVERY_CODE_COUNT

    assert uosa_security.get_session(old_session["access_token"]) is None
    assert uosa_security.get_session(second_session["access_token"]) is None
    assert uosa_security.get_session(persistent_general["access_token"]) is None
    revoked_grant_refresh = client.post(
        f"/api/v1/security/general-grants/{persistent_general['grant_id']}/refresh-challenges"
    )
    assert revoked_grant_refresh.status_code == 401
    devices = uosa_security.list_devices()
    active = [item for item in devices if item["active"]]
    assert [item["device_id"] for item in active] == ["replacement-device"]
    assert active[0]["enrollment_generation"] == 1

    reused = client.post(
        "/api/v1/security/recovery/challenges",
        json={
            "recovery_code": recovery_code,
            "device_id": "attacker-device",
            "label": "attempted reuse",
            "public_key": _keypair()[1],
        },
    )
    assert reused.status_code == 401

    new_session = _issue_session(
        new_private, "replacement-device", ["ale.review.read"]
    )
    assert uosa_security.get_session(new_session["access_token"]) is not None


def test_recovery_requires_new_key_proof_and_consumes_failed_challenge(uosa_db):
    old_private, old_public = _keypair()
    enrolled = _enroll(old_private, old_public)
    new_private, new_public = _keypair()
    challenge = client.post(
        "/api/v1/security/recovery/challenges",
        json={
            "recovery_code": enrolled["recovery_codes"][0],
            "device_id": "replacement-device",
            "label": "new device",
            "public_key": new_public,
        },
    )
    assert challenge.status_code == 200

    failed = client.post(
        "/api/v1/security/recovery/complete",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(old_private, challenge.json()),
        },
    )
    assert failed.status_code == 401
    replay = client.post(
        "/api/v1/security/recovery/complete",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(new_private, challenge.json()),
        },
    )
    assert replay.status_code == 401
    assert uosa_security.list_devices()[0]["active"] is True


def test_recovery_attempts_are_persistently_rate_limited(uosa_db):
    _, public_key = _keypair()
    for attempt in range(uosa_security.RECOVERY_MAX_ATTEMPTS):
        response = client.post(
            "/api/v1/security/recovery/challenges",
            json={
                "recovery_code": "x" * 43,
                "device_id": f"invalid-recovery-{attempt}",
                "label": "bad code",
                "public_key": public_key,
            },
        )
        assert response.status_code == 401
    locked = client.post(
        "/api/v1/security/recovery/challenges",
        json={
            "recovery_code": "x" * 43,
            "device_id": "locked-device",
            "label": "rate limited",
            "public_key": public_key,
        },
    )
    assert locked.status_code == 429
    with uosa_security._connect() as connection:
        row = connection.execute(
            "SELECT attempts, blocked_until FROM uosa_recovery_attempts"
        ).fetchone()
    assert row["attempts"] == uosa_security.RECOVERY_MAX_ATTEMPTS + 1
    assert row["blocked_until"] is not None


def test_key_rotation_requires_old_and_new_key_proofs_and_revokes_sessions(uosa_db):
    old_private, old_public = _keypair()
    _enroll(old_private, old_public)
    old_session = _issue_session(
        old_private, "test-device-1", ["uosa.device.rotate"]
    )
    new_private, new_public = _keypair()

    challenge = client.post(
        "/api/v1/security/devices/test-device-1/rotation-challenges",
        headers={"Authorization": f"Bearer {old_session['access_token']}"},
        json={"replacement_public_key": new_public},
    )
    assert challenge.status_code == 200
    assert new_public not in challenge.text
    assert challenge.json()["message"].find("replacement_key_fingerprint") >= 0

    payload = {
        "challenge_id": challenge.json()["challenge_id"],
        "old_signature": _sign(old_private, challenge.json()),
        "new_signature": _sign(new_private, challenge.json()),
    }
    body = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    endpoint = "/api/v1/security/devices/test-device-1/rotate"
    elevated_headers = _l2_headers(
        old_private,
        "test-device-1",
        "uosa.device.rotate",
        "POST",
        endpoint,
        body,
    )
    rotated = client.post(endpoint, headers=elevated_headers, json=payload)
    assert rotated.status_code == 200
    assert rotated.json()["status"] == "rotated"
    assert rotated.json()["enrollment_generation"] == 2
    assert rotated.json()["previous_sessions_revoked"] is True
    assert uosa_security.get_session(old_session["access_token"]) is None

    new_session = _issue_session(
        new_private, "test-device-1", ["ale.review.read"]
    )
    assert uosa_security.get_session(new_session["access_token"]) is not None


def test_key_rotation_rejects_wrong_replacement_key_signature(uosa_db):
    old_private, old_public = _keypair()
    _enroll(old_private, old_public)
    old_session = _issue_session(
        old_private, "test-device-1", ["uosa.device.rotate"]
    )
    new_private, new_public = _keypair()
    wrong_private, _ = _keypair()
    challenge = client.post(
        "/api/v1/security/devices/test-device-1/rotation-challenges",
        headers={"Authorization": f"Bearer {old_session['access_token']}"},
        json={"replacement_public_key": new_public},
    )
    assert challenge.status_code == 200
    payload = {
        "challenge_id": challenge.json()["challenge_id"],
        "old_signature": _sign(old_private, challenge.json()),
        "new_signature": _sign(wrong_private, challenge.json()),
    }
    body = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    endpoint = "/api/v1/security/devices/test-device-1/rotate"
    elevated_headers = _l2_headers(
        old_private,
        "test-device-1",
        "uosa.device.rotate",
        "POST",
        endpoint,
        body,
    )
    denied = client.post(endpoint, headers=elevated_headers, json=payload)
    assert denied.status_code == 401
    assert uosa_security.list_devices()[0]["enrollment_generation"] == 1
    assert uosa_security.get_session(old_session["access_token"]) is not None


def test_device_inventory_requires_read_scope_and_hides_key_material(uosa_db):
    private_key, public_key = _keypair()
    _enroll(private_key, public_key)
    session = _issue_session(
        private_key, "test-device-1", ["uosa.device.read"]
    )
    response = client.get(
        "/api/v1/security/devices",
        headers={"Authorization": f"Bearer {session['access_token']}"},
    )
    assert response.status_code == 200
    device = response.json()["items"][0]
    assert device["device_id"] == "test-device-1"
    assert device["active"] is True
    assert "public_key_der" not in device
    assert "public_key" not in device


def test_initial_setup_is_permanently_disabled_after_first_enrollment(uosa_db):
    private_key, public_key = _keypair()
    _enroll(private_key, public_key)
    assert uosa_security.revoke_device("test-device-1") is True

    with pytest.raises(uosa_security.UosaConflictError, match="recovery ceremony"):
        uosa_security.create_setup_code()


def test_additional_device_enrollment_requires_l1_preflight_and_l2_completion(uosa_db):
    owner_private, owner_public = _keypair()
    _enroll(owner_private, owner_public)
    new_private, new_public, enrolled = _add_device(
        owner_private, "test-device-1", "additional-device"
    )
    assert enrolled["status"] == "enrolled"
    assert enrolled["auth_level"] == "L0"
    assert enrolled["requires_session_issuance"] is True

    issued = _issue_session(new_private, "additional-device", ["ale.review.read"])
    assert uosa_security.get_session(issued["access_token"]) is not None

    old = client.post(
        "/api/v1/security/devices/enrollment-challenges",
        json={
            "device_id": "unauthorized-device",
            "label": "without current authority",
            "public_key": _keypair()[1],
        },
    )
    assert old.status_code == 401


def test_recovery_codes_can_be_rotated_only_with_transaction_bound_l2(uosa_db):
    private_key, public_key = _keypair()
    enrolled = _enroll(private_key, public_key)
    old_code = enrolled["recovery_codes"][0]
    endpoint = "/api/v1/security/recovery/codes/rotate"
    elevated = _l2_headers(
        private_key,
        "test-device-1",
        "uosa.recovery.manage",
        "POST",
        endpoint,
    )
    rotated = client.post(endpoint, headers=elevated)
    assert rotated.status_code == 200
    payload = rotated.json()
    assert payload["status"] == "rotated"
    assert payload["recovery_generation"] == 2
    assert payload["previous_codes_revoked"] is True
    assert len(payload["recovery_codes"]) == uosa_security.RECOVERY_CODE_COUNT

    replacement_key = _keypair()[1]
    invalid_old = client.post(
        "/api/v1/security/recovery/challenges",
        json={
            "recovery_code": old_code,
            "device_id": "lost-device",
            "label": "old code reuse",
            "public_key": replacement_key,
        },
    )
    assert invalid_old.status_code == 401

    valid_new = client.post(
        "/api/v1/security/recovery/challenges",
        json={
            "recovery_code": payload["recovery_codes"][0],
            "device_id": "new-device",
            "label": "new device",
            "public_key": replacement_key,
        },
    )
    assert valid_new.status_code == 200


def test_security_audit_events_are_scoped_metadata_and_bounded(uosa_db):
    private_key, public_key = _keypair()
    enrolled = _enroll(private_key, public_key)
    session = _issue_session(
        private_key, "test-device-1", ["uosa.security.audit.read"]
    )
    response = client.get(
        "/api/v1/security/audit-events?limit=50",
        headers={"Authorization": f"Bearer {session['access_token']}"},
    )
    assert response.status_code == 200
    events = response.json()["items"]
    assert events
    assert any(event["event_type"] == "device.initial_owner_enrolled" for event in events)
    serialized = json.dumps(events)
    assert session["access_token"] not in serialized
    assert enrolled["recovery_codes"][0] not in serialized
    assert public_key not in serialized
    with pytest.raises(uosa_security.UosaValidationError):
        uosa_security.list_security_events(limit=201)


def test_security_audit_events_require_dedicated_scope(uosa_db):
    private_key, public_key = _keypair()
    _enroll(private_key, public_key)
    session = _issue_session(private_key, "test-device-1", ["ale.review.read"])
    denied = client.get(
        "/api/v1/security/audit-events",
        headers={"Authorization": f"Bearer {session['access_token']}"},
    )
    assert denied.status_code == 403


def test_sqlite_connection_context_always_closes_and_rolls_back(tmp_path, monkeypatch):
    import sqlite3

    database = tmp_path / "uosa-connection-lifecycle.sqlite3"
    monkeypatch.setenv("AMIKI_UOSA_DB_PATH", str(database))
    with uosa_security._connection() as connection:
        connection.execute("CREATE TABLE lifecycle_probe(value TEXT)")
        connection.execute("INSERT INTO lifecycle_probe(value) VALUES ('committed')")
        retained = connection
    with pytest.raises(sqlite3.ProgrammingError):
        retained.execute("SELECT 1")

    with pytest.raises(RuntimeError, match="rollback probe"):
        with uosa_security._connection() as connection:
            connection.execute("INSERT INTO lifecycle_probe(value) VALUES ('rolled-back')")
            raise RuntimeError("rollback probe")

    with uosa_security._connection() as connection:
        values = [
            row["value"]
            for row in connection.execute(
                "SELECT value FROM lifecycle_probe ORDER BY rowid"
            )
        ]
    assert values == ["committed"]


def test_session_challenge_issuance_is_rate_limited_per_client(uosa_db):
    private_key, public_key = _keypair()
    _enroll(private_key, public_key)

    for _ in range(uosa_security.CHALLENGE_ISSUE_MAX_ATTEMPTS):
        challenge = client.post(
            "/api/v1/security/challenges",
            json={
                "device_id": "test-device-1",
                "audience": "amiki-core",
                "scopes": ["ale.review.read"],
            },
        )
        assert challenge.status_code == 200, challenge.text
        completed = client.post(
            "/api/v1/security/sessions",
            json={
                "challenge_id": challenge.json()["challenge_id"],
                "signature": _sign(private_key, challenge.json()),
            },
        )
        assert completed.status_code == 200, completed.text

    blocked = client.post(
        "/api/v1/security/challenges",
        json={
            "device_id": "test-device-1",
            "audience": "amiki-core",
            "scopes": ["ale.review.read"],
        },
    )
    assert blocked.status_code == 429
    assert "rate-limited" in blocked.json()["detail"]
    with uosa_security._connection() as connection:
        attempt = connection.execute(
            "SELECT attempts, blocked_until FROM uosa_challenge_attempts"
        ).fetchone()
    assert attempt["attempts"] == uosa_security.CHALLENGE_ISSUE_MAX_ATTEMPTS + 1
    assert attempt["blocked_until"] is not None


def test_expired_setup_code_is_safely_replaced(tmp_path, monkeypatch, uosa_db):
    import stat

    handoff = tmp_path / "uosa-setup-code"
    monkeypatch.setenv("AMIKI_UOSA_SETUP_CODE_FILE", str(handoff))
    first_now = 1_900_000_000
    monkeypatch.setattr(uosa_security, "_now", lambda: first_now)
    first = uosa_security.create_setup_code()
    assert handoff.read_text(encoding="ascii").strip() == first["setup_code"]
    assert stat.S_IMODE(handoff.stat().st_mode) == 0o600

    monkeypatch.setattr(
        uosa_security,
        "_now",
        lambda: first["expires_at"] + 1,
    )
    second = uosa_security.create_setup_code()
    assert second["setup_code"] != first["setup_code"]
    assert handoff.read_text(encoding="ascii").strip() == second["setup_code"]


def test_expired_setup_does_not_overwrite_unverifiable_handoff_file(
    tmp_path, monkeypatch, uosa_db
):
    handoff = tmp_path / "uosa-setup-code"
    monkeypatch.setenv("AMIKI_UOSA_SETUP_CODE_FILE", str(handoff))
    monkeypatch.setattr(uosa_security, "_now", lambda: 1_900_000_000)
    first = uosa_security.create_setup_code()
    handoff.write_text("unrelated-content\n", encoding="ascii")
    monkeypatch.setattr(
        uosa_security,
        "_now",
        lambda: first["expires_at"] + 1,
    )

    with pytest.raises(uosa_security.UosaConfigurationError, match="does not match"):
        uosa_security.create_setup_code()
    assert handoff.read_text(encoding="ascii") == "unrelated-content\n"


def test_concurrent_l2_consumption_allows_exactly_one_request(uosa_db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    private_key, public_key = _keypair()
    _enroll(private_key, public_key)
    base = _issue_session(private_key, "test-device-1", ["ale.review.approve"])
    endpoint = "/api/v1/learning/review-queue/record-123/approve"
    binding = uosa_security.compute_transaction_binding("POST", endpoint, b"")
    challenge = client.post(
        "/api/v1/security/elevation-challenges",
        headers={"Authorization": f"Bearer {base['access_token']}"},
        json={
            "auth_level": "L2",
            "scope": "ale.review.approve",
            "transaction_hash": binding,
        },
    )
    assert challenge.status_code == 200
    elevated = client.post(
        "/api/v1/security/elevated-sessions",
        json={
            "challenge_id": challenge.json()["challenge_id"],
            "signature": _sign(private_key, challenge.json()),
        },
    )
    assert elevated.status_code == 200
    token = elevated.json()["access_token"]
    barrier = Barrier(2)

    def attempt():
        barrier.wait(timeout=5)
        return uosa_security.consume_elevated_session(
            token, "ale.review.approve", binding
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == [False, True]


def test_secondary_device_cannot_complete_another_devices_enrollment(uosa_db):
    owner_private, owner_public = _keypair()
    _enroll(owner_private, owner_public)
    second_private, _, _ = _add_device(
        owner_private, "test-device-1", "second-device"
    )
    new_private, new_public = _keypair()
    base = _issue_session(
        owner_private, "test-device-1", ["uosa.device.enroll"]
    )
    challenge = client.post(
        "/api/v1/security/devices/enrollment-challenges",
        headers={"Authorization": f"Bearer {base['access_token']}"},
        json={
            "device_id": "third-device",
            "label": "third test device",
            "public_key": new_public,
        },
    )
    assert challenge.status_code == 200
    payload = {
        "challenge_id": challenge.json()["challenge_id"],
        "signature": _sign(new_private, challenge.json()),
    }
    endpoint = "/api/v1/security/devices/enroll"
    body = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    second_session = _issue_session(
        second_private, "second-device", ["uosa.device.enroll"]
    )
    binding = uosa_security.compute_transaction_binding("POST", endpoint, body)
    elevation = client.post(
        "/api/v1/security/elevation-challenges",
        headers={"Authorization": f"Bearer {second_session['access_token']}"},
        json={
            "auth_level": "L2",
            "scope": "uosa.device.enroll",
            "transaction_hash": binding,
        },
    )
    assert elevation.status_code == 200
    elevated = client.post(
        "/api/v1/security/elevated-sessions",
        json={
            "challenge_id": elevation.json()["challenge_id"],
            "signature": _sign(second_private, elevation.json()),
        },
    )
    assert elevated.status_code == 200

    denied = client.post(
        endpoint,
        headers={"Authorization": f"Bearer {elevated.json()['access_token']}"},
        json=payload,
    )
    assert denied.status_code == 403
    assert all(
        item["device_id"] != "third-device"
        for item in uosa_security.list_devices()
    )


def test_secondary_device_cannot_complete_primary_device_key_rotation(uosa_db):
    owner_private, owner_public = _keypair()
    _enroll(owner_private, owner_public)
    second_private, _, _ = _add_device(
        owner_private, "test-device-1", "second-device"
    )
    new_private, new_public = _keypair()
    primary_session = _issue_session(
        owner_private, "test-device-1", ["uosa.device.rotate"]
    )
    challenge = client.post(
        "/api/v1/security/devices/test-device-1/rotation-challenges",
        headers={"Authorization": f"Bearer {primary_session['access_token']}"},
        json={"replacement_public_key": new_public},
    )
    assert challenge.status_code == 200
    payload = {
        "challenge_id": challenge.json()["challenge_id"],
        "old_signature": _sign(owner_private, challenge.json()),
        "new_signature": _sign(new_private, challenge.json()),
    }
    endpoint = "/api/v1/security/devices/test-device-1/rotate"
    body = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    second_session = _issue_session(
        second_private, "second-device", ["uosa.device.rotate"]
    )
    binding = uosa_security.compute_transaction_binding("POST", endpoint, body)
    elevation = client.post(
        "/api/v1/security/elevation-challenges",
        headers={"Authorization": f"Bearer {second_session['access_token']}"},
        json={
            "auth_level": "L2",
            "scope": "uosa.device.rotate",
            "transaction_hash": binding,
        },
    )
    assert elevation.status_code == 200
    elevated = client.post(
        "/api/v1/security/elevated-sessions",
        json={
            "challenge_id": elevation.json()["challenge_id"],
            "signature": _sign(second_private, elevation.json()),
        },
    )
    assert elevated.status_code == 200

    denied = client.post(
        endpoint,
        headers={"Authorization": f"Bearer {elevated.json()['access_token']}"},
        json=payload,
    )
    assert denied.status_code == 403
    devices = uosa_security.list_devices()
    primary = next(item for item in devices if item["device_id"] == "test-device-1")
    assert primary["enrollment_generation"] == 1
