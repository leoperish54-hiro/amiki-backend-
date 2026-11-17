"""UOSA device enrollment and short-lived owner session primitives.

This module stores only public device keys and hashes of opaque session tokens.
Biometric verification happens locally; the backend verifies ECDSA signatures.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


CHALLENGE_TTL_SECONDS = 120
CHALLENGE_ISSUE_WINDOW_SECONDS = 300
CHALLENGE_ISSUE_MAX_ATTEMPTS = 30
CHALLENGE_ISSUE_LOCK_SECONDS = 120
SESSION_TTL_SECONDS = 900
ELEVATED_SESSION_TTL_SECONDS = 120
RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_TTL_SECONDS = 365 * 24 * 60 * 60
RECOVERY_MAX_ATTEMPTS = 10
RECOVERY_WINDOW_SECONDS = 900
RECOVERY_LOCK_SECONDS = 900
MAX_SECURITY_EVENTS = 10000
SETUP_CODE_TTL_SECONDS = 900
MAX_ACTIVE_DEVICES = 5
OWNER_AUDIENCE = "amiki-core"
L1_SCOPES = frozenset({
    "ale.review.read",
    "ale.progress.read",
    "ale.progress.write",
    "ale.projects.read",
    "ale.projects.write",
    "ale.knowledge.read",
    "ale.claims.analyze",
    "ale.claims.review",
    "cloud.outbox.inspect",
    "uosa.device.read",
    "uosa.security.audit.read",
    "academic.read",
    "academic.write",

    "cipher.inspect",
    "device.read",
    "device.plan",
    "memory.read",
    "memory.session.clear",
    "settings.read",
    "observability.read",
    "chat.use",
    "conversations.read",
    "conversations.write",
    "workspace.read",
    "workspace.proposals.write",
    "cloud.autonomy.read",
    "cloud.jobs.read",
    "cloud.goals.read",
    "cloud.monitor.read",
    "cloud.autonomy.stop",
    "cloud.jobs.cancel",
    "cloud.goals.pause",
    "cloud.monitor.acknowledge",
})
L2_SCOPES = frozenset({
    "ale.review.approve",
    "ale.review.reject",
    "ale.claims.evaluate",
    "ale.claims.extract",
    "agents.run",
    "cloud.outbox.retry",
    "cloud.outbox.restart",
    "uosa.device.revoke",
    "uosa.device.rotate",
    "uosa.recovery.manage",
    "uosa.device.enroll",
    "settings.write",
    "device.execute",
    "memory.persistent.clear",
    "workspace.project.create",
    "workspace.change.apply",
    "workspace.file.write",
    "workspace.file.execute",
    "workspace.command.execute",
    "cloud.autonomy.resume",
    "cloud.jobs.create",
    "cloud.jobs.run",
    "cloud.goals.create",
    "cloud.goals.resume",
    "cloud.goals.update",
    "cloud.schedules.create",
    "cloud.schedules.manage",
})
ALLOWED_SCOPES = L1_SCOPES | L2_SCOPES
SCOPE_AUTH_LEVEL = {
    **{scope: "L1" for scope in L1_SCOPES},
    **{scope: "L2" for scope in L2_SCOPES},
}
AUTH_LEVEL_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
DOMAIN = "AMIKI-UOSA-CHALLENGE-v1\n"


class UosaError(ValueError):
    """Base class for safe, client-displayable UOSA errors."""

    status_code = 400


class UosaValidationError(UosaError):
    status_code = 422


class UosaAuthenticationError(UosaError):
    status_code = 401


class UosaAuthorizationError(UosaError):
    status_code = 403


class UosaConflictError(UosaError):
    status_code = 409


class UosaRateLimitError(UosaError):
    status_code = 429


class UosaConfigurationError(UosaError):
    status_code = 500


def _now() -> int:
    return int(time.time())


def _database_path() -> Path:
    configured = os.getenv("AMIKI_UOSA_DB_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise UosaConfigurationError("UOSA database path must be absolute.")
        return path
    return Path.home() / ".local" / "state" / "amiki" / "uosa.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    connection = sqlite3.connect(str(path), timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection




@contextmanager
def _connection():
    """Yield one SQLite connection and always commit/rollback and close it."""
    connection = _connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS uosa_devices (
            device_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            public_key_der BLOB NOT NULL,
            key_fingerprint TEXT NOT NULL UNIQUE,
            enrollment_generation INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            revoked_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS uosa_challenges (
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
        CREATE INDEX IF NOT EXISTS idx_uosa_challenges_device
            ON uosa_challenges(device_id, created_at);
        CREATE TABLE IF NOT EXISTS uosa_sessions (
            token_hash TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            audience TEXT NOT NULL,
            scopes_json TEXT NOT NULL,
            auth_level TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked_at INTEGER,
            device_generation INTEGER NOT NULL DEFAULT 1,
            transaction_hash TEXT,
            single_use INTEGER NOT NULL DEFAULT 0,
            consumed_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_uosa_sessions_device
            ON uosa_sessions(device_id, expires_at);
        CREATE TABLE IF NOT EXISTS uosa_security_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            device_id TEXT,
            created_at INTEGER NOT NULL,
            details_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS uosa_recovery_state (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            generation INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS uosa_recovery_codes (
            code_hash TEXT PRIMARY KEY,
            generation INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            consumed_at INTEGER,
            revoked_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_uosa_recovery_codes_generation
            ON uosa_recovery_codes(generation, expires_at);
        CREATE TABLE IF NOT EXISTS uosa_recovery_attempts (
            client_key_hash TEXT PRIMARY KEY,
            window_started_at INTEGER NOT NULL,
            attempts INTEGER NOT NULL,
            blocked_until INTEGER
        );
        CREATE TABLE IF NOT EXISTS uosa_setup_state (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            code_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            consumed_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS uosa_challenge_attempts (
            client_key_hash TEXT PRIMARY KEY,
            window_started_at INTEGER NOT NULL,
            attempts INTEGER NOT NULL,
            blocked_until INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_uosa_sessions_expiry
            ON uosa_sessions(expires_at);
        CREATE INDEX IF NOT EXISTS idx_uosa_challenges_expiry
            ON uosa_challenges(expires_at);
        """
    )
    device_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(uosa_devices)")
    }
    if "enrollment_generation" not in device_columns:
        connection.execute(
            "ALTER TABLE uosa_devices ADD COLUMN enrollment_generation "
            "INTEGER NOT NULL DEFAULT 1"
        )
    session_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(uosa_sessions)")
    }
    if "device_generation" not in session_columns:
        connection.execute(
            "ALTER TABLE uosa_sessions ADD COLUMN device_generation "
            "INTEGER NOT NULL DEFAULT 1"
        )
    if "transaction_hash" not in session_columns:
        connection.execute("ALTER TABLE uosa_sessions ADD COLUMN transaction_hash TEXT")
    if "single_use" not in session_columns:
        connection.execute(
            "ALTER TABLE uosa_sessions ADD COLUMN single_use INTEGER NOT NULL DEFAULT 0"
        )
    if "consumed_at" not in session_columns:
        connection.execute("ALTER TABLE uosa_sessions ADD COLUMN consumed_at INTEGER")
    connection.commit()
    try:
        os.chmod(_database_path(), 0o600)
    except OSError:
        pass


def _event(
    connection: sqlite3.Connection,
    event_type: str,
    device_id: str | None,
    details: dict[str, Any],
) -> None:
    inserted = connection.execute(
        "INSERT INTO uosa_security_events(event_type, device_id, created_at, details_json) "
        "VALUES (?, ?, ?, ?)",
        (event_type, device_id, _now(), json.dumps(details, sort_keys=True)),
    )
    if inserted.lastrowid and inserted.lastrowid % 100 == 0:
        connection.execute(
            "DELETE FROM uosa_security_events WHERE event_id NOT IN "
            "(SELECT event_id FROM uosa_security_events "
            "ORDER BY event_id DESC LIMIT ?)",
            (MAX_SECURITY_EVENTS,),
        )


def _decode_b64(value: str, field: str) -> bytes:
    if not value or len(value) > 8192:
        raise UosaValidationError(f"Invalid {field}.")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise UosaValidationError(f"Invalid {field} encoding.") from exc


def _load_p256_public_key(encoded: bytes) -> ec.EllipticCurvePublicKey:
    try:
        key = serialization.load_der_public_key(encoded)
    except (ValueError, TypeError) as exc:
        raise UosaValidationError("The device public key is invalid.") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise UosaValidationError("Only P-256 ECDSA device keys are accepted.")
    return key


def _fingerprint(public_key_der: bytes) -> str:
    return hashlib.sha256(public_key_der).hexdigest()


def _challenge_message(
    challenge_id: str,
    nonce: str,
    purpose: str,
    device_id: str,
    created_at: int,
    expires_at: int,
    audience: str = "",
    scopes: list[str] | None = None,
    key_fingerprint: str = "",
    enrollment_generation: int = 0,
    auth_level: str = "L1",
    transaction_hash: str = "",
    replacement_key_fingerprint: str = "",
) -> str:
    payload = {
        "audience": audience,
        "challenge_id": challenge_id,
        "auth_level": auth_level,
        "created_at": created_at,
        "device_id": device_id,
        "expires_at": expires_at,
        "key_fingerprint": key_fingerprint,
        "replacement_key_fingerprint": replacement_key_fingerprint,
        "enrollment_generation": enrollment_generation,
        "nonce": nonce,
        "purpose": purpose,
        "scopes": sorted(scopes or []),
        "transaction_hash": transaction_hash,
    }
    return DOMAIN + json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _new_challenge(
    connection: sqlite3.Connection,
    *,
    purpose: str,
    device_id: str,
    context: dict[str, Any],
    audience: str = "",
    scopes: list[str] | None = None,
    key_fingerprint: str = "",
    enrollment_generation: int = 0,
    auth_level: str = "L1",
    transaction_hash: str = "",
    replacement_key_fingerprint: str = "",
) -> dict[str, Any]:
    now = _now()
    connection.execute("DELETE FROM uosa_challenges WHERE expires_at <= ?", (now,))
    pending = connection.execute(
        "SELECT COUNT(*) AS count FROM uosa_challenges "
        "WHERE device_id = ? AND consumed_at IS NULL AND expires_at > ?",
        (device_id, now),
    ).fetchone()["count"]
    if pending >= 5:
        raise UosaRateLimitError("Too many pending challenges for this device.")
    expires = now + CHALLENGE_TTL_SECONDS
    challenge_id = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(32)
    message = _challenge_message(
        challenge_id, nonce, purpose, device_id, now, expires,
        audience, scopes, key_fingerprint, enrollment_generation,
        auth_level, transaction_hash, replacement_key_fingerprint,
    )
    connection.execute(
        "INSERT INTO uosa_challenges "
        "(challenge_id, purpose, device_id, nonce, message, context_json, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            challenge_id, purpose, device_id, nonce, message,
            json.dumps(context, sort_keys=True), now, expires,
        ),
    )
    return {
        "challenge_id": challenge_id,
        "message": message,
        "expires_at": expires,
        "expires_in_seconds": CHALLENGE_TTL_SECONDS,
        "signature_algorithm": "SHA256withECDSA",
        "public_key_format": "X.509 SubjectPublicKeyInfo DER",
        "signature_format": "ASN.1 DER ECDSA",
    }




def _recovery_code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def _recovery_client_hash(client_key: str) -> str:
    normalized = (client_key or "unknown").strip()[:256] or "unknown"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _check_recovery_rate_limit(
    connection: sqlite3.Connection, client_key: str, now: int
) -> None:
    client_hash = _recovery_client_hash(client_key)
    row = connection.execute(
        "SELECT window_started_at, attempts, blocked_until "
        "FROM uosa_recovery_attempts WHERE client_key_hash = ?",
        (client_hash,),
    ).fetchone()
    if row is not None and row["blocked_until"] is not None and row["blocked_until"] > now:
        raise UosaRateLimitError("Owner recovery is temporarily rate-limited.")
    if row is None or now - row["window_started_at"] >= RECOVERY_WINDOW_SECONDS:
        connection.execute(
            "INSERT INTO uosa_recovery_attempts "
            "(client_key_hash, window_started_at, attempts, blocked_until) "
            "VALUES (?, ?, 1, NULL) "
            "ON CONFLICT(client_key_hash) DO UPDATE SET "
            "window_started_at=excluded.window_started_at, attempts=1, blocked_until=NULL",
            (client_hash, now),
        )
        connection.commit()
        return
    attempts = row["attempts"] + 1
    blocked_until = now + RECOVERY_LOCK_SECONDS if attempts > RECOVERY_MAX_ATTEMPTS else None
    connection.execute(
        "UPDATE uosa_recovery_attempts SET attempts = ?, blocked_until = ? "
        "WHERE client_key_hash = ?",
        (attempts, blocked_until, client_hash),
    )
    if blocked_until is not None:
        _event(connection, "recovery.rate_limited", None, {
            "client_key_hash": client_hash,
            "blocked_until": blocked_until,
        })
        connection.commit()
        raise UosaRateLimitError("Owner recovery is temporarily rate-limited.")
    connection.commit()




def _check_challenge_issue_limit(
    connection: sqlite3.Connection, client_key: str, now: int
) -> None:
    client_hash = _recovery_client_hash(client_key)
    row = connection.execute(
        "SELECT window_started_at, attempts, blocked_until "
        "FROM uosa_challenge_attempts WHERE client_key_hash = ?",
        (client_hash,),
    ).fetchone()
    if row is not None and row["blocked_until"] is not None and row["blocked_until"] > now:
        raise UosaRateLimitError("Owner authentication challenge requests are rate-limited.")
    if row is None or now - row["window_started_at"] >= CHALLENGE_ISSUE_WINDOW_SECONDS:
        connection.execute(
            "INSERT INTO uosa_challenge_attempts "
            "(client_key_hash, window_started_at, attempts, blocked_until) "
            "VALUES (?, ?, 1, NULL) "
            "ON CONFLICT(client_key_hash) DO UPDATE SET "
            "window_started_at=excluded.window_started_at, attempts=1, blocked_until=NULL",
            (client_hash, now),
        )
        return
    attempts = row["attempts"] + 1
    blocked_until = (
        now + CHALLENGE_ISSUE_LOCK_SECONDS
        if attempts > CHALLENGE_ISSUE_MAX_ATTEMPTS
        else None
    )
    connection.execute(
        "UPDATE uosa_challenge_attempts SET attempts = ?, blocked_until = ? "
        "WHERE client_key_hash = ?",
        (attempts, blocked_until, client_hash),
    )
    if blocked_until is not None:
        _event(connection, "session.challenge_rate_limited", None, {
            "client_key_hash": client_hash,
            "blocked_until": blocked_until,
        })
        connection.commit()
        raise UosaRateLimitError("Owner authentication challenge requests are rate-limited.")


def _prune_expired_records(connection: sqlite3.Connection, now: int) -> None:
    """Bound ephemeral credential history while retaining recent audit context."""
    connection.execute("DELETE FROM uosa_challenges WHERE expires_at < ?", (now - 86400,))
    connection.execute(
        "DELETE FROM uosa_sessions WHERE expires_at < ? OR "
        "(revoked_at IS NOT NULL AND revoked_at < ?)",
        (now - 86400, now - 86400),
    )
    connection.execute(
        "DELETE FROM uosa_recovery_codes WHERE expires_at < ? OR "
        "(revoked_at IS NOT NULL AND revoked_at < ?) OR "
        "(consumed_at IS NOT NULL AND consumed_at < ?)",
        (now - 86400, now - 86400, now - 86400),
    )
    connection.execute(
        "DELETE FROM uosa_recovery_attempts WHERE window_started_at < ? "
        "AND (blocked_until IS NULL OR blocked_until < ?)",
        (now - (30 * 86400), now),
    )
    connection.execute(
        "DELETE FROM uosa_challenge_attempts WHERE window_started_at < ? "
        "AND (blocked_until IS NULL OR blocked_until < ?)",
        (now - (30 * 86400), now),
    )


def _rotate_recovery_codes(
    connection: sqlite3.Connection, now: int
) -> tuple[list[str], int]:
    current = connection.execute(
        "SELECT generation FROM uosa_recovery_state WHERE singleton_id = 1"
    ).fetchone()
    generation = current["generation"] + 1 if current is not None else 1
    connection.execute(
        "INSERT INTO uosa_recovery_state(singleton_id, generation, updated_at) "
        "VALUES (1, ?, ?) "
        "ON CONFLICT(singleton_id) DO UPDATE SET "
        "generation=excluded.generation, updated_at=excluded.updated_at",
        (generation, now),
    )
    connection.execute(
        "UPDATE uosa_recovery_codes SET revoked_at = ? "
        "WHERE consumed_at IS NULL AND revoked_at IS NULL",
        (now,),
    )
    codes = [secrets.token_urlsafe(32) for _ in range(RECOVERY_CODE_COUNT)]
    connection.executemany(
        "INSERT INTO uosa_recovery_codes "
        "(code_hash, generation, created_at, expires_at, consumed_at, revoked_at) "
        "VALUES (?, ?, ?, ?, NULL, NULL)",
        [
            (_recovery_code_hash(code), generation, now, now + RECOVERY_CODE_TTL_SECONDS)
            for code in codes
        ],
    )
    _event(connection, "recovery.codes_issued", None, {
        "generation": generation,
        "count": RECOVERY_CODE_COUNT,
        "expires_at": now + RECOVERY_CODE_TTL_SECONDS,
    })
    return codes, generation




def _setup_code_path() -> Path:
    configured = os.getenv("AMIKI_UOSA_SETUP_CODE_FILE", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise UosaConfigurationError("UOSA setup-code path must be absolute.")
        return path
    return Path.home() / ".config" / "amiki" / "uosa-setup-code"


def create_setup_code() -> dict[str, Any]:
    """Create the one-time first-owner setup code, stored on disk only for handoff."""
    path = _setup_code_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    code = secrets.token_urlsafe(32)
    now = _now()
    created_file = False
    try:
        with _connection() as connection:
            _initialize(connection)
            connection.execute("BEGIN IMMEDIATE")
            device_count = connection.execute(
                "SELECT COUNT(*) AS count FROM uosa_devices"
            ).fetchone()["count"]
            recovery_state = connection.execute(
                "SELECT 1 FROM uosa_recovery_state WHERE singleton_id = 1"
            ).fetchone()
            if device_count or recovery_state is not None:
                raise UosaConflictError(
                    "Initial setup is disabled after UOSA has been initialized; "
                    "use the recovery ceremony instead."
                )
            current = connection.execute(
                "SELECT code_hash, expires_at, consumed_at "
                "FROM uosa_setup_state WHERE singleton_id = 1"
            ).fetchone()
            if (
                current is not None
                and current["consumed_at"] is None
                and current["expires_at"] > now
            ):
                raise UosaConflictError("A valid owner setup code already exists.")
            if path.exists() or path.is_symlink():
                if current is None or path.is_symlink():
                    raise UosaConfigurationError(
                        "The setup-code handoff file does not match a known expired setup. "
                        "Inspect it manually; it was not replaced."
                    )
                try:
                    existing_code = path.read_text(encoding="ascii").strip()
                    existing_hash = hashlib.sha256(existing_code.encode("ascii")).hexdigest()
                except (OSError, UnicodeError):
                    raise UosaConfigurationError(
                        "The expired setup-code handoff file cannot be verified; "
                        "it was not replaced."
                    )
                if not secrets.compare_digest(existing_hash, current["code_hash"]):
                    raise UosaConfigurationError(
                        "The setup-code handoff file does not match its stored hash; "
                        "it was not replaced."
                    )
                path.unlink()
            try:
                descriptor = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError as exc:
                raise UosaConfigurationError(
                    "The protected setup-code file already exists; inspect and remove it "
                    "only after confirming it is no longer needed."
                ) from exc
            created_file = True
            try:
                with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                    handle.write(code + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            connection.execute(
                "INSERT INTO uosa_setup_state "
                "(singleton_id, code_hash, created_at, expires_at, consumed_at) "
                "VALUES (1, ?, ?, ?, NULL) "
                "ON CONFLICT(singleton_id) DO UPDATE SET "
                "code_hash=excluded.code_hash, created_at=excluded.created_at, "
                "expires_at=excluded.expires_at, consumed_at=NULL",
                (
                    hashlib.sha256(code.encode("ascii")).hexdigest(),
                    now,
                    now + SETUP_CODE_TTL_SECONDS,
                ),
            )
            connection.commit()
    except Exception:
        if created_file:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return {
        "status": "created",
        "expires_at": now + SETUP_CODE_TTL_SECONDS,
        "expires_in_seconds": SETUP_CODE_TTL_SECONDS,
        "path": str(path),
        "setup_code": code,
    }


def create_setup_enrollment_challenge(
    *,
    setup_code: str,
    device_id: str,
    label: str,
    public_key_b64: str,
) -> dict[str, Any]:
    """Begin initial owner enrollment using the one-time setup code."""
    if not device_id or len(device_id) > 80:
        raise UosaValidationError("Invalid device identifier.")
    if not label or len(label) > 80:
        raise UosaValidationError("Device label must be between 1 and 80 characters.")
    if len(setup_code) < 32 or len(setup_code) > 100:
        raise UosaAuthenticationError("Owner setup code is invalid, expired, or already used.")
    public_key_der = _decode_b64(public_key_b64, "public key")
    public_key = _load_p256_public_key(public_key_der)
    canonical_der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    fingerprint = _fingerprint(canonical_der)
    code_hash = hashlib.sha256(setup_code.encode("ascii")).hexdigest()
    now = _now()
    with _connection() as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        state = connection.execute(
            "SELECT code_hash, expires_at, consumed_at FROM uosa_setup_state "
            "WHERE singleton_id = 1"
        ).fetchone()
        active = connection.execute(
            "SELECT COUNT(*) AS count FROM uosa_devices WHERE revoked_at IS NULL"
        ).fetchone()["count"]
        if (
            state is None
            or state["consumed_at"] is not None
            or state["expires_at"] <= now
            or not secrets.compare_digest(state["code_hash"], code_hash)
            or active
        ):
            raise UosaAuthenticationError(
                "Owner setup code is invalid, expired, or already used."
            )
        key_owner = connection.execute(
            "SELECT device_id FROM uosa_devices WHERE key_fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if key_owner is not None:
            raise UosaConflictError("This device key is already assigned to a device.")
        result = _new_challenge(
            connection,
            purpose="setup",
            device_id=device_id,
            context={
                "label": label,
                "public_key_der": base64.b64encode(canonical_der).decode("ascii"),
                "key_fingerprint": fingerprint,
                "setup_code_hash": code_hash,
            },
            key_fingerprint=fingerprint,
            enrollment_generation=1,
        )
        _event(connection, "setup.enrollment_challenge_issued", device_id, {
            "key_fingerprint": fingerprint,
        })
        return result


def complete_setup_enrollment(
    *, challenge_id: str, signature_b64: str
) -> dict[str, Any]:
    """Consume setup proof, enroll the first owner device and rotate recovery codes."""
    with _connection() as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        challenge = _consume_challenge(connection, challenge_id, "setup")
        connection.commit()
        context = json.loads(challenge["context_json"])
        public_key_der = _decode_b64(context["public_key_der"], "stored public key")
        _verify_signature(public_key_der, challenge["message"], signature_b64)

        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        state = connection.execute(
            "SELECT code_hash, expires_at, consumed_at FROM uosa_setup_state "
            "WHERE singleton_id = 1"
        ).fetchone()
        active = connection.execute(
            "SELECT COUNT(*) AS count FROM uosa_devices WHERE revoked_at IS NULL"
        ).fetchone()["count"]
        key_owner = connection.execute(
            "SELECT device_id FROM uosa_devices WHERE key_fingerprint = ?",
            (context["key_fingerprint"],),
        ).fetchone()
        if (
            state is None
            or state["consumed_at"] is not None
            or state["expires_at"] <= now
            or not secrets.compare_digest(state["code_hash"], context["setup_code_hash"])
            or active
            or key_owner is not None
        ):
            raise UosaAuthenticationError(
                "Owner setup state changed; request a new setup code."
            )
        connection.execute(
            "UPDATE uosa_setup_state SET consumed_at = ? WHERE singleton_id = 1 "
            "AND consumed_at IS NULL",
            (now,),
        )
        connection.execute(
            "INSERT INTO uosa_devices "
            "(device_id, label, public_key_der, key_fingerprint, enrollment_generation, "
            "created_at, revoked_at) VALUES (?, ?, ?, ?, 1, ?, NULL)",
            (
                challenge["device_id"], context["label"], public_key_der,
                context["key_fingerprint"], now,
            ),
        )
        recovery_codes, recovery_generation = _rotate_recovery_codes(connection, now)
        _event(connection, "device.initial_owner_enrolled", challenge["device_id"], {
            "key_fingerprint": context["key_fingerprint"],
            "enrollment_generation": 1,
            "recovery_generation": recovery_generation,
        })
        connection.commit()
    try:
        _setup_code_path().unlink(missing_ok=True)
    except OSError:
        # The stored hash is consumed; an orphan file is harmless and cannot authorize.
        pass
    return {
        "status": "enrolled",
        "device_id": challenge["device_id"],
        "key_fingerprint": context["key_fingerprint"],
        "enrollment_generation": 1,
        "auth_level": "L0",
        "recovery_generation": recovery_generation,
        "recovery_codes": recovery_codes,
    }


def create_enrollment_challenge(
    *,
    token: str,
    device_id: str,
    label: str,
    public_key_b64: str,
) -> dict[str, Any]:
    """Preflight a new-device enrollment, authorized by an existing owner session."""
    session = get_session(token)
    if session is None or session["audience"] != OWNER_AUDIENCE:
        raise UosaAuthenticationError("A valid Core owner session is required.")
    if session["auth_level"] != "L1" or "uosa.device.enroll" not in session["scopes"]:
        raise UosaAuthorizationError(
            "A current L1 session with the device-enrollment scope is required."
        )
    if not device_id or len(device_id) > 80:
        raise UosaValidationError("Invalid device identifier.")
    if not label or len(label) > 80:
        raise UosaValidationError("Device label must be between 1 and 80 characters.")
    public_key_der = _decode_b64(public_key_b64, "public key")
    public_key = _load_p256_public_key(public_key_der)
    canonical_der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    fingerprint = _fingerprint(canonical_der)
    session_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _connection() as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT revoked_at, enrollment_generation FROM uosa_devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if existing is not None and existing["revoked_at"] is None:
            raise UosaConflictError("This device is already enrolled.")
        active = connection.execute(
            "SELECT COUNT(*) AS count FROM uosa_devices WHERE revoked_at IS NULL"
        ).fetchone()["count"]
        if active >= MAX_ACTIVE_DEVICES:
            raise UosaConflictError("The owner device enrollment limit has been reached.")
        key_owner = connection.execute(
            "SELECT device_id FROM uosa_devices WHERE key_fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if key_owner is not None:
            raise UosaConflictError("Enrollment requires a newly generated device key.")
        device_generation = existing["enrollment_generation"] if existing is not None else 0
        current_authority = connection.execute(
            "SELECT 1 FROM uosa_sessions AS s JOIN uosa_devices AS d "
            "ON d.device_id = s.device_id AND d.revoked_at IS NULL "
            "AND d.enrollment_generation = s.device_generation "
            "WHERE s.token_hash = ? AND s.revoked_at IS NULL "
            "AND s.consumed_at IS NULL AND s.expires_at > ?",
            (session_hash, _now()),
        ).fetchone()
        if current_authority is None:
            raise UosaAuthenticationError("The authorizing owner session is no longer active.")
        result = _new_challenge(
            connection,
            purpose="enroll",
            device_id=device_id,
            context={
                "label": label,
                "public_key_der": base64.b64encode(canonical_der).decode("ascii"),
                "key_fingerprint": fingerprint,
                "enrollment_generation": device_generation + 1,
                "authorizing_session_hash": session_hash,
                "authorizing_device_id": session["device_id"],
                "authorizing_device_generation": session["device_generation"],
            },
            key_fingerprint=fingerprint,
            enrollment_generation=device_generation + 1,
        )
        _event(connection, "device.enrollment_challenge_issued", device_id, {
            "key_fingerprint": fingerprint,
            "authorizing_device_id": session["device_id"],
        })
        return result


def complete_enrollment(
    *,
    challenge_id: str,
    signature_b64: str,
    authorized_device_id: str,
) -> dict[str, Any]:
    """Complete an L2-authorized new-device enrollment."""
    with _connection() as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        challenge = _consume_challenge(connection, challenge_id, "enroll")
        connection.commit()
        context = json.loads(challenge["context_json"])
        if context["authorizing_device_id"] != authorized_device_id:
            raise UosaAuthorizationError(
                "The additional device must be enrolled by the device that authorized its challenge."
            )
        public_key_der = _decode_b64(context["public_key_der"], "stored public key")
        _verify_signature(public_key_der, challenge["message"], signature_b64)

        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        authority = connection.execute(
            "SELECT 1 FROM uosa_sessions AS s JOIN uosa_devices AS d "
            "ON d.device_id = s.device_id AND d.revoked_at IS NULL "
            "AND d.enrollment_generation = s.device_generation "
            "WHERE s.token_hash = ? AND s.device_id = ? AND s.revoked_at IS NULL "
            "AND s.consumed_at IS NULL AND s.expires_at > ?",
            (
                context["authorizing_session_hash"],
                context["authorizing_device_id"],
                now,
            ),
        ).fetchone()
        if authority is None:
            raise UosaAuthenticationError(
                "The authorizing owner session expired or was revoked."
            )
        owner_generation = connection.execute(
            "SELECT enrollment_generation FROM uosa_devices "
            "WHERE device_id = ? AND revoked_at IS NULL",
            (context["authorizing_device_id"],),
        ).fetchone()
        if (
            owner_generation is None
            or owner_generation["enrollment_generation"]
            != context["authorizing_device_generation"]
        ):
            raise UosaAuthenticationError("The authorizing owner device has changed.")
        active = connection.execute(
            "SELECT COUNT(*) AS count FROM uosa_devices WHERE revoked_at IS NULL"
        ).fetchone()["count"]
        if active >= MAX_ACTIVE_DEVICES:
            raise UosaConflictError("The owner device enrollment limit has been reached.")
        existing = connection.execute(
            "SELECT revoked_at, enrollment_generation FROM uosa_devices WHERE device_id = ?",
            (challenge["device_id"],),
        ).fetchone()
        current_generation = (
            existing["enrollment_generation"] if existing is not None else 0
        )
        if (
            (existing is not None and existing["revoked_at"] is None)
            or context["enrollment_generation"] != current_generation + 1
        ):
            raise UosaAuthenticationError(
                "The enrollment challenge is stale; request a new challenge."
            )
        key_owner = connection.execute(
            "SELECT device_id FROM uosa_devices WHERE key_fingerprint = ?",
            (context["key_fingerprint"],),
        ).fetchone()
        if key_owner is not None:
            raise UosaConflictError("The new device key is already assigned.")
        connection.execute(
            "INSERT INTO uosa_devices "
            "(device_id, label, public_key_der, key_fingerprint, enrollment_generation, "
            "created_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(device_id) DO UPDATE SET label=excluded.label, "
            "public_key_der=excluded.public_key_der, key_fingerprint=excluded.key_fingerprint, "
            "enrollment_generation=excluded.enrollment_generation, "
            "created_at=excluded.created_at, revoked_at=NULL",
            (
                challenge["device_id"],
                context["label"],
                public_key_der,
                context["key_fingerprint"],
                context["enrollment_generation"],
                now,
            ),
        )
        _event(connection, "device.enrolled", challenge["device_id"], {
            "key_fingerprint": context["key_fingerprint"],
            "authorizing_device_id": context["authorizing_device_id"],
            "enrollment_generation": context["enrollment_generation"],
        })
        return {
            "status": "enrolled",
            "device_id": challenge["device_id"],
            "key_fingerprint": context["key_fingerprint"],
            "enrollment_generation": context["enrollment_generation"],
            "auth_level": "L0",
            "requires_session_issuance": True,
        }


def _consume_challenge(
    connection: sqlite3.Connection, challenge_id: str, purpose: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM uosa_challenges WHERE challenge_id = ? AND purpose = ?",
        (challenge_id, purpose),
    ).fetchone()
    now = _now()
    if row is None or row["consumed_at"] is not None or row["expires_at"] <= now:
        raise UosaAuthenticationError("Challenge is invalid, expired, or already used.")
    updated = connection.execute(
        "UPDATE uosa_challenges SET consumed_at = ? "
        "WHERE challenge_id = ? AND consumed_at IS NULL AND expires_at > ?",
        (now, challenge_id, now),
    )
    if updated.rowcount != 1:
        raise UosaAuthenticationError("Challenge is invalid, expired, or already used.")
    return row


def _verify_signature(public_key_der: bytes, message: str, signature_b64: str) -> None:
    public_key = _load_p256_public_key(public_key_der)
    signature = _decode_b64(signature_b64, "signature")
    try:
        public_key.verify(
            signature,
            message.encode("utf-8"),
            ec.ECDSA(hashes.SHA256()),
        )
    except InvalidSignature as exc:
        raise UosaAuthenticationError("The device signature is invalid.") from exc


def create_recovery_challenge(
    *,
    recovery_code: str,
    device_id: str,
    label: str,
    public_key_b64: str,
    client_key: str,
) -> dict[str, Any]:
    """Issue a new-device proof challenge using one high-entropy recovery code."""
    if not device_id or len(device_id) > 80:
        raise UosaValidationError("Invalid device identifier.")
    if not label or len(label) > 80:
        raise UosaValidationError("Device label must be between 1 and 80 characters.")
    if len(recovery_code) < 32 or len(recovery_code) > 100:
        raise UosaAuthenticationError("Recovery code is invalid, expired, or already used.")
    try:
        code_hash = _recovery_code_hash(recovery_code)
    except UnicodeEncodeError as exc:
        raise UosaAuthenticationError(
            "Recovery code is invalid, expired, or already used."
        ) from exc
    public_key_der = _decode_b64(public_key_b64, "public key")
    public_key = _load_p256_public_key(public_key_der)
    canonical_der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    fingerprint = _fingerprint(canonical_der)
    now = _now()
    with _connection() as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        _check_recovery_rate_limit(connection, client_key, now)
        state = connection.execute(
            "SELECT generation FROM uosa_recovery_state WHERE singleton_id = 1"
        ).fetchone()
        code = connection.execute(
            "SELECT generation FROM uosa_recovery_codes WHERE code_hash = ? "
            "AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > ?",
            (code_hash, now),
        ).fetchone()
        if (
            state is None
            or code is None
            or code["generation"] != state["generation"]
        ):
            raise UosaAuthenticationError(
                "Recovery code is invalid, expired, or already used."
            )
        existing = connection.execute(
            "SELECT enrollment_generation FROM uosa_devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        expected_generation = (
            existing["enrollment_generation"] + 1 if existing is not None else 1
        )
        key_owner = connection.execute(
            "SELECT device_id FROM uosa_devices WHERE key_fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if key_owner is not None:
            raise UosaConflictError("Recovery requires a newly generated device key.")
        result = _new_challenge(
            connection,
            purpose="recover",
            device_id=device_id,
            context={
                "label": label,
                "public_key_der": base64.b64encode(canonical_der).decode("ascii"),
                "key_fingerprint": fingerprint,
                "previous_generation": (
                    existing["enrollment_generation"] if existing is not None else None
                ),
                "enrollment_generation": expected_generation,
                "recovery_code_hash": code_hash,
                "recovery_generation": state["generation"],
            },
            key_fingerprint=fingerprint,
            enrollment_generation=expected_generation,
        )
        _event(connection, "recovery.challenge_issued", device_id, {
            "key_fingerprint": fingerprint,
            "recovery_generation": state["generation"],
        })
        return result


def complete_recovery(
    *, challenge_id: str, signature_b64: str, client_key: str
) -> dict[str, Any]:
    """Consume recovery proof, invalidate lost devices, and rotate recovery codes."""
    now = _now()
    with _connection() as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        _check_recovery_rate_limit(connection, client_key, now)
        challenge = _consume_challenge(connection, challenge_id, "recover")
        connection.commit()
        context = json.loads(challenge["context_json"])
        public_key_der = _decode_b64(context["public_key_der"], "stored public key")
        _verify_signature(public_key_der, challenge["message"], signature_b64)

        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        state = connection.execute(
            "SELECT generation FROM uosa_recovery_state WHERE singleton_id = 1"
        ).fetchone()
        code = connection.execute(
            "SELECT generation FROM uosa_recovery_codes WHERE code_hash = ? "
            "AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > ?",
            (context["recovery_code_hash"], now),
        ).fetchone()
        existing = connection.execute(
            "SELECT enrollment_generation FROM uosa_devices WHERE device_id = ?",
            (challenge["device_id"],),
        ).fetchone()
        current_generation = (
            existing["enrollment_generation"] if existing is not None else None
        )
        if (
            state is None
            or state["generation"] != context["recovery_generation"]
            or code is None
            or code["generation"] != context["recovery_generation"]
            or current_generation != context["previous_generation"]
        ):
            raise UosaAuthenticationError(
                "Recovery state changed; request a new recovery challenge."
            )
        key_owner = connection.execute(
            "SELECT device_id FROM uosa_devices WHERE key_fingerprint = ?",
            (context["key_fingerprint"],),
        ).fetchone()
        if key_owner is not None:
            raise UosaConflictError("Recovery requires a newly generated device key.")

        # Recovery is a global owner-account ceremony: every previous device/session
        # loses authority before the new device and replacement codes become active.
        connection.execute(
            "UPDATE uosa_devices SET revoked_at = ? WHERE revoked_at IS NULL",
            (now,),
        )
        connection.execute(
            "UPDATE uosa_sessions SET revoked_at = ? WHERE revoked_at IS NULL",
            (now,),
        )
        connection.execute(
            "UPDATE uosa_challenges SET consumed_at = ? "
            "WHERE consumed_at IS NULL AND expires_at > ?",
            (now, now),
        )
        connection.execute(
            "UPDATE uosa_recovery_codes SET consumed_at = ? "
            "WHERE code_hash = ? AND consumed_at IS NULL AND revoked_at IS NULL",
            (now, context["recovery_code_hash"]),
        )
        connection.execute(
            "INSERT INTO uosa_devices "
            "(device_id, label, public_key_der, key_fingerprint, enrollment_generation, "
            "created_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(device_id) DO UPDATE SET label=excluded.label, "
            "public_key_der=excluded.public_key_der, key_fingerprint=excluded.key_fingerprint, "
            "enrollment_generation=excluded.enrollment_generation, "
            "created_at=excluded.created_at, revoked_at=NULL",
            (
                challenge["device_id"], context["label"], public_key_der,
                context["key_fingerprint"], context["enrollment_generation"], now,
            ),
        )
        recovery_codes, recovery_generation = _rotate_recovery_codes(connection, now)
        _event(connection, "device.recovered", challenge["device_id"], {
            "key_fingerprint": context["key_fingerprint"],
            "enrollment_generation": context["enrollment_generation"],
            "recovery_generation": recovery_generation,
        })
        return {
            "status": "recovered",
            "device_id": challenge["device_id"],
            "key_fingerprint": context["key_fingerprint"],
            "enrollment_generation": context["enrollment_generation"],
            "recovery_generation": recovery_generation,
            "auth_level": "L0",
            "recovery_codes": recovery_codes,
            "revoked_previous_devices": True,
        }


def create_session_challenge(
    *, device_id: str, audience: str, scopes: list[str], client_key: str = "unknown"
) -> dict[str, Any]:
    if audience != OWNER_AUDIENCE:
        raise UosaAuthorizationError("The requested session audience is not permitted.")
    if not scopes or len(scopes) > len(ALLOWED_SCOPES):
        raise UosaValidationError("At least one valid owner scope is required.")
    normalized = sorted(set(scopes))
    if any(scope not in ALLOWED_SCOPES for scope in normalized):
        raise UosaAuthorizationError("One or more requested owner scopes are not permitted.")
    with _connection() as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        device = connection.execute(
            "SELECT key_fingerprint, enrollment_generation FROM uosa_devices "
            "WHERE device_id = ? AND revoked_at IS NULL",
            (device_id,),
        ).fetchone()
        if device is None:
            raise UosaAuthenticationError("The owner device is not enrolled or has been revoked.")
        now = _now()
        _check_challenge_issue_limit(connection, client_key, now)
        _prune_expired_records(connection, now)
        result = _new_challenge(
            connection,
            purpose="session",
            device_id=device_id,
            context={
                "audience": audience,
                "scopes": normalized,
                "enrollment_generation": device["enrollment_generation"],
                "key_fingerprint": device["key_fingerprint"],
            },
            audience=audience,
            scopes=normalized,
            key_fingerprint=device["key_fingerprint"],
            enrollment_generation=device["enrollment_generation"],
        )
        _event(connection, "session.challenge_issued", device_id, {
            "audience": audience, "scopes": normalized,
        })
        return result


def complete_session(*, challenge_id: str, signature_b64: str) -> dict[str, Any]:
    with _connection() as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        challenge = _consume_challenge(connection, challenge_id, "session")
        connection.commit()  # Consume before verification so invalid attempts cannot replay.
        device = connection.execute(
            "SELECT * FROM uosa_devices WHERE device_id = ? AND revoked_at IS NULL",
            (challenge["device_id"],),
        ).fetchone()
        if device is None:
            raise UosaAuthenticationError("The owner device is not enrolled or has been revoked.")
        context = json.loads(challenge["context_json"])
        if (
            device["enrollment_generation"] != context["enrollment_generation"]
            or device["key_fingerprint"] != context["key_fingerprint"]
        ):
            raise UosaAuthenticationError("The owner device enrollment has changed.")
        _verify_signature(
            bytes(device["public_key_der"]), challenge["message"], signature_b64
        )
        connection.execute("BEGIN IMMEDIATE")
        device = connection.execute(
            "SELECT 1 FROM uosa_devices WHERE device_id = ? AND revoked_at IS NULL "
            "AND enrollment_generation = ? AND key_fingerprint = ?",
            (
                challenge["device_id"], context["enrollment_generation"],
                context["key_fingerprint"],
            ),
        ).fetchone()
        if device is None:
            raise UosaAuthenticationError(
                "The owner device was revoked or re-enrolled during authentication."
            )
        now = _now()
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        expires = now + SESSION_TTL_SECONDS
        connection.execute(
            "INSERT INTO uosa_sessions "
            "(token_hash, device_id, audience, scopes_json, auth_level, created_at, "
            "expires_at, device_generation) VALUES (?, ?, ?, ?, 'L1', ?, ?, ?)",
            (
                token_hash, challenge["device_id"], context["audience"],
                json.dumps(context["scopes"], sort_keys=True), now, expires,
                context["enrollment_generation"],
            ),
        )
        _event(connection, "session.issued", challenge["device_id"], {
            "audience": context["audience"], "scopes": context["scopes"],
            "expires_at": expires,
        })
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_at": expires,
            "expires_in_seconds": SESSION_TTL_SECONDS,
            "audience": context["audience"],
            "scopes": context["scopes"],
            "auth_level": "L1",
        }



def compute_transaction_binding(method: str, path: str, body: bytes) -> str:
    """Return the canonical digest bound to a one-time elevated owner action."""
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UosaValidationError("Elevated owner actions require a valid JSON body.") from exc
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    material = f"{method.upper()}\\n{path}\\n{canonical}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def create_elevation_challenge(
    *, token: str, auth_level: str, scope: str, transaction_hash: str
) -> dict[str, Any]:
    """Issue a fresh-biometric challenge for one exact, pre-authorized L2 action."""
    if auth_level != "L2":
        raise UosaAuthorizationError(
            "L3 elevation is not enabled until a separately federated critical-action policy exists."
        )
    if SCOPE_AUTH_LEVEL.get(scope) != "L2":
        raise UosaAuthorizationError("The requested scope is not eligible for L2 elevation.")
    if (
        len(transaction_hash) != 64
        or any(char not in "0123456789abcdef" for char in transaction_hash.lower())
    ):
        raise UosaValidationError("A SHA-256 transaction binding is required.")
    session = get_session(token)
    if session is None or session["audience"] != OWNER_AUDIENCE:
        raise UosaAuthenticationError("A valid Core owner session is required.")
    if session["auth_level"] != "L1" or scope not in session["scopes"]:
        raise UosaAuthorizationError(
            "A current L1 session with the requested scope is required before elevation."
        )
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _connection() as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        device = connection.execute(
            "SELECT key_fingerprint, enrollment_generation FROM uosa_devices "
            "WHERE device_id = ? AND revoked_at IS NULL",
            (session["device_id"],),
        ).fetchone()
        if device is None:
            raise UosaAuthenticationError("The owner device is not enrolled or has been revoked.")
        result = _new_challenge(
            connection,
            purpose="elevate",
            device_id=session["device_id"],
            context={
                "audience": OWNER_AUDIENCE,
                "auth_level": auth_level,
                "scopes": [scope],
                "transaction_hash": transaction_hash,
                "base_token_hash": token_hash,
                "enrollment_generation": device["enrollment_generation"],
                "key_fingerprint": device["key_fingerprint"],
            },
            audience=OWNER_AUDIENCE,
            scopes=[scope],
            key_fingerprint=device["key_fingerprint"],
            enrollment_generation=device["enrollment_generation"],
            auth_level=auth_level,
            transaction_hash=transaction_hash,
        )
        _event(connection, "session.elevation_challenge_issued", session["device_id"], {
            "auth_level": auth_level,
            "scope": scope,
            "transaction_hash": transaction_hash,
        })
        return result


def complete_elevation(*, challenge_id: str, signature_b64: str) -> dict[str, Any]:
    """Exchange a consumed biometric proof for one short-lived, one-use L2 grant."""
    with _connection() as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        challenge = _consume_challenge(connection, challenge_id, "elevate")
        connection.commit()
        context = json.loads(challenge["context_json"])
        device = connection.execute(
            "SELECT * FROM uosa_devices WHERE device_id = ? AND revoked_at IS NULL",
            (challenge["device_id"],),
        ).fetchone()
        if device is None:
            raise UosaAuthenticationError("The owner device is not enrolled or has been revoked.")
        if (
            device["enrollment_generation"] != context["enrollment_generation"]
            or device["key_fingerprint"] != context["key_fingerprint"]
        ):
            raise UosaAuthenticationError("The owner device enrollment has changed.")
        base = connection.execute(
            "SELECT 1 FROM uosa_sessions WHERE token_hash = ? AND revoked_at IS NULL "
            "AND consumed_at IS NULL AND expires_at > ?",
            (context["base_token_hash"], _now()),
        ).fetchone()
        if base is None:
            raise UosaAuthenticationError("The base owner session is no longer active.")
        _verify_signature(bytes(device["public_key_der"]), challenge["message"], signature_b64)
        connection.execute("BEGIN IMMEDIATE")
        current_device = connection.execute(
            "SELECT 1 FROM uosa_devices WHERE device_id = ? AND revoked_at IS NULL "
            "AND enrollment_generation = ? AND key_fingerprint = ?",
            (
                challenge["device_id"], context["enrollment_generation"],
                context["key_fingerprint"],
            ),
        ).fetchone()
        current_base = connection.execute(
            "SELECT 1 FROM uosa_sessions WHERE token_hash = ? AND revoked_at IS NULL "
            "AND consumed_at IS NULL AND expires_at > ?",
            (context["base_token_hash"], _now()),
        ).fetchone()
        if current_device is None or current_base is None:
            raise UosaAuthenticationError(
                "The device or base session changed during elevation."
            )
        now = _now()
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        expires = now + ELEVATED_SESSION_TTL_SECONDS
        connection.execute(
            "INSERT INTO uosa_sessions "
            "(token_hash, device_id, audience, scopes_json, auth_level, created_at, "
            "expires_at, device_generation, transaction_hash, single_use) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                token_hash, challenge["device_id"], context["audience"],
                json.dumps(context["scopes"], sort_keys=True), context["auth_level"],
                now, expires, context["enrollment_generation"],
                context["transaction_hash"],
            ),
        )
        _event(connection, "session.elevated", challenge["device_id"], {
            "auth_level": context["auth_level"],
            "scopes": context["scopes"],
            "transaction_hash": context["transaction_hash"],
            "expires_at": expires,
        })
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_at": expires,
            "expires_in_seconds": ELEVATED_SESSION_TTL_SECONDS,
            "audience": context["audience"],
            "scopes": context["scopes"],
            "auth_level": context["auth_level"],
            "transaction_hash": context["transaction_hash"],
            "single_use": True,
        }


def consume_elevated_session(token: str, scope: str, transaction_hash: str) -> bool:
    """Atomically consume a one-time elevated grant for its exact protected request."""
    if not token or len(token) > 256:
        return False
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _connection() as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT s.device_id, s.scopes_json, s.auth_level, s.transaction_hash, "
            "s.single_use FROM uosa_sessions AS s "
            "JOIN uosa_devices AS d ON d.device_id = s.device_id "
            "AND d.revoked_at IS NULL "
            "AND d.enrollment_generation = s.device_generation "
            "WHERE s.token_hash = ? AND s.revoked_at IS NULL "
            "AND s.consumed_at IS NULL AND s.expires_at > ?",
            (token_hash, _now()),
        ).fetchone()
        if row is None or row["auth_level"] != "L2" or not row["single_use"]:
            return False
        now = _now()
        updated = connection.execute(
            "UPDATE uosa_sessions SET consumed_at = ? "
            "WHERE token_hash = ? AND consumed_at IS NULL AND revoked_at IS NULL "
            "AND expires_at > ?",
            (now, token_hash, now),
        )
        if updated.rowcount != 1:
            return False

        # Any attempted use consumes the grant, even when the caller presents it
        # to the wrong scope or changes the transaction. This makes failed
        # substitutions non-replayable instead of leaving a live authorization.
        allowed = (
            row["transaction_hash"] == transaction_hash
            and scope in json.loads(row["scopes_json"])
        )
        _event(connection, "session.elevated_consumed", row["device_id"], {
            "scope": scope,
            "transaction_match": row["transaction_hash"] == transaction_hash,
            "scope_match": scope in json.loads(row["scopes_json"]),
            "authorized": allowed,
        })
        return allowed

def get_session(token: str) -> dict[str, Any] | None:
    if not token or len(token) > 256:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _connection() as connection:
        _initialize(connection)
        row = connection.execute(
            "SELECT s.device_id, s.audience, s.scopes_json, s.auth_level, "
            "s.created_at, s.expires_at, s.transaction_hash, s.single_use, "
            "s.consumed_at, s.device_generation FROM uosa_sessions AS s "
            "JOIN uosa_devices AS d ON d.device_id = s.device_id "
            "AND d.revoked_at IS NULL "
            "AND d.enrollment_generation = s.device_generation "
            "WHERE s.token_hash = ? AND s.revoked_at IS NULL "
            "AND s.consumed_at IS NULL AND s.expires_at > ?",
            (token_hash, _now()),
        ).fetchone()
        if row is None:
            return None
        return {
            "device_id": row["device_id"],
            "audience": row["audience"],
            "scopes": json.loads(row["scopes_json"]),
            "auth_level": row["auth_level"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "transaction_hash": row["transaction_hash"],
            "single_use": bool(row["single_use"]),
            "device_generation": row["device_generation"],
        }






def list_security_events(limit: int = 100) -> list[dict[str, Any]]:
    """Return bounded metadata-only UOSA audit records, newest first."""
    if not 1 <= limit <= 200:
        raise UosaValidationError("Audit event limit must be between 1 and 200.")
    with _connection() as connection:
        _initialize(connection)
        rows = connection.execute(
            "SELECT event_id, event_type, device_id, created_at, details_json "
            "FROM uosa_security_events ORDER BY event_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "device_id": row["device_id"],
                "created_at": row["created_at"],
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

def list_devices() -> list[dict[str, Any]]:
    """Return bounded owner-device metadata without exposing public-key material."""
    with _connection() as connection:
        _initialize(connection)
        rows = connection.execute(
            "SELECT device_id, label, key_fingerprint, enrollment_generation, "
            "created_at, revoked_at FROM uosa_devices ORDER BY created_at DESC LIMIT ?",
            (MAX_ACTIVE_DEVICES * 4,),
        ).fetchall()
        return [
            {
                "device_id": row["device_id"],
                "label": row["label"],
                "key_fingerprint": row["key_fingerprint"],
                "enrollment_generation": row["enrollment_generation"],
                "created_at": row["created_at"],
                "revoked_at": row["revoked_at"],
                "active": row["revoked_at"] is None,
            }
            for row in rows
        ]

def revoke_session(token: str) -> bool:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _connection() as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT device_id FROM uosa_sessions WHERE token_hash = ? AND revoked_at IS NULL",
            (token_hash,),
        ).fetchone()
        if row is None:
            return False
        now = _now()
        connection.execute(
            "UPDATE uosa_sessions SET revoked_at = ? "
            "WHERE token_hash = ? AND revoked_at IS NULL",
            (now, token_hash),
        )
        _event(connection, "session.revoked", row["device_id"], {})
        return True






def rotate_recovery_codes() -> dict[str, Any]:
    """Rotate one-time recovery codes after the API's L2 transaction guard passes."""
    now = _now()
    with _connection() as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            "SELECT COUNT(*) AS count FROM uosa_devices WHERE revoked_at IS NULL"
        ).fetchone()["count"]
        if active == 0:
            raise UosaConflictError(
                "Recovery codes can only be rotated while an owner device is active."
            )
        codes, generation = _rotate_recovery_codes(connection, now)
        return {
            "status": "rotated",
            "recovery_generation": generation,
            "expires_at": now + RECOVERY_CODE_TTL_SECONDS,
            "recovery_codes": codes,
            "previous_codes_revoked": True,
        }

def create_key_rotation_challenge(
    *, token: str, device_id: str, replacement_public_key_b64: str
) -> dict[str, Any]:
    """Create a dual-key challenge for rotating the caller's enrolled device key."""
    session = get_session(token)
    if session is None or session["audience"] != OWNER_AUDIENCE:
        raise UosaAuthenticationError("A valid Core owner session is required.")
    if "uosa.device.rotate" not in session["scopes"]:
        raise UosaAuthorizationError("The owner session lacks the key-rotation scope.")
    if session["auth_level"] != "L1":
        raise UosaAuthorizationError("Key-rotation challenge creation requires an L1 session.")
    if device_id != session["device_id"]:
        raise UosaAuthorizationError("A device may rotate only its own enrolled key.")
    replacement_der = _decode_b64(replacement_public_key_b64, "replacement public key")
    replacement_key = _load_p256_public_key(replacement_der)
    canonical_der = replacement_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    replacement_fingerprint = _fingerprint(canonical_der)
    with _connection() as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT label, key_fingerprint, enrollment_generation "
            "FROM uosa_devices WHERE device_id = ? AND revoked_at IS NULL",
            (device_id,),
        ).fetchone()
        if current is None:
            raise UosaAuthenticationError("The owner device is not enrolled or has been revoked.")
        if current["enrollment_generation"] != session["device_generation"]:
            raise UosaAuthenticationError("The owner device enrollment has changed.")
        key_owner = connection.execute(
            "SELECT device_id FROM uosa_devices WHERE key_fingerprint = ?",
            (replacement_fingerprint,),
        ).fetchone()
        if key_owner is not None:
            raise UosaConflictError("The replacement key is already assigned to a device.")
        result = _new_challenge(
            connection,
            purpose="rotate",
            device_id=device_id,
            context={
                "previous_key_fingerprint": current["key_fingerprint"],
                "previous_generation": current["enrollment_generation"],
                "replacement_public_key_der": base64.b64encode(canonical_der).decode("ascii"),
                "replacement_key_fingerprint": replacement_fingerprint,
                "label": current["label"],
            },
            key_fingerprint=current["key_fingerprint"],
            enrollment_generation=current["enrollment_generation"],
            replacement_key_fingerprint=replacement_fingerprint,
        )
        _event(connection, "device.rotation_challenge_issued", device_id, {
            "previous_key_fingerprint": current["key_fingerprint"],
            "replacement_key_fingerprint": replacement_fingerprint,
            "enrollment_generation": current["enrollment_generation"],
        })
        return result


def complete_key_rotation(
    *,
    device_id: str,
    challenge_id: str,
    old_signature_b64: str,
    new_signature_b64: str,
) -> dict[str, Any]:
    """Verify old+new key possession and atomically rotate device authority."""
    with _connection() as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        challenge = _consume_challenge(connection, challenge_id, "rotate")
        if challenge["device_id"] != device_id:
            raise UosaAuthenticationError("The key-rotation challenge belongs to another device.")
        context = json.loads(challenge["context_json"])
        current = connection.execute(
            "SELECT public_key_der, key_fingerprint, enrollment_generation "
            "FROM uosa_devices WHERE device_id = ? AND revoked_at IS NULL",
            (device_id,),
        ).fetchone()
        connection.commit()
        if (
            current is None
            or current["key_fingerprint"] != context["previous_key_fingerprint"]
            or current["enrollment_generation"] != context["previous_generation"]
        ):
            raise UosaAuthenticationError("The device changed before key rotation completed.")
        replacement_der = _decode_b64(
            context["replacement_public_key_der"], "stored replacement public key"
        )
        _verify_signature(bytes(current["public_key_der"]), challenge["message"], old_signature_b64)
        _verify_signature(replacement_der, challenge["message"], new_signature_b64)

        connection.execute("BEGIN IMMEDIATE")
        latest = connection.execute(
            "SELECT key_fingerprint, enrollment_generation FROM uosa_devices "
            "WHERE device_id = ? AND revoked_at IS NULL",
            (device_id,),
        ).fetchone()
        if (
            latest is None
            or latest["key_fingerprint"] != context["previous_key_fingerprint"]
            or latest["enrollment_generation"] != context["previous_generation"]
        ):
            raise UosaAuthenticationError("The device changed while key rotation was in progress.")
        key_owner = connection.execute(
            "SELECT device_id FROM uosa_devices WHERE key_fingerprint = ?",
            (context["replacement_key_fingerprint"],),
        ).fetchone()
        if key_owner is not None:
            raise UosaConflictError("The replacement key is already assigned to a device.")
        now = _now()
        new_generation = context["previous_generation"] + 1
        updated = connection.execute(
            "UPDATE uosa_devices SET public_key_der = ?, key_fingerprint = ?, "
            "enrollment_generation = ?, created_at = ? WHERE device_id = ? "
            "AND revoked_at IS NULL AND enrollment_generation = ?",
            (
                replacement_der,
                context["replacement_key_fingerprint"],
                new_generation,
                now,
                device_id,
                context["previous_generation"],
            ),
        )
        if updated.rowcount != 1:
            raise UosaAuthenticationError("The device changed while key rotation was committing.")
        connection.execute(
            "UPDATE uosa_sessions SET revoked_at = ? "
            "WHERE device_id = ? AND revoked_at IS NULL",
            (now, device_id),
        )
        connection.execute(
            "UPDATE uosa_challenges SET consumed_at = ? "
            "WHERE device_id = ? AND consumed_at IS NULL AND expires_at > ?",
            (now, device_id, now),
        )
        _event(connection, "device.key_rotated", device_id, {
            "previous_key_fingerprint": context["previous_key_fingerprint"],
            "key_fingerprint": context["replacement_key_fingerprint"],
            "previous_generation": context["previous_generation"],
            "enrollment_generation": new_generation,
        })
        return {
            "status": "rotated",
            "device_id": device_id,
            "key_fingerprint": context["replacement_key_fingerprint"],
            "enrollment_generation": new_generation,
            "previous_sessions_revoked": True,
        }


def revoke_device(device_id: str) -> bool:
    with _connection() as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        cursor = connection.execute(
            "UPDATE uosa_devices SET revoked_at = ? "
            "WHERE device_id = ? AND revoked_at IS NULL",
            (now, device_id),
        )
        if cursor.rowcount:
            connection.execute(
                "UPDATE uosa_sessions SET revoked_at = ? "
                "WHERE device_id = ? AND revoked_at IS NULL",
                (now, device_id),
            )
            connection.execute(
                "UPDATE uosa_challenges SET consumed_at = ? "
                "WHERE device_id = ? AND consumed_at IS NULL AND expires_at > ?",
                (now, device_id, now),
            )
            _event(connection, "device.revoked", device_id, {})
        return cursor.rowcount == 1


def parse_bearer(authorization: str | None) -> str | None:
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        return None
    return token
