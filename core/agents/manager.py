"""Safety-first, bounded agent execution primitives for Amiki."""

from dataclasses import dataclass, field
from threading import Event, Lock, Timer, Thread
from time import monotonic
from uuid import uuid4

from core.observability import get_observability
from typing import Any, Callable, Dict, Iterable, Mapping, Optional


Tool = Callable[..., Any]


class AgentCancelledError(RuntimeError):
    """Internal signal used for cooperative agent cancellation."""


class AgentToolFailure(RuntimeError):
    """A tool-reported failure with a deliberately safe public message."""

    def __init__(self, public_message: str):
        message = public_message.strip() if isinstance(public_message, str) else ""
        super().__init__(message[:300] or "Agent tool did not complete the requested action.")
        self.public_message = str(self)[:300]


@dataclass
class AgentCancellationToken:
    _event: Event = field(default_factory=Event, init=False, repr=False)
    _reason: Optional[str] = field(default=None, init=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def cancel(self, reason: str = "Agent execution cancelled.") -> None:
        with self._lock:
            self._reason = reason
            self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> Optional[str]:
        with self._lock:
            return self._reason

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise AgentCancelledError(self._reason or "Agent execution cancelled.")


@dataclass(frozen=True)
class AgentExecutionContext:
    cancellation: AgentCancellationToken
    deadline: float

    def raise_if_cancelled(self) -> None:
        self.cancellation.raise_if_cancelled()
        if monotonic() >= self.deadline:
            self.cancellation.cancel("Agent execution time limit exceeded.")
            raise AgentCancelledError("Agent execution time limit exceeded.")

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - monotonic())


@dataclass(frozen=True)
class AgentTool:
    handler: Tool
    permission: str = "agent.read"
    read_only: bool = True
    timeout_seconds: float = 5.0
    pass_context: bool = True


@dataclass(frozen=True)
class AgentStep:
    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    """Safe execution result plus bounded provenance for the orchestrator."""

    status: str
    completed_steps: int = 0
    results: list[Any] = field(default_factory=list)
    error: Optional[str] = None
    agent: Optional[str] = None
    planned_steps: int = 0
    stop_condition: Optional[str] = None
    evidence: dict[str, Any] = field(default_factory=dict)


class AgentManager:
    """Execute explicit plans without autonomous replanning or recursion."""

    def __init__(
        self,
        tools: Optional[Mapping[str, AgentTool | Tool]] = None,
        *,
        max_steps: int = 5,
        max_result_chars: int = 12000,
        max_runtime_seconds: float = 30.0,
        allowed_tools: Optional[Iterable[str]] = None,
        allowed_permissions: Iterable[str] = ("agent.read",),
    ) -> None:
        if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 1:
            raise ValueError("max_steps must be a positive integer.")
        if not isinstance(max_result_chars, int) or isinstance(max_result_chars, bool) or max_result_chars < 1:
            raise ValueError("max_result_chars must be a positive integer.")
        if not isinstance(max_runtime_seconds, (int, float)) or isinstance(max_runtime_seconds, bool) or max_runtime_seconds <= 0:
            raise ValueError("max_runtime_seconds must be positive.")

        registered: Dict[str, AgentTool] = {}
        for name, tool in dict(tools or {}).items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Agent tools must have non-empty names.")
            if isinstance(tool, AgentTool):
                spec = tool
            elif callable(tool):
                spec = AgentTool(handler=tool, permission="agent.read", read_only=True, pass_context=False)
            else:
                raise ValueError("Agent tools must be callable or AgentTool instances.")

            if not callable(spec.handler):
                raise ValueError("Agent tool handlers must be callable.")
            if not isinstance(spec.permission, str) or not spec.permission.strip():
                raise ValueError("Agent tool permissions must be non-empty.")
            if not isinstance(spec.read_only, bool):
                raise ValueError("Agent tool read_only must be a boolean.")
            if not isinstance(spec.timeout_seconds, (int, float)) or isinstance(spec.timeout_seconds, bool) or spec.timeout_seconds <= 0:
                raise ValueError("Agent tool timeout_seconds must be positive.")
            if spec.timeout_seconds > max_runtime_seconds:
                raise ValueError("Agent tool timeout cannot exceed max runtime.")
            if not spec.read_only and spec.permission.endswith(".read"):
                raise ValueError("Read-only permission cannot be assigned to a mutating agent tool.")
            registered[name] = spec

        self._tools = registered
        self._max_steps = max_steps
        self._max_result_chars = max_result_chars
        self._max_runtime_seconds = float(max_runtime_seconds)

        if allowed_tools is not None and isinstance(allowed_tools, (str, bytes)):
            raise ValueError("allowed_tools must be an iterable of tool names, not a string.")
        if isinstance(allowed_permissions, (str, bytes)):
            raise ValueError("allowed_permissions must be an iterable of permission names, not a string.")

        self._allowed_tools = set(registered) if allowed_tools is None else set(allowed_tools)
        self._allowed_permissions = set(allowed_permissions)

        if any(not isinstance(tool_name, str) or not tool_name.strip() for tool_name in self._allowed_tools):
            raise ValueError("Agent tool names must be non-empty strings.")
        if not self._allowed_tools.issubset(registered):
            raise ValueError("allowed_tools must reference registered agent tools.")
        if any(not isinstance(permission, str) or not permission.strip() for permission in self._allowed_permissions):
            raise ValueError("Agent permissions must be non-empty strings.")

    @property
    def max_steps(self) -> int:
        return self._max_steps

    @property
    def max_result_chars(self) -> int:
        return self._max_result_chars

    @property
    def max_runtime_seconds(self) -> float:
        return self._max_runtime_seconds

    def execute(
        self,
        steps: list[AgentStep],
        *,
        cancellation: Optional[AgentCancellationToken] = None,
        correlation_id: Optional[str] = None,
    ) -> AgentResult:
        """Execute a fixed plan with permission, result, cancellation and time bounds."""
        if not isinstance(steps, list):
            return AgentResult(status="error", error="Agent steps must be a list.")
        if len(steps) > self._max_steps:
            return AgentResult(status="error", error="Agent step limit exceeded.")

        token = cancellation or AgentCancellationToken()
        deadline = monotonic() + self._max_runtime_seconds
        effective_correlation_id = correlation_id or f"agent_{uuid4().hex}"
        results: list[Any] = []

        for step in steps:
            try:
                token.raise_if_cancelled()
                if monotonic() >= deadline:
                    token.cancel("Agent execution time limit exceeded.")
                    raise AgentCancelledError("Agent execution time limit exceeded.")

                if not isinstance(step, AgentStep):
                    return AgentResult(status="error", completed_steps=len(results), results=results, error="Invalid agent step.")
                if not isinstance(step.tool, str) or not step.tool.strip():
                    return AgentResult(status="error", completed_steps=len(results), results=results, error="Invalid agent tool.")
                if not isinstance(step.arguments, Mapping):
                    return AgentResult(status="error", completed_steps=len(results), results=results, error="Invalid agent arguments.")
                if step.tool not in self._allowed_tools:
                    return AgentResult(status="error", completed_steps=len(results), results=results, error="Agent tool is not permitted.")

                spec = self._tools.get(step.tool)
                if spec is None:
                    return AgentResult(status="error", completed_steps=len(results), results=results, error="Agent tool is not available.")
                if spec.permission not in self._allowed_permissions:
                    return AgentResult(status="error", completed_steps=len(results), results=results, error="Agent tool permission denied.")
                if not spec.read_only and spec.permission == "agent.read":
                    return AgentResult(status="error", completed_steps=len(results), results=results, error="Agent write tool has invalid permission.")

                get_observability().emit({
                    "event_type": "TOOL_STARTED",
                    "severity": "INFO",
                    "source": "backend",
                    "component": step.tool,
                    "summary": "Agent tool started.",
                    "status": "started",
                    "correlation_id": effective_correlation_id,
                    "metadata": {
                        "read_only": spec.read_only,
                        "permission": spec.permission,
                    },
                })

                step_deadline = min(deadline, monotonic() + spec.timeout_seconds)
                context = AgentExecutionContext(token, step_deadline)
                timer = Timer(max(0.001, context.remaining_seconds), token.cancel, kwargs={"reason": "Agent tool execution timed out."})
                timer.daemon = True
                timer.start()

                holder: dict[str, Any] = {}
                tool_error: list[BaseException] = []

                def invoke() -> None:
                    try:
                        if spec.pass_context:
                            holder["value"] = spec.handler(step.arguments, context)
                        else:
                            holder["value"] = spec.handler(step.arguments)
                    except BaseException as exc:
                        tool_error.append(exc)

                worker = Thread(target=invoke, daemon=True)
                worker.start()
                worker.join(context.remaining_seconds)
                timer.cancel()

                if worker.is_alive():
                    token.cancel("Agent tool execution timed out.")
                    get_observability().emit_failure(
                        {
                            "event_type": "TOOL_FAILED",
                            "severity": "ERROR",
                            "source": "backend",
                            "component": step.tool,
                            "summary": "Agent tool timed out.",
                            "status": "failed",
                            "correlation_id": effective_correlation_id,
                        },
                        category="AGENT_TOOL",
                        confirmed_evidence=["Agent tool execution exceeded its time limit."],
                    )
                    return AgentResult(status="error", completed_steps=len(results), results=results, error="Agent tool execution timed out.")

                if tool_error:
                    exc = tool_error[0]
                    get_observability().emit_failure(
                        {
                            "event_type": "TOOL_FAILED",
                            "severity": "ERROR",
                            "source": "backend",
                            "component": step.tool,
                            "summary": "Agent tool execution failed.",
                            "status": "failed",
                            "correlation_id": effective_correlation_id,
                        },
                        category="AGENT_TOOL",
                        confirmed_evidence=["Agent tool execution failure was observed."],
                    )
                    if isinstance(exc, AgentCancelledError):
                        return AgentResult(status="error", completed_steps=len(results), results=results, error=str(exc))
                    if isinstance(exc, AgentToolFailure):
                        return AgentResult(
                            status="error",
                            completed_steps=len(results),
                            results=results,
                            error=exc.public_message,
                        )
                    return AgentResult(status="error", completed_steps=len(results), results=results, error="Agent tool execution failed.")

                context.raise_if_cancelled()

                value = holder.get("value")
                try:
                    result_size = len(repr(value))
                except Exception:
                    return AgentResult(status="error", completed_steps=len(results), results=results, error="Agent tool result could not be safely represented.")
                if result_size > self._max_result_chars:
                    return AgentResult(status="error", completed_steps=len(results), results=results, error="Agent tool result exceeded the size limit.")
                results.append(value)
                get_observability().emit({
                    "event_type": "TOOL_COMPLETED",
                    "severity": "INFO",
                    "source": "backend",
                    "component": step.tool,
                    "summary": "Agent tool completed.",
                    "status": "completed",
                    "correlation_id": effective_correlation_id,
                })

            except AgentCancelledError as exc:
                return AgentResult(status="error", completed_steps=len(results), results=results, error=str(exc))

        return AgentResult(status="success", completed_steps=len(results), results=results)


__all__ = [
    "AgentCancelledError",
    "AgentCancellationToken",
    "AgentExecutionContext",
    "AgentManager",
    "AgentResult",
    "AgentStep",
    "AgentTool",
    "AgentToolFailure",
]
