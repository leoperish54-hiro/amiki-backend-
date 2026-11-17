from pydantic import BaseModel


class CapabilitiesResponse(BaseModel):
    analysis: bool
    university: bool
    workspace: bool
    noctis_requiem: bool
    web: bool
    device: bool
