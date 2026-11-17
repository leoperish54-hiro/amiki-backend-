from pydantic import BaseModel


class ProviderStateResponse(BaseModel):
    configured: bool
    available: bool | None = None
    active: bool
    last_observed_status: str | None = None
    evidence_request_started_at: str | None = None
    evidence_request_id: str | None = None


class SystemStateResponse(BaseModel):
    identity: dict[str, str]
    runtime: dict[str, str]
    capabilities: dict[str, bool]
    device: dict
    awareness: dict
    providers: dict[str, ProviderStateResponse]
    architecture: dict[str, list[str]]
    context: dict[str, str | None]
    limitations: list[str]
