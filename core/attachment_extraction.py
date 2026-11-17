"""Content extraction for validated Amiki attachments."""

from dataclasses import dataclass, field
from io import BytesIO
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

from pypdf import PdfReader

from core.attachment_ingestion import IngestedAttachment
from core.attachments import AttachmentStatus


DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


@dataclass(frozen=True)
class ExtractedAttachment:
    attachment_id: str
    content_type: str
    text: str | None = None
    data: bytes | None = None
    metadata: dict[str, str] = field(default_factory=dict)


def _extract_docx_text(data: bytes) -> str:
    """Extract paragraph text from a DOCX package without executing anything."""

    try:
        with ZipFile(BytesIO(data)) as archive:
            if "word/document.xml" not in archive.namelist():
                raise ValueError("DOCX document.xml is missing.")

            xml_data = archive.read("word/document.xml")
    except BadZipFile as error:
        raise ValueError("Invalid DOCX container.") from error

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as error:
        raise ValueError("Invalid DOCX XML content.") from error

    paragraphs = []

    for paragraph in root.iter(f"{{{WORD_NAMESPACE}}}p"):
        text_parts = [
            node.text or ""
            for node in paragraph.iter(f"{{{WORD_NAMESPACE}}}t")
        ]

        text = "".join(text_parts).strip()

        if text:
            paragraphs.append(text)

    return "\n".join(paragraphs)


def _extract_pdf_text(data: bytes) -> str:
    """Extract text from a PDF using pypdf."""

    try:
        reader = PdfReader(BytesIO(data))
    except Exception as error:
        raise ValueError("Invalid PDF document.") from error

    pages = []

    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception as error:
            raise ValueError("Failed to extract PDF text.") from error

        if text.strip():
            pages.append(text.strip())

    return "\n\n".join(pages)


def extract_attachment_content(
    ingested: IngestedAttachment,
) -> ExtractedAttachment:
    """Convert verified attachment bytes into model-ready content."""

    attachment = ingested.attachment
    data = ingested.content
    content_type = ingested.detected_type

    try:
        return _extract_attachment_content(ingested)
    except (ValueError, TypeError) as error:
        attachment.status = AttachmentStatus.FAILED
        raise error


def _extract_attachment_content(
    ingested: IngestedAttachment,
) -> ExtractedAttachment:
    """Perform extraction after the public failure-state boundary."""

    attachment = ingested.attachment
    data = ingested.content
    content_type = ingested.detected_type

    if content_type == "text/plain":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Text attachment is not valid UTF-8.") from error

        return ExtractedAttachment(
            attachment_id=attachment.id,
            content_type=content_type,
            text=text,
            metadata={
                "filename": attachment.filename,
            },
        )

    if content_type == "application/pdf":
        return ExtractedAttachment(
            attachment_id=attachment.id,
            content_type=content_type,
            text=_extract_pdf_text(data),
            metadata={
                "filename": attachment.filename,
            },
        )

    if content_type == DOCX_MIME:
        return ExtractedAttachment(
            attachment_id=attachment.id,
            content_type=content_type,
            text=_extract_docx_text(data),
            metadata={
                "filename": attachment.filename,
            },
        )

    if content_type.startswith("image/"):
        return ExtractedAttachment(
            attachment_id=attachment.id,
            content_type=content_type,
            data=data,
            metadata={
                "filename": attachment.filename,
                "size_bytes": str(len(data)),
            },
        )

    raise ValueError(
        f"No extraction handler exists for content type: {content_type}"
    )
