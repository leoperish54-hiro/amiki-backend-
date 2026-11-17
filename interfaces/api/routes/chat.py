from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from hashlib import sha256
import json
from threading import Lock
from time import monotonic

from core.router import chat, run_pipeline
from core.conversations.store import (
    ConversationNotFoundError,
    get_conversation_store,
    normalize_conversation_id,
)
from core.attachments import Attachment, AttachmentInput, MAX_ATTACHMENT_SIZE_BYTES
from interfaces.api.schemas.chat import ChatRequest, ChatResponse


router = APIRouter()

MAX_ATTACHMENT_COUNT = 5
MAX_TOTAL_ATTACHMENT_SIZE_BYTES = 40 * 1024 * 1024

_IDEMPOTENCY_TTL_SECONDS = 600
_IDEMPOTENCY_MAX_ENTRIES = 1000
_IDEMPOTENCY_LOCK = Lock()
_IDEMPOTENCY_CACHE: dict[str, dict] = {}


def _request_fingerprint(message: str, capability: str | None, attachment_hashes=None, location_context=None, conversation_id=None) -> str:
    payload = {
        "message": message,
        "capability": capability,
        "attachments": attachment_hashes or [],
        "conversation_id": conversation_id,
    }
    if location_context is not None:
        payload["location_context"] = location_context
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()



def _resolve_conversation_id(value: str | None) -> str | None:
    """Validate a conversation ID and require an existing registry record."""
    if value is None:
        return None

    try:
        normalized = normalize_conversation_id(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="Conversation ID must be a canonical UUID v4.",
        ) from None

    try:
        get_conversation_store().get_conversation(normalized)
    except ConversationNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Conversation does not exist.",
        ) from None

    return normalized


def _prune_idempotency_cache(now: float) -> None:
    expired = [
        key for key, entry in _IDEMPOTENCY_CACHE.items()
        if now - entry["created_at"] > _IDEMPOTENCY_TTL_SECONDS
    ]
    for key in expired:
        _IDEMPOTENCY_CACHE.pop(key, None)


def _run_idempotent_chat(request_id: str | None, fingerprint: str, operation):
    """Run one chat request once per client key within a bounded retry window."""
    if request_id is None:
        response = operation()
        _raise_chat_error(response)
        return ChatResponse(response=response)

    request_id = request_id.strip()
    if not request_id or len(request_id) > 128:
        raise HTTPException(status_code=400, detail="Chat request ID is invalid.")

    now = monotonic()
    with _IDEMPOTENCY_LOCK:
        _prune_idempotency_cache(now)
        existing = _IDEMPOTENCY_CACHE.get(request_id)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="This request ID was already used for different chat content.",
                )
            if existing["state"] == "processing":
                raise HTTPException(
                    status_code=409,
                    detail="This chat request is still processing. Do not submit it again.",
                )
            if "error_status" in existing:
                raise HTTPException(
                    status_code=existing["error_status"],
                    detail=existing["error_detail"],
                )
            return ChatResponse(response=existing["response"])

        while len(_IDEMPOTENCY_CACHE) >= _IDEMPOTENCY_MAX_ENTRIES:
            completed = [
                (key, value["created_at"])
                for key, value in _IDEMPOTENCY_CACHE.items()
                if value["state"] == "completed"
            ]
            if not completed:
                raise HTTPException(
                    status_code=503,
                    detail="Amiki is processing too many distinct chat requests. Retry shortly.",
                )
            oldest_key = min(completed, key=lambda item: item[1])[0]
            _IDEMPOTENCY_CACHE.pop(oldest_key, None)

        entry = {
            "fingerprint": fingerprint,
            "state": "processing",
            "created_at": now,
        }
        _IDEMPOTENCY_CACHE[request_id] = entry

    try:
        response = operation()
        _raise_chat_error(response)
    except HTTPException as error:
        with _IDEMPOTENCY_LOCK:
            entry.update({
                "state": "completed",
                "created_at": monotonic(),
                "error_status": error.status_code,
                "error_detail": error.detail,
            })
        raise
    except Exception:
        with _IDEMPOTENCY_LOCK:
            entry.update({
                "state": "completed",
                "created_at": monotonic(),
                "error_status": 500,
                "error_detail": "Amiki encountered an internal error.",
            })
        raise HTTPException(
            status_code=500,
            detail="Amiki encountered an internal error.",
        ) from None

    with _IDEMPOTENCY_LOCK:
        entry.update({
            "state": "completed",
            "created_at": monotonic(),
            "response": response,
        })
    return ChatResponse(response=response)


def _raise_chat_error(response):
    """Map internal model failures to safe HTTP errors without exposing details."""
    if (
        response.startswith("ERROR: Amiki is temporarily rate-limited by the AI service.")
        or response.startswith("ERROR: OpenRouter is rate-limited.")
    ):
        raise HTTPException(
            status_code=429,
            detail="Amiki is temporarily rate-limited by the AI service. Please wait a moment and try again.",
        )

    if response.startswith("ERROR: API request failed: 503"):
        raise HTTPException(
            status_code=503,
            detail="Amiki's AI service is temporarily unavailable. Please try again in a moment.",
        )

    if response == "ERROR: AI provider is not configured.":
        raise HTTPException(
            status_code=503,
            detail="Amiki's AI service is not configured. Please try again later.",
        )

    if response.startswith("ERROR:"):
        raise HTTPException(status_code=500, detail="Amiki encountered an internal error.")


@router.post("/chat", response_model=ChatResponse)
def chat_endpoint(request: ChatRequest):
    if request.attachments:
        raise HTTPException(
            status_code=400,
            detail=(
                "JSON chat does not accept attachment metadata without file "
                "content. Use /api/v1/chat/attachments for file uploads."
            ),
        )

    location_context = (
        request.location_context.model_dump()
        if request.location_context is not None else None
    )
    conversation_id = _resolve_conversation_id(
        str(request.conversation_id) if request.conversation_id is not None else None
    )
    fingerprint = _request_fingerprint(
        request.message,
        request.capability,
        location_context=location_context,
        conversation_id=conversation_id,
    )

    def run_chat():
        if conversation_id is None:
            return (
                chat(request.message, request.capability)
                if location_context is None
                else chat(
                    request.message,
                    request.capability,
                    location_context=location_context,
                )
            )
        return (
            chat(
                request.message,
                request.capability,
                conversation_id=conversation_id,
            )
            if location_context is None
            else chat(
                request.message,
                request.capability,
                location_context=location_context,
                conversation_id=conversation_id,
            )
        )

    return _run_idempotent_chat(
        request.request_id,
        fingerprint,
        run_chat,
    )


@router.post("/chat/attachments", response_model=ChatResponse)
async def chat_attachments_endpoint(
    message: str = Form(...),
    capability: str | None = Form(None),
    request_id: str | None = Form(None),
    conversation_id: str | None = Form(None),
    attachments: list[UploadFile] = File(default=[]),
):
    conversation_id = _resolve_conversation_id(conversation_id)
    if len(attachments) > MAX_ATTACHMENT_COUNT:
        raise HTTPException(
            status_code=413,
            detail=f"At most {MAX_ATTACHMENT_COUNT} attachments are allowed per request.",
        )

    attachment_inputs = []
    attachment_hashes = []
    total_attachment_bytes = 0

    try:
        for upload in attachments:
            # Never materialize an unbounded upload in memory. Read at most one
            # byte beyond the per-file limit so oversized payloads are rejected.
            data = await upload.read(MAX_ATTACHMENT_SIZE_BYTES + 1)
            size_bytes = len(data)
            if size_bytes > MAX_ATTACHMENT_SIZE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="An attachment exceeds the maximum allowed size of 20 MiB.",
                )

            total_attachment_bytes += size_bytes
            if total_attachment_bytes > MAX_TOTAL_ATTACHMENT_SIZE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Attachments exceed the maximum combined size of 40 MiB.",
                )

            attachment = Attachment(
                filename=upload.filename or "",
                mime_type=upload.content_type or "",
                size_bytes=size_bytes,
                source="api",
            )
            attachment_inputs.append(AttachmentInput(attachment=attachment, data=data))
            attachment_hashes.append({
                "filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "sha256": sha256(data).hexdigest(),
                "size_bytes": size_bytes,
            })

        fingerprint = _request_fingerprint(
            message,
            capability,
            attachment_hashes,
            conversation_id=conversation_id,
        )

    except HTTPException:
        raise
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail="The attachment or request could not be processed.",
        ) from None
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Amiki encountered an internal error.",
        ) from None
    finally:
        for upload in attachments:
            try:
                await upload.close()
            except Exception:
                pass

    def run_attachment_chat():
        if conversation_id is None:
            return run_pipeline(
                message,
                capability,
                attachments=attachment_inputs,
            )
        return run_pipeline(
            message,
            capability,
            attachments=attachment_inputs,
            conversation_id=conversation_id,
        )

    return _run_idempotent_chat(
        request_id,
        fingerprint,
        run_attachment_chat,
    )
