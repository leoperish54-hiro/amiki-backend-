"""Typed input boundary for Amiki's conversation pipeline."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.attachments import AttachmentInput
from core.conversations.store import normalize_conversation_id


@dataclass(frozen=True)
class ConversationInput:
    """Validated, immutable input passed into one conversation run."""

    message: str
    capability: str | None
    attachments: tuple[AttachmentInput, ...]
    location_context: dict | None
    conversation_id: str | None = None

    @classmethod
    def create(
        cls,
        message,
        capability=None,
        attachments=None,
        location_context=None,
        conversation_id=None,
    ):
        if not isinstance(message, str):
            raise TypeError("Conversation message must be a string.")

        if capability is not None and not isinstance(capability, str):
            raise TypeError("Conversation capability must be a string or None.")

        if attachments is None:
            normalized_attachments = ()
        else:
            if (
                not isinstance(attachments, Sequence)
                or isinstance(attachments, (str, bytes, bytearray))
            ):
                raise TypeError("Conversation attachments must be a sequence.")

            if any(
                not isinstance(item, AttachmentInput)
                for item in attachments
            ):
                raise TypeError(
                    "Conversation attachments must contain AttachmentInput objects."
                )

            normalized_attachments = tuple(attachments)

        if location_context is None:
            normalized_location = None
        elif isinstance(location_context, Mapping):
            normalized_location = dict(location_context)
        else:
            raise TypeError(
                "Conversation location context must be a mapping or None."
            )

        if conversation_id is None:
            normalized_conversation_id = None
        else:
            if not isinstance(conversation_id, str):
                raise TypeError("Conversation ID must be a string or None.")
            normalized_conversation_id = normalize_conversation_id(
                conversation_id
            )

        return cls(
            message=message,
            capability=capability,
            attachments=normalized_attachments,
            location_context=normalized_location,
            conversation_id=normalized_conversation_id,
        )
