"""Cryptographic UOSA-backed TestClient for owner-route regression tests.

Legacy-looking OWNER_TOKEN values are test sentinels only. This client never
sends them to protected business routes: it enrolls a disposable P-256 device,
obtains a real L1 session, and signs a fresh one-use L2 grant for exact requests.
"""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from core import uosa_security


def _sign(private_key, challenge: dict) -> str:
    signature = private_key.sign(
        challenge["message"].encode("utf-8"),
        ec.ECDSA(hashes.SHA256()),
    )
    return base64.b64encode(signature).decode("ascii")


def _scope_for(method: str, path: str) -> str | None:
    method = method.upper()
    route = path.removeprefix("/api/v1")
    if route == "/learning/progress":
        return "ale.progress.read" if method == "GET" else None
    if re.fullmatch(r"/learning/progress/[^/]+/attempt", route):
        return "ale.progress.write" if method == "POST" else None
    if re.fullmatch(r"/learning/curricula/[^/]+/progress", route):
        return "ale.progress.read" if method == "GET" else None
    if re.fullmatch(r"/learning/curricula/[^/]+/progress/[^/]+/attempt", route):
        return "ale.progress.write" if method == "POST" else None
    if route == "/learning/projects":
        return {
            "GET": "ale.projects.read",
            "POST": "ale.projects.write",
        }.get(method)
    if re.fullmatch(r"/learning/projects/[^/]+", route):
        return "ale.projects.read" if method == "GET" else None
    if re.fullmatch(r"/learning/projects/[^/]+/stages/[^/]+/attempt", route):
        return "ale.projects.write" if method == "POST" else None
    if route == "/learning/knowledge-map":
        return "ale.knowledge.read" if method == "GET" else None
    if route == "/learning/knowledge-map/semantic-analysis":
        return "ale.claims.analyze" if method == "POST" else None
    if re.fullmatch(r"/learning/claims/[^/]+/evidence-assessment", route):
        return "ale.claims.review" if method == "GET" else None
    if re.fullmatch(r"/learning/claims/[^/]+/evidence-evaluation", route):
        return "ale.claims.evaluate" if method == "POST" else None
    if re.fullmatch(r"/learning/knowledge/[^/]+/extract-claims", route):
        return "ale.claims.extract" if method == "POST" else None
    if route == "/learning/review-queue":
        return "ale.review.read" if method == "GET" else None
    if re.fullmatch(r"/learning/review-queue/[^/]+/approve", route):
        return "ale.review.approve" if method == "POST" else None
    if re.fullmatch(r"/learning/review-queue/[^/]+/reject", route):
        return "ale.review.reject" if method == "POST" else None
    if route == "/security/devices":
        return "uosa.device.read" if method == "GET" else None
    if route == "/security/audit-events":
        return "uosa.security.audit.read" if method == "GET" else None
    if route == "/security/recovery/codes/rotate":
        return "uosa.recovery.manage" if method == "POST" else None
    if route == "/security/devices/enrollment-challenges":
        return "uosa.device.enroll" if method == "POST" else None
    if route == "/security/devices/enroll":
        return "uosa.device.enroll" if method == "POST" else None
    if re.fullmatch(r"/security/devices/[^/]+/rotation-challenges", route):
        return "uosa.device.rotate" if method == "POST" else None
    if re.fullmatch(r"/security/devices/[^/]+/rotate", route):
        return "uosa.device.rotate" if method == "POST" else None
    if re.fullmatch(r"/security/devices/[^/]+/revoke", route):
        return "uosa.device.revoke" if method == "POST" else None
    if route in {"/chat", "/chat/attachments", "/voice"}:
        return "chat.use" if method == "POST" else None
    if route == "/conversations":
        return (
            "conversations.read" if method == "GET"
            else "conversations.write" if method == "POST"
            else None
        )
    if re.fullmatch(r"/conversations/[^/]+(?:/messages)?", route):
        return "conversations.read" if method == "GET" else None
    if route == "/academic":
        return "academic.write" if method in {"PUT", "POST"} else "academic.read"
    if re.fullmatch(r"/academic/courses(?:/[^/]+)?(?:/study-records)?", route):
        return "academic.write" if method in {"PUT", "POST"} else "academic.read"
    if route == "/agents/run":
        return "agents.run" if method == "POST" else None
    if route in {"/cipher/status", "/cipher/inspect"}:
        return "cipher.inspect" if method in {"GET", "POST"} else None
    if route == "/device/state":
        return "device.read" if method == "GET" else None
    if route == "/device/plan" or route == "/device/plan-request":
        return "device.plan" if method == "POST" else None
    if route == "/device/execute":
        return "device.execute" if method == "POST" else None
    if route == "/memory":
        return "memory.read" if method == "GET" else None
    if route == "/memory/current-session":
        return "memory.session.clear" if method == "DELETE" else None
    if route == "/memory/persistent":
        return "memory.persistent.clear" if method == "DELETE" else None
    if route in {"/settings", "/settings/model", "/settings/memory"}:
        return (
            "settings.read" if method == "GET"
            else "settings.write" if method == "PUT"
            else None
        )
    if route == "/system-state":
        return "device.read" if method == "GET" else None
    if route == "/database/status" or route == "/diagnostics":
        return "observability.read" if method == "GET" else None
    if route in {
        "/observability/events",
        "/observability/diagnostics",
        "/observability/status",
    } or re.fullmatch(r"/observability/diagnostics/[^/]+", route):
        return "observability.read" if method == "GET" else None
    if route.startswith("/workspace/"):
        if method == "GET":
            return "workspace.read"
        if route == "/workspace/projects" and method == "POST":
            return "workspace.project.create"
        if route in {"/workspace/change-proposals", "/workspace/change-proposals/generate"} and method == "POST":
            return "workspace.proposals.write"
        if re.fullmatch(r"/workspace/change-proposals/[^/]+/approve", route) and method == "POST":
            return "workspace.change.apply"
        if re.fullmatch(r"/workspace/change-proposals/[^/]+/reject", route) and method == "POST":
            return "workspace.proposals.write"
        if route in {"/workspace/file", "/workspace/new-file"} and method == "POST":
            return "workspace.file.write"
        if route == "/workspace/command" and method == "POST":
            return "workspace.command.execute"
        if route == "/workspace/check" and method == "POST":
            return "workspace.read"
        if route == "/workspace/execute" and method == "POST":
            return "workspace.file.execute"
    if route == "/cloud/autonomy/status":
        return "cloud.autonomy.read" if method == "GET" else None
    if route == "/cloud/autonomy/stop":
        return "cloud.autonomy.stop" if method == "POST" else None
    if route == "/cloud/autonomy/resume":
        return "cloud.autonomy.resume" if method == "POST" else None
    if route == "/cloud/autonomy/worker/status":
        return "cloud.autonomy.read" if method == "GET" else None
    if route == "/cloud/jobs":
        return (
            "cloud.jobs.read" if method == "GET"
            else "cloud.jobs.create" if method == "POST"
            else None
        )
    if re.fullmatch(r"/cloud/jobs/[^/]+", route):
        return "cloud.jobs.read" if method == "GET" else None
    if re.fullmatch(r"/cloud/jobs/[^/]+/run", route):
        return "cloud.jobs.run" if method == "POST" else None
    if re.fullmatch(r"/cloud/jobs/[^/]+/cancel", route):
        return "cloud.jobs.cancel" if method == "POST" else None
    if route == "/cloud/goal-schedules":
        return (
            "cloud.goals.read" if method == "GET"
            else "cloud.schedules.create" if method == "POST"
            else None
        )
    if re.fullmatch(r"/cloud/goal-schedules/[^/]+/runs", route):
        return "cloud.goals.read" if method == "GET" else None
    if re.fullmatch(r"/cloud/goal-schedules/[^/]+/(pause|resume|cancel)", route):
        return "cloud.schedules.manage" if method == "POST" else None
    if route == "/cloud/goals":
        return (
            "cloud.goals.read" if method == "GET"
            else "cloud.goals.create" if method == "POST"
            else None
        )
    if route == "/cloud/goals/plan":
        return "cloud.goals.create" if method == "POST" else None
    if re.fullmatch(r"/cloud/goals/[^/]+", route):
        return "cloud.goals.read" if method == "GET" else None
    if re.fullmatch(r"/cloud/goals/[^/]+/(pause|cancel)", route):
        return "cloud.goals.pause" if method == "POST" else None
    if re.fullmatch(r"/cloud/goals/[^/]+/resume", route):
        return "cloud.goals.resume" if method == "POST" else None
    if re.fullmatch(r"/cloud/goals/[^/]+/tasks/[^/]+", route):
        return "cloud.goals.update" if method == "PATCH" else None
    if route == "/cloud/monitor/status":
        return "cloud.monitor.read" if method == "GET" else None
    if route == "/cloud/monitor/events":
        return "cloud.monitor.read" if method == "GET" else None
    if re.fullmatch(r"/cloud/monitor/events/\d+/acknowledge", route):
        return "cloud.monitor.acknowledge" if method == "POST" else None
    if route == "/cloud/autonomy/worker/outbox/dead-letters":
        return "cloud.outbox.inspect" if method == "GET" else None
    if route == "/cloud/autonomy/worker/outbox/retry":
        return "cloud.outbox.retry" if method == "POST" else None
    if route == "/cloud/autonomy/worker/outbox/supervisor/restart":
        return "cloud.outbox.restart" if method == "POST" else None
    return None


class UosaTestClient(TestClient):
    """TestClient that translates an explicit test sentinel into real UOSA proof."""

    def __init__(self, app, owner_token: str, *, auto_owner_auth: bool = False, **kwargs):
        super().__init__(app, **kwargs)
        self._owner_token = owner_token
        self._auto_owner_auth = auto_owner_auth
        self._state_dir = Path(tempfile.mkdtemp(prefix="amiki-uosa-test-"))
        self._db_path = self._state_dir / "uosa.sqlite3"
        self._device_id = "pytest-" + self._state_dir.name[-24:]
        self._private_key = ec.generate_private_key(ec.SECP256R1())
        public_der = self._private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self._public_key_b64 = base64.b64encode(public_der).decode("ascii")
        self._session_token: str | None = None

    def _prepare_session(self) -> str:
        os.environ["AMIKI_UOSA_DB_PATH"] = str(self._db_path)
        os.environ["AMIKI_UOSA_SETUP_CODE_FILE"] = str(
            self._state_dir / "uosa-setup-code"
        )
        if self._session_token is not None:
            return self._session_token

        setup = uosa_security.create_setup_code()
        challenge = super().request(
            "POST",
            "/api/v1/security/setup/enrollment-challenges",
            json={
                "setup_code": setup["setup_code"],
                "device_id": self._device_id,
                "label": "pytest UOSA device",
                "public_key": self._public_key_b64,
            },
        )
        assert challenge.status_code == 200, challenge.text
        enrolled = super().request(
            "POST",
            "/api/v1/security/setup/enroll",
            json={
                "challenge_id": challenge.json()["challenge_id"],
                "signature": _sign(self._private_key, challenge.json()),
            },
        )
        assert enrolled.status_code == 200, enrolled.text

        session_challenge = super().request(
            "POST",
            "/api/v1/security/challenges",
            json={
                "device_id": self._device_id,
                "audience": uosa_security.OWNER_AUDIENCE,
                "scopes": sorted(uosa_security.ALLOWED_SCOPES),
            },
        )
        assert session_challenge.status_code == 200, session_challenge.text
        issued = super().request(
            "POST",
            "/api/v1/security/sessions",
            json={
                "challenge_id": session_challenge.json()["challenge_id"],
                "signature": _sign(self._private_key, session_challenge.json()),
            },
        )
        assert issued.status_code == 200, issued.text
        self._session_token = issued.json()["access_token"]
        return self._session_token

    def request(self, method, url, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        supplied = headers.get("Authorization") or headers.get("authorization")
        parsed = urlsplit(str(url))
        scope = _scope_for(method, parsed.path)
        if supplied is None and self._auto_owner_auth and scope is not None:
            supplied = f"Bearer {self._owner_token}"
        if supplied == f"Bearer {self._owner_token}":
            params = kwargs.get("params")
            query = parsed.query
            if params:
                encoded = urlencode(params, doseq=True)
                query = f"{query}&{encoded}" if query else encoded
            request_path = parsed.path + (f"?{query}" if query else "")
            if scope is not None:
                base_token = self._prepare_session()
                auth_level = uosa_security.SCOPE_AUTH_LEVEL[scope]
                if (
                    parsed.path.endswith("/rotation-challenges")
                    or parsed.path.endswith("/enrollment-challenges")
                ):
                    auth_level = "L1"
                active_token = base_token
                if auth_level == "L2":
                    body_object = kwargs.get("json")
                    body = (
                        json.dumps(
                            body_object,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode("utf-8")
                        if body_object is not None
                        else b""
                    )
                    transaction_hash = uosa_security.compute_transaction_binding(
                        method, request_path, body
                    )
                    elevation = super().request(
                        "POST",
                        "/api/v1/security/elevation-challenges",
                        headers={"Authorization": f"Bearer {base_token}"},
                        json={
                            "auth_level": "L2",
                            "scope": scope,
                            "transaction_hash": transaction_hash,
                        },
                    )
                    assert elevation.status_code == 200, elevation.text
                    proof = super().request(
                        "POST",
                        "/api/v1/security/elevated-sessions",
                        json={
                            "challenge_id": elevation.json()["challenge_id"],
                            "signature": _sign(self._private_key, elevation.json()),
                        },
                    )
                    assert proof.status_code == 200, proof.text
                    active_token = proof.json()["access_token"]
                headers.pop("authorization", None)
                headers["Authorization"] = f"Bearer {active_token}"
                kwargs["headers"] = headers

        if self._db_path.exists():
            os.environ["AMIKI_UOSA_DB_PATH"] = str(self._db_path)
        return super().request(method, url, **kwargs)
