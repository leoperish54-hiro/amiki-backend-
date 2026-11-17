from pydantic import BaseModel


class MemoryItem(BaseModel):
    category: str
    key: str
    value: str


class MemoryResponse(BaseModel):
    memories: list[MemoryItem]
