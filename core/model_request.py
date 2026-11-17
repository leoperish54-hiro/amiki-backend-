"""Provider-neutral request contract for structured model conversations."""

from dataclasses import dataclass
from typing import Literal


MessageRole = Literal["user", "assistant"]


@dataclass(frozen=True)
class ModelMessage:
    """One conversational turn with an explicit speaker role."""

    role: MessageRole
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant"}:
            raise ValueError("Model messages must use the user or assistant role.")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("Model message content must be non-empty text.")


@dataclass(frozen=True)
class ModelRequest:
    """A system instruction, separate runtime context, and ordered chat turns."""

    system_instruction: str
    messages: tuple[ModelMessage, ...]
    context: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.system_instruction, str) or not self.system_instruction.strip():
            raise ValueError("Model requests require a non-empty system instruction.")

        if not isinstance(self.messages, (list, tuple)) or not self.messages:
            raise ValueError("Model requests require at least one conversation message.")

        normalized = tuple(self.messages)
        if any(not isinstance(message, ModelMessage) for message in normalized):
            raise TypeError("Model request messages must be ModelMessage instances.")
        if normalized[-1].role != "user":
            raise ValueError("The final model request message must be from the user.")

        object.__setattr__(self, "messages", normalized)

        if self.context is not None and (
            not isinstance(self.context, str) or not self.context.strip()
        ):
            raise ValueError("Model request context must be non-empty text when supplied.")
