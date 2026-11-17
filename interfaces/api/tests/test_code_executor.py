from pathlib import Path
import pytest

from core.agents import AgentManager, AgentStep
from core.agents.code_executor import build_code_execution_tool


def _manager():
    tool = build_code_execution_tool()
    return AgentManager(
        {"python": tool},
        allowed_tools={"python"},
        allowed_permissions={"agent.code.execute"},
        max_runtime_seconds=5.0,
    )


def test_code_executor_runs_bounded_python():
    result = _manager().execute([
        AgentStep("python", {"code": "print(2 + 3)"})
    ])
    assert result.status == "success"
    assert result.results[0]["exit_code"] == 0
    assert result.results[0]["stdout"].strip() == "5"


def test_code_executor_rejects_restricted_modules():
    result = _manager().execute([
        AgentStep("python", {"code": "import os\nprint(os.getcwd())"})
    ])
    assert result.status == "error"
    assert result.error == "Agent tool execution failed."


def test_code_executor_rejects_dynamic_execution():
    result = _manager().execute([
        AgentStep("python", {"code": "eval('2 + 2')"})
    ])
    assert result.status == "error"
    assert result.error == "Agent tool execution failed."


def test_code_executor_does_not_receive_normal_agent_read_permission():
    tool = build_code_execution_tool()
    manager = AgentManager({"python": tool})
    result = manager.execute([AgentStep("python", {"code": "print('no')"})])
    assert result.status == "error"
    assert result.error == "Agent tool permission denied."


def test_code_executor_isolated_working_directory():
    result = _manager().execute([
        AgentStep("python", {"code": "print(__file__)"})
    ])
    assert result.status == "success"
    assert result.results[0]["exit_code"] == 0
    path = Path(result.results[0]["stdout"].strip())
    assert path.name == "main.py"
    assert path.parent.name.startswith("amiki-agent-")


def test_code_executor_rejects_reflection_escape_hatches():
    from core.agents.code_executor import CodeExecutionRejected, _validate_code

    for code in (
        "print((1).__class__)",
        "print((1).__class__.__base__)",
        "print((1).__class__.__subclasses__())",
        "print(getattr(1, '__class__'))",
        "print(__builtins__)",
    ):
        try:
            _validate_code(code)
            assert False, f"unsafe reflection was accepted: {code}"
        except CodeExecutionRejected:
            pass
