"""Fixed, least-privilege specialist agent definitions."""

from dataclasses import dataclass

from core.agents.direct_specialists import analysis_answer, tutor_answer
from core.agents.inspection import build_inspection_tool
from core.agents.diagnostics import build_diagnostics_tool
from core.agents.security import build_security_tool
from core.agents.manager import AgentManager, AgentResult, AgentStep, AgentTool
from core.agents.tools import (
    build_analysis_tool,
    build_engineering_edit_tool,
    build_engineering_execute_tool,
    build_engineering_inspect_tool,
    build_engineering_git_metadata_tool,
    build_engineering_read_tool,
    build_engineering_verify_tool,
    build_read_only_web_tool,
    build_device_tool,
    build_device_execution_tool,
    build_tutor_tool,
)


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    purpose: str
    permissions: frozenset[str]
    tools: frozenset[str]


def build_research_agent():
    d = AgentDefinition(
        "research",
        "Bounded factual web research",
        frozenset({"agent.web.read"}),
        frozenset({"web_search"}),
    )
    return d, AgentManager(
        {"web_search": build_read_only_web_tool()},
        allowed_tools=d.tools,
        allowed_permissions=d.permissions,
        max_steps=3,
        max_runtime_seconds=20,
        max_result_chars=12000,
    )




def _research_step(message: str) -> AgentStep:
    """Select browser only after an explicit browser directive."""
    stripped = message.strip()
    lowered = stripped.lower()
    for prefix in ("browse:", "open page:", "visit:"):
        if lowered.startswith(prefix):
            target = stripped[len(prefix):].strip()
            return AgentStep("web_browse", {"url": target})
    return AgentStep("web_search", {"message": message})


def build_direct_tutor_agent():
    d = AgentDefinition(
        "tutor",
        "Direct model-backed tutoring",
        frozenset({"agent.study.read"}),
        frozenset({"tutor_answer"}),
    )
    return d, AgentManager(
        {"tutor_answer": AgentTool(tutor_answer, permission="agent.study.read", read_only=True, timeout_seconds=55.0)},
        allowed_tools=d.tools,
        allowed_permissions=d.permissions,
        max_steps=1,
        max_runtime_seconds=65,
        max_result_chars=8000,
    )


def build_tutor_agent():
    d = AgentDefinition(
        "tutor",
        "Structured study preparation",
        frozenset({"agent.study.read"}),
        frozenset({"study_prepare"}),
    )
    return d, AgentManager(
        {"study_prepare": build_tutor_tool()},
        allowed_tools=d.tools,
        allowed_permissions=d.permissions,
        max_steps=1,
        max_runtime_seconds=65,
        max_result_chars=8000,
    )


def build_engineering_agent():
    d = AgentDefinition(
        "engineering",
        "Bounded project inspection, approved editing, execution and verification",
        frozenset({
            "agent.workspace.read",
            "agent.workspace.write",
            "agent.workspace.execute",
            "agent.workspace.verify",
            "agent.repository.metadata.read",
        }),
        frozenset({
            "workspace_inspect",
            "workspace_read",
            "workspace_edit",
            "workspace_execute",
            "workspace_verify",
            "git_metadata",
        }),
    )
    return d, AgentManager(
        {
            "workspace_inspect": build_engineering_inspect_tool(),
            "workspace_read": build_engineering_read_tool(),
            "workspace_edit": build_engineering_edit_tool(),
            "workspace_execute": build_engineering_execute_tool(),
            "workspace_verify": build_engineering_verify_tool(),
            "git_metadata": build_engineering_git_metadata_tool(),
        },
        allowed_tools=d.tools,
        allowed_permissions=d.permissions,
        max_steps=5,
        max_runtime_seconds=30,
        max_result_chars=12000,
    )




def build_direct_engineering_agent():
    """Expose project inspection only to direct clients until proposal approval is external."""
    d = AgentDefinition(
        "engineering",
        "Read-only project inspection and verification for direct clients",
        frozenset({
            "agent.workspace.read",
            "agent.workspace.verify",
            "agent.repository.metadata.read",
        }),
        frozenset({
            "workspace_inspect",
            "workspace_read",
            "workspace_verify",
            "git_metadata",
        }),
    )
    return d, AgentManager(
        {
            "workspace_inspect": build_engineering_inspect_tool(),
            "workspace_read": build_engineering_read_tool(),
            "workspace_verify": build_engineering_verify_tool(),
            "git_metadata": build_engineering_git_metadata_tool(),
        },
        allowed_tools=d.tools,
        allowed_permissions=d.permissions,
        max_steps=5,
        max_runtime_seconds=30,
        max_result_chars=12000,
    )


def build_device_agent():
    d = AgentDefinition(
        "device",
        "Bounded Android device queries and confirmation-gated actions",
        frozenset({"agent.device.plan", "agent.device.execute"}),
        frozenset({"device_plan", "device_execute"}),
    )
    return d, AgentManager(
        {
            "device_plan": build_device_tool(),
            "device_execute": build_device_execution_tool(),
        },
        allowed_tools=d.tools,
        allowed_permissions=d.permissions,
        max_steps=1,
        max_runtime_seconds=75,
        max_result_chars=6000,
    )

def build_direct_analysis_agent():
    d = AgentDefinition(
        "analysis",
        "Direct model-backed analysis",
        frozenset({"agent.analysis.read"}),
        frozenset({"analysis_answer"}),
    )
    return d, AgentManager(
        {"analysis_answer": AgentTool(analysis_answer, permission="agent.analysis.read", read_only=True, timeout_seconds=55.0)},
        allowed_tools=d.tools,
        allowed_permissions=d.permissions,
        max_steps=1,
        max_runtime_seconds=65,
        max_result_chars=8000,
    )


def build_analysis_agent():
    d = AgentDefinition(
        "analysis",
        "Structured reasoning preparation",
        frozenset({"agent.analysis.read"}),
        frozenset({"analysis_prepare"}),
    )
    return d, AgentManager(
        {"analysis_prepare": build_analysis_tool()},
        allowed_tools=d.tools,
        allowed_permissions=d.permissions,
        max_steps=1,
        max_runtime_seconds=65,
        max_result_chars=8000,
    )


def build_diagnostics_agent():
    d = AgentDefinition(
        "diagnostics",
        "Read-only, evidence-backed diagnostics across Amiki subsystems",
        frozenset({"agent.diagnostics.read"}),
        frozenset({"diagnose"}),
    )
    return d, AgentManager(
        {"diagnose": build_diagnostics_tool()},
        allowed_tools=d.tools,
        allowed_permissions=d.permissions,
        max_steps=1,
        max_runtime_seconds=12,
        max_result_chars=12000,
    )


def build_inspection_agent():
    d = AgentDefinition(
        "inspection",
        "Safe runtime diagnostics",
        frozenset({"agent.diagnostics.read"}),
        frozenset({"inspect"}),
    )
    return d, AgentManager(
        {"inspect": build_inspection_tool()},
        allowed_tools=d.tools,
        allowed_permissions=d.permissions,
        max_steps=1,
        max_runtime_seconds=12,
        max_result_chars=20000,
    )


def build_security_agent():
    d = AgentDefinition(
        "security",
        "Bounded read-only runtime and source-pattern security auditing",
        frozenset({"agent.security.audit"}),
        frozenset({"security_audit"}),
    )
    return d, AgentManager(
        {"security_audit": build_security_tool()},
        allowed_tools=d.tools,
        allowed_permissions=d.permissions,
        max_steps=1,
        max_runtime_seconds=12,
        max_result_chars=12000,
    )


def execute_agent(name: str, message: str = "") -> AgentResult:
    builders = {
        "research": build_research_agent,
        "tutor": build_tutor_agent,
        "engineering": build_engineering_agent,
        "analysis": build_analysis_agent,
        "inspection": build_inspection_agent,
        "device": build_device_agent,
        "security": build_security_agent,
    }
    if name not in builders:
        return AgentResult(status="error", error="Unknown agent.")

    _, manager = builders[name]()
    if name == "inspection":
        step = AgentStep("inspect", {"message": message.strip()})
    elif name == "security":
        step = AgentStep("security_audit")
    elif name == "research":
        step = AgentStep("web_search", {"message": message})
    elif name == "tutor":
        step = AgentStep("study_prepare", {"message": message})
    elif name == "device":
        step = AgentStep("device_plan", {"action": "device_state", "arguments": {}})
    elif name == "analysis":
        step = AgentStep("analysis_prepare", {"message": message})
    else:
        step = AgentStep("workspace_inspect", {"path": "."})
    return manager.execute([step])


__all__ = [
    "AgentDefinition",
    "build_analysis_agent",
    "build_direct_analysis_agent",
    "build_direct_tutor_agent",
    "build_engineering_agent",
    "build_device_agent",
    "build_inspection_agent",
    "build_security_agent",
    "build_research_agent",
    "build_tutor_agent",
    "execute_agent",
]
