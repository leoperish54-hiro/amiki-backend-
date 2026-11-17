"""Safe attachment ingestion and content-type verification for Amiki."""

from dataclasses import dataclass

from core.attachments import Attachment, AttachmentStatus


@dataclass(frozen=True)
class IngestedAttachment:
    attachment: Attachment
    detected_type: str
    content: bytes


def detect_content_type(data: bytes) -> str:
    """Detect supported attachment types from file signatures/content."""

    if not data:
        raise ValueError("Attachment content cannot be empty.")

    # JPEG
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"

    # PNG
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    # WEBP: RIFF....WEBP
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"

    # PDF
    if data.startswith(b"%PDF-"):
        return "application/pdf"

    # DOCX is a ZIP container. Verify the ZIP signature first.
    if data.startswith(b"PK\x03\x04"):
        return (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    # Text: require valid UTF-8 and reject obvious binary NUL data.
    if b"\x00" not in data:
        try:
            data.decode("utf-8")
            return "text/plain"
        except UnicodeDecodeError:
            pass

    raise ValueError("Unsupported or unrecognized attachment content type.")


def ingest_attachment(attachment: Attachment, data: bytes) -> IngestedAttachment:
    """Validate metadata and independently verify the supplied file bytes."""

    if not isinstance(data, bytes):
        raise TypeError("Attachment content must be bytes.")

    attachment.validate()

    if len(data) != attachment.size_bytes:
        attachment.status = AttachmentStatus.REJECTED
        raise ValueError(
            "Attachment size does not match the supplied content."
        )

    try:
        detected_type = detect_content_type(data)
    except ValueError:
        attachment.status = AttachmentStatus.REJECTED
        raise

    if detected_type != attachment.mime_type:
        attachment.status = AttachmentStatus.REJECTED
        raise ValueError(
            "Attachment MIME type does not match the detected content type."
        )

    attachment.status = AttachmentStatus.READY

    return IngestedAttachment(
        attachment=attachment,
        detected_type=detected_type,
        content=data,
    )
