"""Least-privilege adapters for specialized Amiki agents."""

from collections.abc import Mapping
from typing import Any

from core.agents.contracts import (
    normalize_analysis_result,
    normalize_engineering_result,
    normalize_research_result,
    normalize_tutor_result,
)
from core.agents.manager import AgentExecutionContext, AgentTool, AgentToolFailure
from core.agents.device import device_capabilities, plan_device_action
from core.handlers.analysis import handle as analysis_handler
from core.handlers.browser import browse as browser_handler
from core.handlers.university import handle as university_handler
from core.handlers.web import handle as web_handler
from core.handlers.codebase import (
    inspect_codebase,
    read_codebase,
)
from core.handlers.git_metadata import get_head_commit
from core.handlers.termux_device import (
    execute_approved_device_action,
    read_device_action,
)
from core.handlers.workspace import (
    edit_workspace,
    execute_workspace,
    read_workspace,
    inspect_workspace,
    write_workspace,
    check_workspace_python,
    resolve_workspace_project,
)


def _message(arguments: Mapping[str, Any]) -> str:
    message = arguments.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must be a non-empty string")
    return message.strip()


def _approved(arguments: Mapping[str, Any]) -> None:
    if arguments.get("approved") is not True:
        raise ValueError("Explicit approval is required for this action.")


def research_lookup(arguments: Mapping[str, Any], context: AgentExecutionContext):
    context.raise_if_cancelled()
    result = web_handler(_message(arguments))
    context.raise_if_cancelled()
    return normalize_research_result(result)


def research_browse(arguments: Mapping[str, Any], context: AgentExecutionContext):
    context.raise_if_cancelled()
    url = arguments.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    result = browser_handler(url.strip())
    context.raise_if_cancelled()
    return normalize_research_result(result)


def tutor_prepare(arguments: Mapping[str, Any], context: AgentExecutionContext):
    context.raise_if_cancelled()
    result = university_handler(_message(arguments))
    context.raise_if_cancelled()
    return normalize_tutor_result(result)


def analysis_prepare(arguments: Mapping[str, Any], context: AgentExecutionContext):
    context.raise_if_cancelled()
    result = analysis_handler(_message(arguments))
    context.raise_if_cancelled()
    return normalize_analysis_result(result)


def engineering_inspect(arguments: Mapping[str, Any], context: AgentExecutionContext):
    context.raise_if_cancelled()
    path = arguments.get("path", ".")
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    project_id = arguments.get("project_id")
    if isinstance(project_id, str):
        root = resolve_workspace_project(project_id)
        if root is None:
            return normalize_engineering_result({
                "status": "error", "type": "inspection",
                "error": "Selected project is unknown or unavailable.",
            })
        result = inspect_workspace(path, root=root)
    else:
        result = inspect_codebase(path)
    context.raise_if_cancelled()
    return normalize_engineering_result(result)


def engineering_read(arguments: Mapping[str, Any], context: AgentExecutionContext):
    context.raise_if_cancelled()
    path = arguments.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    project_id = arguments.get("project_id")
    if isinstance(project_id, str):
        root = resolve_workspace_project(project_id)
        if root is None:
            return normalize_engineering_result({
                "status": "error", "type": "file",
                "error": "Selected project is unknown or unavailable.",
            })
        result = read_workspace(path, max_bytes=50_000, root=root)
    else:
        result = read_codebase(path)
    context.raise_if_cancelled()
    return normalize_engineering_result(result)


def engineering_edit(arguments: Mapping[str, Any], context: AgentExecutionContext):
    context.raise_if_cancelled()
    _approved(arguments)
    path = arguments.get("path")
    old_text = arguments.get("old_text")
    new_text = arguments.get("new_text")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    if not isinstance(old_text, str) or not old_text:
        raise ValueError("old_text must be a non-empty string")
    if not isinstance(new_text, str):
        raise ValueError("new_text must be a string")

    project_id = arguments.get("project_id")
    if isinstance(project_id, str):
        root = resolve_workspace_project(project_id)
        if root is None:
            return normalize_engineering_result({
                "status": "error", "type": "file",
                "error": "Selected project is unknown or unavailable.",
            })
        current = read_workspace(path, max_bytes=50_000, root=root)
        if current.get("status") != "success":
            return normalize_engineering_result(current)
        content = current.get("content", "")
        if content.count(old_text) != 1:
            return normalize_engineering_result({
                "status": "error", "type": "file", "path": path,
                "error": "Requested text must occur exactly once.",
            })
        result = write_workspace(
            path, expected_content=content,
            new_content=content.replace(old_text, new_text, 1),
            root=root,
        )
    else:
        result = edit_workspace(path, old_text, new_text)
    context.raise_if_cancelled()

    return normalize_engineering_result({
        "status": result.get("status", "error"),
        "type": result.get("type", "file"),
        "path": result.get("path", path),
        "replacement_count": result.get("replacement_count", 0),
    })


def engineering_execute(arguments: Mapping[str, Any], context: AgentExecutionContext):
    context.raise_if_cancelled()
    _approved(arguments)
    command = arguments.get("command", "python")
    args = arguments.get("args")
    if command != "python":
        raise ValueError("Only the allowlisted Python command is available.")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError("args must be a list of strings")

    project_id = arguments.get("project_id")
    if isinstance(project_id, str):
        root = resolve_workspace_project(project_id)
        if root is None:
            return normalize_engineering_result({
                "status": "error", "type": "execution",
                "error": "Selected project is unknown or unavailable.",
            })
        result = execute_workspace(command, args, root=root)
    else:
        result = execute_workspace(command, args)
    context.raise_if_cancelled()

    return normalize_engineering_result({
        "status": result.get("status", "error"),
        "command": command,
        "args": args,
        "return_code": result.get("return_code"),
        "stdout": str(result.get("stdout", ""))[-4000:],
        "stderr": str(result.get("stderr", ""))[-4000:],
    })


def engineering_git_metadata(arguments: Mapping[str, Any], context: AgentExecutionContext):
    context.raise_if_cancelled()
    result = get_head_commit()
    context.raise_if_cancelled()
    return normalize_engineering_result(result)


def engineering_verify(arguments: Mapping[str, Any], context: AgentExecutionContext):
    context.raise_if_cancelled()
    path = arguments.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")

    project_id = arguments.get("project_id")
    if isinstance(project_id, str):
        root = resolve_workspace_project(project_id)
        if root is None:
            return normalize_engineering_result({
                "status": "error", "type": "verification",
                "error": "Selected project is unknown or unavailable.",
            })
        result = read_workspace(path, max_bytes=50_000, root=root)
    else:
        result = read_codebase(path)
    if result.get("status") != "success":
        return normalize_engineering_result(result)

    content = result.get("content", "")
    if path.lower().endswith(".py"):
        try:
            compile(content, path, "exec")
        except SyntaxError as exc:
            return normalize_engineering_result({
                "status": "error",
                "type": "verification",
                "path": path,
                "error": f"Python syntax error at line {exc.lineno}.",
            })

    context.raise_if_cancelled()
    return normalize_engineering_result({
        "status": "success",
        "type": "verification",
        "path": path,
        "verified": True,
        "checks": ["file_readable", "python_syntax" if path.lower().endswith(".py") else "file_present"],
    })


def device_plan(arguments: Mapping[str, Any], context: AgentExecutionContext):
    context.raise_if_cancelled()
    action = arguments.get("action")
    args = arguments.get("arguments", {})
    if not isinstance(action, str) or not isinstance(args, Mapping):
        raise ValueError("action and arguments are required")

    if action in {"battery_state", "device_state"}:
        result = read_device_action(action)
        context.raise_if_cancelled()
        return result

    plan = plan_device_action(action, dict(args))
    context.raise_if_cancelled()
    return {
        "status": "planned",
        "action": plan.action,
        "arguments": plan.arguments,
        "requires_user_approval": plan.requires_user_approval,
        "reason": plan.reason,
        "execution_model": "android_client",
    }


def device_execute(arguments: Mapping[str, Any], context: AgentExecutionContext):
    context.raise_if_cancelled()
    action = arguments.get("action")
    args = arguments.get("arguments", {})
    if not isinstance(action, str) or not isinstance(args, Mapping):
        raise ValueError("action and arguments are required")
    result = execute_approved_device_action(action, dict(args))
    context.raise_if_cancelled()
    if result.get("status") != "accepted":
        reason = result.get("reason")
        public_reason = (
            reason[:300]
            if isinstance(reason, str) and reason.strip()
            else "The device action did not complete."
        )
        raise AgentToolFailure(public_reason)
    return result


def build_device_tool() -> AgentTool:
    return AgentTool(device_plan, permission="agent.device.plan", read_only=True, timeout_seconds=5.0)


def build_device_execution_tool() -> AgentTool:
    return AgentTool(
        device_execute,
        permission="agent.device.execute",
        read_only=False,
        timeout_seconds=70.0,
    )


def build_read_only_web_tool() -> AgentTool:
    return AgentTool(research_lookup, permission="agent.web.read", read_only=True, timeout_seconds=10.0)


def build_read_only_browser_tool() -> AgentTool:
    return AgentTool(research_browse, permission="agent.web.read", read_only=True, timeout_seconds=10.0)


def build_tutor_tool() -> AgentTool:
    return AgentTool(tutor_prepare, permission="agent.study.read", read_only=True, timeout_seconds=55.0)


def build_analysis_tool() -> AgentTool:
    return AgentTool(analysis_prepare, permission="agent.analysis.read", read_only=True, timeout_seconds=55.0)


def build_engineering_inspect_tool() -> AgentTool:
    return AgentTool(engineering_inspect, permission="agent.workspace.read", read_only=True, timeout_seconds=5.0)


def build_engineering_read_tool() -> AgentTool:
    return AgentTool(engineering_read, permission="agent.workspace.read", read_only=True, timeout_seconds=5.0)


def build_engineering_edit_tool() -> AgentTool:
    return AgentTool(engineering_edit, permission="agent.workspace.write", read_only=False, timeout_seconds=5.0)


def build_engineering_execute_tool() -> AgentTool:
    return AgentTool(engineering_execute, permission="agent.workspace.execute", read_only=False, timeout_seconds=10.0)


def build_engineering_verify_tool() -> AgentTool:
    return AgentTool(engineering_verify, permission="agent.workspace.verify", read_only=True, timeout_seconds=5.0)


def build_engineering_git_metadata_tool() -> AgentTool:
    return AgentTool(
        engineering_git_metadata,
        permission="agent.repository.metadata.read",
        read_only=True,
        timeout_seconds=3.0,
    )


__all__ = [
    "analysis_prepare",
    "build_analysis_tool",
    "build_engineering_edit_tool",
    "build_engineering_execute_tool",
    "build_engineering_inspect_tool",
    "build_engineering_git_metadata_tool",
    "build_engineering_read_tool",
    "build_engineering_verify_tool",
    "build_read_only_browser_tool",
    "build_read_only_web_tool",
    "build_tutor_tool",
    "engineering_edit",
    "engineering_execute",
    "engineering_inspect",
    "engineering_git_metadata",
    "engineering_read",
    "engineering_verify",
    "research_browse",
    "research_lookup",
    "tutor_prepare",
]
