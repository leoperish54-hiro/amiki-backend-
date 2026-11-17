"""Durable, isolated conversation metadata and transcript storage.

The store intentionally does not import or migrate the legacy global
memory/current_session.json transcript. Legacy compatibility is handled at the
chat boundary; new conversation IDs always resolve to isolated records here.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
import sqlite3
import uuid
from threading import RLock
from typing import Iterator


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "database", "conversations.sqlite3")

DEFAULT_CONVERSATION_TITLE = "New Chat"
MAX_TITLE_CHARS = 80
MAX_MESSAGE_CHARS = 100_000
MAX_MESSAGES_PER_CONVERSATION = 10_000
MAX_CONVERSATION_PAGE_SIZE = 100
DEFAULT_CONVERSATION_PAGE_SIZE = 50


class ConversationStoreError(RuntimeError):
    """Base class for conversation persistence failures."""


class ConversationNotFoundError(ConversationStoreError):
    """Raised when an operation references an unknown conversation."""


class ConversationAlreadyExistsError(ConversationStoreError):
    """Raised when a conversation ID is already registered."""


class ConversationLimitExceededError(ConversationStoreError):
    """Raised when a bounded store limit would be exceeded."""


def normalize_conversation_id(value: str) -> str:
    """Validate and normalize a canonical RFC 4122 UUID v4 conversation ID."""
    if not isinstance(value, str):
        raise ValueError("Conversation ID must be a UUID v4 string.")

    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("Conversation ID must be a UUID v4 string.") from error

    if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        raise ValueError("Conversation ID must be a UUID v4 string.")

    normalized = str(parsed)
    if value.lower() != normalized:
        raise ValueError("Conversation ID must use canonical UUID formatting.")

    return normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _normalize_title(value: str | None) -> str:
    if value is None:
        return DEFAULT_CONVERSATION_TITLE
    if not isinstance(value, str):
        raise TypeError("Conversation title must be a string or None.")

    normalized = " ".join(value.split())
    if not normalized:
        return DEFAULT_CONVERSATION_TITLE
    return normalized[:MAX_TITLE_CHARS]


def _title_from_message(message: str) -> str:
    normalized = " ".join(message.split())
    if len(normalized) > MAX_TITLE_CHARS:
        return normalized[: MAX_TITLE_CHARS - 1].rstrip() + "…"
    return normalized or DEFAULT_CONVERSATION_TITLE


class ConversationStore:
    """SQLite-backed conversation registry with isolated ordered transcripts.

    Each write uses a short BEGIN IMMEDIATE transaction. SQLite serializes
    competing writers across threads and processes; WAL mode supports concurrent
    readers without mixing histories. A corrupt database raises an error and is
    never silently recreated or replaced.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        if not isinstance(db_path, (str, os.PathLike)) or not os.fspath(db_path):
            raise ValueError("Conversation database path must be a non-empty path.")

        self.db_path = os.path.abspath(os.fspath(db_path))
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        connection = sqlite3.connect(
            self.db_path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _ensure_schema(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'archived')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0
                        CHECK (message_count >= 0)
                );

                CREATE TABLE IF NOT EXISTS conversation_messages (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK (sequence > 0),
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    text TEXT NOT NULL CHECK (length(text) > 0),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (conversation_id)
                        REFERENCES conversations(conversation_id)
                        ON DELETE CASCADE,
                    UNIQUE (conversation_id, sequence)
                );

                CREATE INDEX IF NOT EXISTS idx_conversations_updated
                    ON conversations(updated_at DESC, conversation_id);

                CREATE INDEX IF NOT EXISTS idx_conversation_messages_order
                    ON conversation_messages(conversation_id, sequence);
                """
            )

    @staticmethod
    def _conversation_from_row(row: sqlite3.Row) -> dict:
        return {
            "conversation_id": row["conversation_id"],
            "title": row["title"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "message_count": int(row["message_count"]),
        }

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> dict:
        return {
            "message_id": row["message_id"],
            "conversation_id": row["conversation_id"],
            "sequence": int(row["sequence"]),
            "role": row["role"],
            "text": row["text"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _require_conversation(connection: sqlite3.Connection, conversation_id: str):
        row = connection.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise ConversationNotFoundError("Conversation does not exist.")
        return row

    def create_conversation(
        self,
        conversation_id: str,
        title: str | None = None,
    ) -> dict:
        """Create an empty conversation using the caller's stable UUID v4."""
        normalized_id = normalize_conversation_id(conversation_id)
        normalized_title = _normalize_title(title)
        timestamp = _utc_now()

        with self._write() as connection:
            exists = connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?",
                (normalized_id,),
            ).fetchone()
            if exists is not None:
                raise ConversationAlreadyExistsError(
                    "Conversation ID is already registered."
                )

            connection.execute(
                """
                INSERT INTO conversations (
                    conversation_id, title, status, created_at, updated_at, message_count
                ) VALUES (?, ?, 'active', ?, ?, 0)
                """,
                (normalized_id, normalized_title, timestamp, timestamp),
            )
            row = self._require_conversation(connection, normalized_id)

        return self._conversation_from_row(row)

    def list_conversations(
        self,
        limit: int = DEFAULT_CONVERSATION_PAGE_SIZE,
        offset: int = 0,
    ) -> list[dict]:
        """List conversation metadata, newest activity first, with bounded paging."""
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > MAX_CONVERSATION_PAGE_SIZE
        ):
            raise ValueError("Conversation page size is outside the allowed range.")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("Conversation page offset must be a non-negative integer.")

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM conversations
                ORDER BY updated_at DESC, conversation_id ASC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()

        return [self._conversation_from_row(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> dict:
        """Return metadata for one conversation or raise a not-found error."""
        normalized_id = normalize_conversation_id(conversation_id)
        with self._connection() as connection:
            row = self._require_conversation(connection, normalized_id)
        return self._conversation_from_row(row)

    def append_message(
        self,
        conversation_id: str,
        role: str,
        text: str,
    ) -> dict:
        """Append one message without trimming older transcript records."""
        normalized_id = normalize_conversation_id(conversation_id)

        if not isinstance(role, str) or role not in {"user", "assistant"}:
            raise ValueError("Conversation message role must be user or assistant.")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Conversation message text must be non-empty.")
        if len(text) > MAX_MESSAGE_CHARS:
            raise ConversationLimitExceededError(
                "Conversation message exceeds the maximum allowed length."
            )

        timestamp = _utc_now()
        message_id = str(uuid.uuid4())

        with self._write() as connection:
            conversation = self._require_conversation(connection, normalized_id)
            current_count = int(conversation["message_count"])
            if current_count >= MAX_MESSAGES_PER_CONVERSATION:
                raise ConversationLimitExceededError(
                    "Conversation reached its maximum transcript length."
                )

            sequence = current_count + 1
            connection.execute(
                """
                INSERT INTO conversation_messages (
                    message_id, conversation_id, sequence, role, text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (message_id, normalized_id, sequence, role, text, timestamp),
            )

            title = conversation["title"]
            if role == "user" and current_count == 0 and title == DEFAULT_CONVERSATION_TITLE:
                title = _title_from_message(text)

            connection.execute(
                """
                UPDATE conversations
                SET title = ?, updated_at = ?, message_count = ?
                WHERE conversation_id = ?
                """,
                (title, timestamp, sequence, normalized_id),
            )

            row = connection.execute(
                """
                SELECT * FROM conversation_messages
                WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()

        return self._message_from_row(row)

    def get_messages(
        self,
        conversation_id: str,
        limit: int | None = None,
        before_sequence: int | None = None,
    ) -> list[dict]:
        """Return chronological transcript messages, optionally paging backwards."""
        normalized_id = normalize_conversation_id(conversation_id)

        if limit is not None and (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > MAX_MESSAGES_PER_CONVERSATION
        ):
            raise ValueError("Transcript page size is outside the allowed range.")
        if before_sequence is not None and (
            not isinstance(before_sequence, int)
            or isinstance(before_sequence, bool)
            or before_sequence < 1
        ):
            raise ValueError("Transcript cursor must be a positive sequence number.")
        if before_sequence is not None and limit is None:
            raise ValueError("Transcript paging requires an explicit page size.")

        with self._connection() as connection:
            self._require_conversation(connection, normalized_id)
            if limit is None:
                rows = connection.execute(
                    """
                    SELECT * FROM conversation_messages
                    WHERE conversation_id = ?
                    ORDER BY sequence ASC
                    """,
                    (normalized_id,),
                ).fetchall()
            elif before_sequence is None:
                rows = connection.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM conversation_messages
                        WHERE conversation_id = ?
                        ORDER BY sequence DESC
                        LIMIT ?
                    ) ORDER BY sequence ASC
                    """,
                    (normalized_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM conversation_messages
                        WHERE conversation_id = ? AND sequence < ?
                        ORDER BY sequence DESC
                        LIMIT ?
                    ) ORDER BY sequence ASC
                    """,
                    (normalized_id, before_sequence, limit),
                ).fetchall()

        return [self._message_from_row(row) for row in rows]

_STORE_LOCK = RLock()
_DEFAULT_STORE: ConversationStore | None = None


def get_conversation_store() -> ConversationStore:
    """Return the process-wide store for the configured default database."""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        with _STORE_LOCK:
            if _DEFAULT_STORE is None:
                _DEFAULT_STORE = ConversationStore()
    return _DEFAULT_STORE
