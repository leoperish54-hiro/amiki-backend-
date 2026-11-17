from pydantic import BaseModel, Field, field_validator

from core.conversations.store import normalize_conversation_id


class AttachmentRequest(BaseModel):
    id: str | None = None
    filename: str
    mime_type: str
    size_bytes: int
    source: str = "unknown"


class LocationContextRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_meters: float = Field(gt=0, le=100000)
    timestamp: str = Field(min_length=10, max_length=64)
    source: str = "android_location_provider"
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    country_name: str | None = Field(default=None, max_length=80)
    locality: str | None = Field(default=None, max_length=100)


class ChatRequest(BaseModel):
    message: str
    capability: str | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    conversation_id: str | None = None
    location_context: LocationContextRequest | None = None
    attachments: list[AttachmentRequest] = Field(default_factory=list)

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, value):
        if value is None:
            return None
        try:
            return normalize_conversation_id(value)
        except (TypeError, ValueError) as error:
            raise ValueError("Conversation ID must be a canonical UUID v4.") from error


class ChatResponse(BaseModel):
    response: str
