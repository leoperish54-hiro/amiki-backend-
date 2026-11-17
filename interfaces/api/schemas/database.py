"""Schemas for Amiki data and storage status."""

from pydantic import BaseModel


class PersistentMemoryStatus(BaseModel):
    enabled: bool
    count: int
    categories: int
    status: str


class CurrentSessionStatus(BaseModel):
    enabled: bool
    active: bool
    message_count: int
    max_messages: int
    status: str


class AttachmentStorageStatus(BaseModel):
    available: bool
    persistent: bool
    status: str


class ChatLogStatus(BaseModel):
    available: bool
    persistent: bool
    status: str


class DatabaseStatusResponse(BaseModel):
    overall_status: str
    persistent_memory: PersistentMemoryStatus
    current_session: CurrentSessionStatus
    attachments: AttachmentStorageStatus
    chat_logs: ChatLogStatus
