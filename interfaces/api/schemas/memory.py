from pydantic import BaseModel, Field


class MemoryItem(BaseModel):
    id: int | None = None
    category: str
    key: str
    value: str
    confidence: float = 0.75
    provenance: dict = Field(default_factory=dict)
    source_type: str = "unknown"
    extraction_method: str = "unknown"
    created_at: str | None = None
    updated_at: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    status: str = "active"


class MemoryResponse(BaseModel):
    memories: list[MemoryItem]


class MemoryHistoryResponse(BaseModel):
    history: list[MemoryItem]
    events: list[dict] = Field(default_factory=list)
