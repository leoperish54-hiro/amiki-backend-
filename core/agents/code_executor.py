"""Bounded Python code execution tool for the agent layer.

This is intentionally not wired into normal chat yet. It executes Python in a
separate process with a temporary working directory, a sanitized environment,
resource limits, and a small safe builtins/module surface.

It is a defense-in-depth executor, not a claim of a perfect OS sandbox.
Production deployment should add a stronger container/OS sandbox boundary.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from core.agents.manager import AgentExecutionContext, AgentTool


MAX_CODE_CHARS = 12000
MAX_OUTPUT_CHARS = 12000
MAX_MEMORY_BYTES = 256 * 1024 * 1024
MAX_PROCESSES = 1


class CodeExecutionRejected(ValueError):
    """Raised when code fails the executor's static safety checks."""


_BLOCKED_IMPORTS = {
    "os",
    "sys",
    "subprocess",
    "socket",
    "ctypes",
    "multiprocessing",
    "threading",
    "signal",
    "resource",
    "pathlib",
    "shutil",
    "importlib",
    "pickle",
    "marshal",
}
_BLOCKED_CALLS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "open",
    "input",
    "breakpoint",
    "getattr",
    "setattr",
    "delattr",
    "globals",
    "locals",
    "vars",
    "dir",
    "help",
}
_BLOCKED_IMPORTS.update({
    "builtins",
    "gc",
    "code",
    "inspect",
    "traceback",
})
_BLOCKED_ATTRIBUTES = {
    "__builtins__",
    "__class__",
    "__base__",
    "__bases__",
    "__subclasses__",
    "__globals__",
    "__getattribute__",
    "__dict__",
    "__loader__",
    "__spec__",
}


def _validate_code(code: str) -> None:
    if not isinstance(code, str) or not code.strip():
        raise CodeExecutionRejected("code must be a non-empty string")
    if len(code) > MAX_CODE_CHARS:
        raise CodeExecutionRejected("code exceeds the size limit")

    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise CodeExecutionRejected("code contains a syntax error") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
            if any(name in _BLOCKED_IMPORTS for name in names):
                raise CodeExecutionRejected("code requests a restricted module")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _BLOCKED_IMPORTS:
                raise CodeExecutionRejected("code requests a restricted module")
        elif isinstance(node, ast.Attribute):
            if node.attr in _BLOCKED_ATTRIBUTES or node.attr.startswith("__"):
                raise CodeExecutionRejected("code requests a restricted attribute")
        elif isinstance(node, ast.Name):
            if node.id in _BLOCKED_ATTRIBUTES:
                raise CodeExecutionRejected("code requests a restricted name")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_CALLS:
                raise CodeExecutionRejected("code requests a restricted operation")


def _limits() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
        resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_CHARS, MAX_OUTPUT_CHARS))
        resource.setrlimit(resource.RLIMIT_NPROC, (MAX_PROCESSES, MAX_PROCESSES))
    except (ImportError, OSError, ValueError):
        # Some platforms do not expose POSIX resource controls.
        pass


def execute_python(arguments: dict[str, Any], context: AgentExecutionContext) -> dict[str, Any]:
    context.raise_if_cancelled()
    code = arguments.get("code")
    _validate_code(code)

    with tempfile.TemporaryDirectory(prefix="amiki-agent-") as workdir:
        script = Path(workdir) / "main.py"
        script.write_text(code, encoding="utf-8")

        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }

        timeout = max(0.05, min(context.remaining_seconds, 3.0))
        try:
            completed = subprocess.run(
                [sys.executable, "-I", str(script)],
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                preexec_fn=_limits if os.name == "posix" else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("code execution timed out") from exc

        context.raise_if_cancelled()
        stdout = completed.stdout[-MAX_OUTPUT_CHARS:]
        stderr = completed.stderr[-MAX_OUTPUT_CHARS:]

        return {
            "exit_code": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }


def build_code_execution_tool() -> AgentTool:
    return AgentTool(
        handler=execute_python,
        permission="agent.code.execute",
        read_only=False,
        timeout_seconds=4.0,
        pass_context=True,
    )
