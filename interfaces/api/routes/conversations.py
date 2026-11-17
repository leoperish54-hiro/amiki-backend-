"""Conversation lifecycle API backed by the isolated conversation store."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from interfaces.api.routes.security import require_uosa_scope
from core.conversations.store import (
    ConversationAlreadyExistsError,
    ConversationNotFoundError,
    get_conversation_store,
    normalize_conversation_id,
)


router = APIRouter()


class ConversationCreateRequest(BaseModel):
    """Create metadata for an Android-owned stable conversation identity."""

    conversation_id: str
    title: str | None = Field(default=None, max_length=80)

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, value):
        try:
            return normalize_conversation_id(value)
        except (TypeError, ValueError) as error:
            raise ValueError("Conversation ID must be a canonical UUID v4.") from error


@router.post("/conversations", status_code=201, dependencies=[Depends(require_uosa_scope("conversations.write"))])
def create_conversation(request: ConversationCreateRequest):
    """Register a client-generated UUID v4 conversation."""
    try:
        return get_conversation_store().create_conversation(
            str(request.conversation_id),
            request.title,
        )
    except ConversationAlreadyExistsError:
        raise HTTPException(
            status_code=409,
            detail="Conversation ID is already registered.",
        ) from None


@router.get("/conversations", dependencies=[Depends(require_uosa_scope("conversations.read"))])
def list_conversations(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Return conversation metadata ordered by most recent activity."""
    return {
        "conversations": get_conversation_store().list_conversations(
            limit=limit,
            offset=offset,
        ),
        "limit": limit,
        "offset": offset,
    }


def _normalize_path_conversation_id(value: str) -> str:
    try:
        return normalize_conversation_id(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="Conversation ID must be a canonical UUID v4.",
        ) from None


@router.get("/conversations/{conversation_id}", dependencies=[Depends(require_uosa_scope("conversations.read"))])
def get_conversation(conversation_id: str):
    """Return one conversation's metadata."""
    try:
        return get_conversation_store().get_conversation(
            _normalize_path_conversation_id(conversation_id)
        )
    except ConversationNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Conversation does not exist.",
        ) from None


@router.get("/conversations/{conversation_id}/messages", dependencies=[Depends(require_uosa_scope("conversations.read"))])
def get_conversation_messages(
    conversation_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    before_sequence: int | None = Query(default=None, ge=1),
):
    """Return a bounded chronological transcript page; cursor moves backwards."""
    store = get_conversation_store()
    identifier = _normalize_path_conversation_id(conversation_id)
    try:
        messages = store.get_messages(
            identifier,
            limit=limit + 1,
            before_sequence=before_sequence,
        )
    except ConversationNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Conversation does not exist.",
        ) from None

    has_more = len(messages) > limit
    if has_more:
        messages = messages[1:]

    return {
        "conversation_id": identifier,
        "messages": messages,
        "has_more": has_more,
        "next_before_sequence": (
            messages[0]["sequence"] if has_more and messages else None
        ),
        "limit": limit,
    }
