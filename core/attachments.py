"""Attachment contracts and validation for Amiki."""

from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4


MAX_ATTACHMENT_SIZE_BYTES = 20 * 1024 * 1024


class AttachmentStatus(str, Enum):
    SELECTED = "selected"
    VALIDATED = "validated"
    REJECTED = "rejected"
    READY = "ready"
    FAILED = "failed"


ALLOWED_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/plain",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@dataclass(frozen=True)
class AttachmentInput:
    """Pipeline input containing attachment metadata and its raw bytes."""

    attachment: "Attachment"
    data: bytes


@dataclass
class Attachment:
    filename: str
    mime_type: str
    size_bytes: int
    source: str = "unknown"
    id: str = field(default_factory=lambda: str(uuid4()))
    status: AttachmentStatus = AttachmentStatus.SELECTED

    def validate(self) -> "Attachment":
        """Validate attachment metadata and update its status."""

        if not self.filename.strip():
            self.status = AttachmentStatus.REJECTED
            raise ValueError("Attachment filename cannot be empty.")

        if self.size_bytes < 0:
            self.status = AttachmentStatus.REJECTED
            raise ValueError("Attachment size cannot be negative.")

        if self.size_bytes > MAX_ATTACHMENT_SIZE_BYTES:
            self.status = AttachmentStatus.REJECTED
            raise ValueError(
                "Attachment exceeds the maximum allowed size of 20 MiB."
            )

        if self.mime_type not in ALLOWED_MIME_TYPES:
            self.status = AttachmentStatus.REJECTED
            raise ValueError(
                f"Unsupported attachment MIME type: {self.mime_type}"
            )

        self.status = AttachmentStatus.VALIDATED
        return self
