"""Transactional, temporal persistent memory backed by SQLite.

The legacy JSON file is imported once and retained untouched as a recovery copy.
SQLite is the source of truth after migration.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

with open(CONFIG_FILE, "r", encoding="utf-8") as file:
    CONFIG = json.load(file)

MEMORY_CONFIG = CONFIG.get("memory", {})
LONG_TERM_FILE = os.path.join(
    BASE_DIR,
    MEMORY_CONFIG.get("long_term_file", "memory/persistent_memory.json"),
)
MEMORY_ENABLED = MEMORY_CONFIG.get("enabled", True)
SQLITE_FILE = os.path.join(
    BASE_DIR,
    MEMORY_CONFIG.get("database_file", "memory/persistent_memory.sqlite3"),
)
_BUSY_TIMEOUT_MS = int(MEMORY_CONFIG.get("busy_timeout_ms", 10000))
_MEMORY_LOCK = RLock()


class MemoryStoreError(RuntimeError):
    """Raised when persistent memory cannot be safely read or changed."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _database_path():
    # Keep the path patchable for isolated tests and alternate installations.
    configured = MEMORY_CONFIG.get("database_file")
    if configured:
        return os.path.join(BASE_DIR, configured)
    if SQLITE_FILE != os.path.join(BASE_DIR, "memory/persistent_memory.sqlite3"):
        return SQLITE_FILE
    legacy = Path(LONG_TERM_FILE)
    return str(legacy.with_suffix(".sqlite3"))


def _validate_fact_fields(category, key, value):
    for field, item in (("category", category), ("key", key), ("value", value)):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Memory {field} must be a non-empty string.")


def _validate_memory(document):
    if not isinstance(document, dict) or not isinstance(document.get("facts"), list):
        raise MemoryStoreError("Persistent memory has an invalid document structure.")
    for index, fact in enumerate(document["facts"]):
        if not isinstance(fact, dict):
            raise MemoryStoreError(f"Invalid memory fact at position {index}.")
        for field in ("category", "key", "value"):
            if not isinstance(fact.get(field), str) or not fact[field].strip():
                raise MemoryStoreError(
                    f"Invalid memory {field} at position {index}."
                )
    return document


def _connect():
    path = _database_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    try:
        connection = sqlite3.connect(
            path,
            timeout=max(_BUSY_TIMEOUT_MS, 1) / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {max(_BUSY_TIMEOUT_MS, 1)}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _initialize_schema(connection)
        return connection
    except (sqlite3.Error, OSError) as exc:
        raise MemoryStoreError("Persistent memory database is unavailable.") from exc


def _initialize_schema(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS memory_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            fact_key TEXT NOT NULL,
            value TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','superseded','forgotten')),
            confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
            provenance_json TEXT NOT NULL DEFAULT '{}',
            source_text TEXT,
            source_type TEXT NOT NULL DEFAULT 'unknown',
            extraction_method TEXT NOT NULL DEFAULT 'unknown',
            embedding_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            superseded_by INTEGER REFERENCES memory_facts(id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS one_active_fact_per_key
        ON memory_facts(category, fact_key) WHERE status = 'active';

        CREATE INDEX IF NOT EXISTS memory_fact_history
        ON memory_facts(category, fact_key, created_at);

        CREATE TABLE IF NOT EXISTS memory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact_id INTEGER REFERENCES memory_facts(id),
            event_type TEXT NOT NULL,
            category TEXT NOT NULL,
            fact_key TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            provenance_json TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS memory_events_by_fact
        ON memory_events(category, fact_key, occurred_at);
        """
    )


@contextmanager
def _transaction(connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _json_object(value, fallback=None):
    if value is None:
        return {} if fallback is None else fallback
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else ({} if fallback is None else fallback)
    except (TypeError, json.JSONDecodeError):
        return {} if fallback is None else fallback


def _row_to_fact(row):
    fact = {
        "category": row["category"],
        "key": row["fact_key"],
        "value": row["value"],
    }
    provenance = _json_object(row["provenance_json"])
    if provenance:
        fact["provenance"] = provenance
    fact["confidence"] = float(row["confidence"])
    fact["source_type"] = row["source_type"]
    fact["extraction_method"] = row["extraction_method"]
    fact["created_at"] = row["created_at"]
    fact["updated_at"] = row["updated_at"]
    fact["valid_from"] = row["valid_from"]
    fact["valid_to"] = row["valid_to"]
    if row["status"] != "active":
        fact["status"] = row["status"]
    if row["superseded_by"] is not None:
        fact["superseded_by"] = row["superseded_by"]
    if row["embedding_json"]:
        try:
            fact["_embedding"] = json.loads(row["embedding_json"])
        except (TypeError, json.JSONDecodeError):
            pass
    return fact


def _legacy_document():
    path = Path(LONG_TERM_FILE)
    if not path.exists():
        return {"facts": []}
    try:
        with path.open("r", encoding="utf-8") as file:
            document = json.load(file)
    except FileNotFoundError:
        return {"facts": []}
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise MemoryStoreError(
            "Legacy persistent memory could not be read safely; it was not changed."
        ) from None
    return _validate_memory(document)


def _migrate_legacy_if_needed(connection):
    migrated = connection.execute(
        "SELECT value FROM memory_meta WHERE key = 'legacy_json_imported'"
    ).fetchone()
    if migrated:
        return

    document = _legacy_document()
    now = _now()
    metadata = {key: value for key, value in document.items() if key != "facts"}
    with _transaction(connection):
        for fact in document["facts"]:
            _validate_fact_fields(fact.get("category"), fact.get("key"), fact.get("value"))
            extra = {
                key: value for key, value in fact.items()
                if key not in {"category", "key", "value"}
            }
            provenance = extra.pop("provenance", {})
            confidence = extra.pop("confidence", 0.75)
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                confidence = 0.75
            confidence = min(max(float(confidence), 0.0), 1.0)
            if extra:
                provenance = dict(provenance) if isinstance(provenance, dict) else {}
                provenance["legacy_metadata"] = extra
            connection.execute(
                """
                INSERT INTO memory_facts
                (category,fact_key,value,status,confidence,provenance_json,
                 source_type,extraction_method,created_at,updated_at,valid_from)
                VALUES (?,?,?,'active',?,?,?,?,?,?,?)
                """,
                (
                    fact["category"], fact["key"], fact["value"], confidence,
                    json.dumps(provenance, ensure_ascii=False),
                    "legacy_import", "legacy_json", now, now, now,
                ),
            )
            fact_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            connection.execute(
                """INSERT INTO memory_events
                (fact_id,event_type,category,fact_key,new_value,provenance_json,occurred_at)
                VALUES (?,'legacy_import',?,?,?,?,?)""",
                (
                    fact_id, fact["category"], fact["key"], fact["value"],
                    json.dumps(provenance, ensure_ascii=False), now,
                ),
            )
        connection.execute(
            "INSERT OR REPLACE INTO memory_meta(key,value) VALUES('legacy_document_metadata',?)",
            (json.dumps(metadata, ensure_ascii=False),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO memory_meta(key,value) VALUES('legacy_json_imported','true')"
        )


def _open_store():
    connection = _connect()
    try:
        _migrate_legacy_if_needed(connection)
        return connection
    except Exception:
        connection.close()
        raise


def _embed_text(text):
    try:
        from core.memory_embeddings import embed_text
        return embed_text(text)
    except Exception:
        # Embeddings are an optional retrieval enhancement. Their failure must
        # never prevent a fact from being durably recorded.
        return None


def add_memory(
    category,
    key,
    value,
    *,
    provenance=None,
    confidence=0.9,
    source_text=None,
    source_type="explicit_user_statement",
    extraction_method="pattern",
    correlation_id=None,
):
    if not MEMORY_ENABLED:
        return False
    _validate_fact_fields(category, key, value)
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ValueError("Memory confidence must be a number between 0 and 1.")
    if not 0.0 <= float(confidence) <= 1.0:
        raise ValueError("Memory confidence must be between 0 and 1.")
    provenance = dict(provenance or {})
    if correlation_id:
        provenance.setdefault("correlation_id", correlation_id)
    if source_text:
        provenance.setdefault("source_text", source_text)
    provenance.setdefault("captured_at", _now())
    embedding = _embed_text(f"{category}: {key}. {value}")
    now = _now()
    connection = _open_store()
    try:
        with _transaction(connection):
            current = connection.execute(
                """SELECT * FROM memory_facts
                WHERE category=? AND fact_key=? AND status='active'""",
                (category, key),
            ).fetchone()
            if current and current["value"] == value:
                old_provenance = _json_object(current["provenance_json"])
                evidence = old_provenance.setdefault("evidence", [])
                evidence.append(provenance)
                connection.execute(
                    """UPDATE memory_facts SET confidence=?, provenance_json=?,
                    updated_at=?, embedding_json=COALESCE(?,embedding_json)
                    WHERE id=?""",
                    (
                        max(float(current["confidence"]), float(confidence)),
                        json.dumps(old_provenance, ensure_ascii=False),
                        now,
                        json.dumps(embedding) if embedding is not None else None,
                        current["id"],
                    ),
                )
                connection.execute(
                    """INSERT INTO memory_events
                    (fact_id,event_type,category,fact_key,old_value,new_value,provenance_json,occurred_at)
                    VALUES (?,'reinforced',?,?,?,?,?,?)""",
                    (
                        current["id"], category, key, value, value,
                        json.dumps(provenance, ensure_ascii=False), now,
                    ),
                )
                return True

            old_id = current["id"] if current else None
            old_value = current["value"] if current else None
            if current:
                connection.execute(
                    """UPDATE memory_facts SET status='superseded', valid_to=?,
                    updated_at=? WHERE id=?""",
                    (now, now, current["id"]),
                )

            connection.execute(
                """
                INSERT INTO memory_facts
                (category,fact_key,value,status,confidence,provenance_json,
                 source_text,source_type,extraction_method,embedding_json,
                 created_at,updated_at,valid_from)
                VALUES (?,?,?,'active',?,?,?,?,?,?,?,?,?)
                """,
                (
                    category, key, value, float(confidence),
                    json.dumps(provenance, ensure_ascii=False),
                    source_text, source_type, extraction_method,
                    json.dumps(embedding) if embedding is not None else None,
                    now, now, now,
                ),
            )
            new_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            if old_id is not None:
                connection.execute(
                    "UPDATE memory_facts SET superseded_by=? WHERE id=?",
                    (new_id, old_id),
                )
            connection.execute(
                """INSERT INTO memory_events
                (fact_id,event_type,category,fact_key,old_value,new_value,provenance_json,occurred_at)
                VALUES (? ,?,?,?,?,?,?,?)""",
                (
                    new_id, "superseded" if old_id else "created", category, key,
                    old_value, value, json.dumps(provenance, ensure_ascii=False), now,
                ),
            )
        return True
    except sqlite3.Error as exc:
        raise MemoryStoreError("Persistent memory could not be updated safely.") from exc
    finally:
        connection.close()


def get_memory(category=None, key=None, *, include_history=False):
    if not MEMORY_ENABLED:
        return []
    connection = _open_store()
    try:
        query = "SELECT * FROM memory_facts"
        clauses = []
        params = []
        if not include_history:
            clauses.append("status='active'")
        if category is not None:
            clauses.append("category=?")
            params.append(category)
        if key is not None:
            clauses.append("fact_key=?")
            params.append(key)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, id"
        rows = connection.execute(query, params).fetchall()
        return [_row_to_fact(row) for row in rows]
    except sqlite3.Error as exc:
        raise MemoryStoreError("Persistent memory could not be read safely.") from exc
    finally:
        connection.close()


def get_memory_history(category=None, key=None, limit=100):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("History limit must be between 1 and 1000.")
    if not MEMORY_ENABLED:
        return []
    connection = _open_store()
    try:
        query = "SELECT * FROM memory_facts"
        clauses = []
        params = []
        if category is not None:
            clauses.append("category=?")
            params.append(category)
        if key is not None:
            clauses.append("fact_key=?")
            params.append(key)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [_row_to_fact(row) for row in connection.execute(query, params).fetchall()]
    except sqlite3.Error as exc:
        raise MemoryStoreError("Memory history could not be read safely.") from exc
    finally:
        connection.close()


def get_memory_events(category=None, key=None, limit=100):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("Event limit must be between 1 and 1000.")
    connection = _open_store()
    try:
        query = "SELECT * FROM memory_events"
        clauses = []
        params = []
        if category is not None:
            clauses.append("category=?")
            params.append(category)
        if key is not None:
            clauses.append("fact_key=?")
            params.append(key)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in connection.execute(query, params).fetchall()]
    except sqlite3.Error as exc:
        raise MemoryStoreError("Memory event history could not be read safely.") from exc
    finally:
        connection.close()


def delete_memory(category, key):
    if not MEMORY_ENABLED:
        return False
    _validate_fact_fields(category, key, "forget-operation")
    now = _now()
    connection = _open_store()
    try:
        with _transaction(connection):
            rows = connection.execute(
                "SELECT * FROM memory_facts WHERE category=? AND fact_key=? AND status='active'",
                (category, key),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE memory_facts SET status='forgotten', valid_to=?, updated_at=? WHERE id=?",
                    (now, now, row["id"]),
                )
                connection.execute(
                    """INSERT INTO memory_events
                    (fact_id,event_type,category,fact_key,old_value,provenance_json,occurred_at)
                    VALUES (?,'forgotten',?,?,?, ?,?)""",
                    (
                        row["id"], category, key, row["value"],
                        row["provenance_json"], now,
                    ),
                )
        return True
    except sqlite3.Error as exc:
        raise MemoryStoreError("Persistent memory could not be changed safely.") from exc
    finally:
        connection.close()


def clear_memory():
    if not MEMORY_ENABLED:
        return
    now = _now()
    connection = _open_store()
    try:
        with _transaction(connection):
            rows = connection.execute(
                "SELECT * FROM memory_facts WHERE status='active'"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE memory_facts SET status='forgotten', valid_to=?, updated_at=? WHERE id=?",
                    (now, now, row["id"]),
                )
                connection.execute(
                    """INSERT INTO memory_events
                    (fact_id,event_type,category,fact_key,old_value,provenance_json,occurred_at)
                    VALUES (?,'forgotten',?,?,?, ?,?)""",
                    (
                        row["id"], row["category"], row["fact_key"], row["value"],
                        row["provenance_json"], now,
                    ),
                )
    except sqlite3.Error as exc:
        raise MemoryStoreError("Persistent memory could not be cleared safely.") from exc
    finally:
        connection.close()


def load_memory():
    if not MEMORY_ENABLED:
        return {"facts": []}
    connection = _open_store()
    try:
        facts = get_memory()
        metadata_row = connection.execute(
            "SELECT value FROM memory_meta WHERE key='legacy_document_metadata'"
        ).fetchone()
        metadata = _json_object(metadata_row["value"]) if metadata_row else {}
        return {**metadata, "facts": facts}
    except sqlite3.Error as exc:
        raise MemoryStoreError("Persistent memory could not be read safely.") from exc
    finally:
        connection.close()


def save_memory(document):
    if not MEMORY_ENABLED:
        return
    _validate_memory(document)
    connection = _open_store()
    now = _now()
    try:
        with _transaction(connection):
            rows = connection.execute(
                "SELECT * FROM memory_facts WHERE status='active'"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE memory_facts SET status='superseded', valid_to=?, updated_at=? WHERE id=?",
                    (now, now, row["id"]),
                )
            for fact in document["facts"]:
                _validate_fact_fields(fact["category"], fact["key"], fact["value"])
                provenance = fact.get("provenance", {})
                confidence = fact.get("confidence", 0.75)
                if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                    confidence = 0.75
                connection.execute(
                    """INSERT INTO memory_facts
                    (category,fact_key,value,status,confidence,provenance_json,
                     source_type,extraction_method,created_at,updated_at,valid_from)
                    VALUES (?,?,?,'active',?,?,?,?,?,?,?)""",
                    (
                        fact["category"], fact["key"], fact["value"],
                        min(max(float(confidence), 0.0), 1.0),
                        json.dumps(provenance, ensure_ascii=False),
                        fact.get("source_type", "imported"),
                        fact.get("extraction_method", "save_memory"),
                        now, now, now,
                    ),
                )
    except sqlite3.Error as exc:
        raise MemoryStoreError("Persistent memory could not be saved safely.") from exc
    finally:
        connection.close()
