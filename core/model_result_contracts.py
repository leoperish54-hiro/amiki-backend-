"""Typed result boundary for the model stage.

The provider adapters retain their legacy string API for compatibility. The
conversation pipeline converts that boundary into this explicit result type.
"""

from dataclasses import dataclass
from typing import Literal


ModelResultStatus = Literal[
    "success",
    "provider_error",
    "invalid_response",
    "empty_response",
    "invalid_result",
]


@dataclass(frozen=True)
class ModelResult:
    """A validated, user-deliverable model-stage outcome."""

    status: ModelResultStatus
    text: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        valid_statuses = {
            "success",
            "provider_error",
            "invalid_response",
            "empty_response",
            "invalid_result",
        }
        if self.status not in valid_statuses:
            raise ValueError("Unsupported model result status.")

        if self.status == "success":
            if not isinstance(self.text, str) or not self.text.strip():
                raise ValueError("Successful model results require non-empty text.")
            if self.error is not None:
                raise ValueError("Successful model results cannot contain an error.")
            return

        if self.text is not None:
            raise ValueError("Failed model results cannot contain answer text.")
        if not isinstance(self.error, str) or not self.error.startswith("ERROR:"):
            raise ValueError("Failed model results require a stable ERROR response.")

    @property
    def succeeded(self) -> bool:
        return self.status == "success"

    @classmethod
    def from_legacy_response(cls, response) -> "ModelResult":
        """Normalize the existing ask_model string contract without leaking types."""
        if not isinstance(response, str):
            return cls(
                status="invalid_result",
                error="ERROR: Invalid model result.",
            )

        if response.startswith("ERROR:"):
            detail = response.removeprefix("ERROR:").strip().lower()
            if (
                "no answer text" in detail
                or "no candidates" in detail
                or "returned no text" in detail
            ):
                status = "empty_response"
            elif "invalid" in detail:
                status = "invalid_response"
            else:
                status = "provider_error"
            return cls(status=status, error=response)

        if not response.strip():
            return cls(
                status="empty_response",
                error="ERROR: Model returned no answer text.",
            )

        return cls(status="success", text=response)

    def to_legacy_response(self) -> str:
        """Preserve the public string response expected by existing callers."""
        if self.succeeded:
            return self.text
        return self.error or "ERROR: Invalid model result."
