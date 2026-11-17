"""Bounded orchestrator for Amiki's specialist agents."""

from dataclasses import dataclass
from typing import Optional

from core.agents.manager import AgentCancellationToken, AgentResult, AgentStep
from core.agents.device import plan_device_request
from core.observability import get_observability
from core.agents.specialized import (
    build_analysis_agent,
    build_engineering_agent,
    build_inspection_agent,
    build_diagnostics_agent,
    build_research_agent,
    build_tutor_agent,
    build_device_agent,
    build_direct_analysis_agent,
    build_direct_tutor_agent,
    build_direct_engineering_agent,
    build_security_agent,
)


@dataclass(frozen=True)
class AgentRoute:
    agent: str
    reason: str


@dataclass(frozen=True)
class AgentPlan:
    agent: str
    steps: tuple[AgentStep, ...]
    max_iterations: int
    stop_condition: str


class AgentOrchestrator:
    """Route requests and execute bounded, auditable specialist plans."""

    _routes = (
        ("security", ("security", "security audit", "permissions audit", "threat model", "vulnerability review")),
        ("diagnostics", (
            "diagnostics", "diagnostic", "health check", "run diagnostics",
            "health report", "system health",
        )),
        ("inspection", (
            "inspect runtime", "inspect system", "amiki cloud", "cloud worker",
            "cloud schedule", "cloud job", "learning", "learning review",
            "learning pipeline", "research pipeline", "agent", "agents",
            "agent registry", "specialist agents", "all agents", "system-wide",
            "amiki", "tell me about amiki", "what can you tell me about amiki",
            "inspect amiki", "amiki system",
        )),
        ("device", ("device", "phone", "android", "battery", "call ", "dial ", "alarm", "set an alarm", "open browser", "open chrome", "open settings", "open https://", "open http://", "visit https://", "visit http://", "speak on my phone", "share from phone", "share text")),
        ("engineering", ("code", "coding", "bug", "debug", "workspace", "file", "project", "build")),
        ("research", ("research", "look up", "search the web", "latest", "source")),
        ("tutor", ("study", "learn", "teach", "explain", "lesson", "homework", "assignment", "exam", "university", "walk me through", "help me understand", "quiz me", "practice", "how do", "how does", "what is", "what are")),
        ("analysis", ("analyze", "analysis", "calculate", "reason", "compare")),
    )

    _builders = {
        "research": build_research_agent,
        "tutor": build_tutor_agent,
        "engineering": build_engineering_agent,
        "analysis": build_analysis_agent,
        "inspection": build_inspection_agent,
        "diagnostics": build_diagnostics_agent,
        "device": build_device_agent,
        "security": build_security_agent,
    }

    _direct_builders = {
        "research": build_research_agent,
        "tutor": build_direct_tutor_agent,
        "analysis": build_direct_analysis_agent,
        "engineering": build_direct_engineering_agent,
        "inspection": build_inspection_agent,
        "diagnostics": build_diagnostics_agent,
        "security": build_security_agent,
    }

    def route(self, message: str) -> AgentRoute:
        if not isinstance(message, str) or not message.strip():
            return AgentRoute("analysis", "empty_or_invalid_request")
        if len(message) > 4000:
            return AgentRoute("analysis", "request_too_large")

        lowered = " ".join(message.lower().split())

        # Explicit specialist directives are authoritative. Resolve them
        # before generic keyword routing so a request such as "Use the
        # Engineering Agent to inspect the backend" cannot be captured by
        # the Inspection Agent's broader inspect+backend heuristic.
        explicit_routes = (
            ("use research agent:", "research"),
            ("use the research agent:", "research"),
            ("use tutor agent:", "tutor"),
            ("use the tutor agent:", "tutor"),
            ("use study agent:", "tutor"),
            ("use the study agent:", "tutor"),
            ("use engineering agent:", "engineering"),
            ("use the engineering agent:", "engineering"),
            ("use analysis agent:", "analysis"),
            ("use the analysis agent:", "analysis"),
            ("use diagnostics agent:", "diagnostics"),
            ("use the diagnostics agent:", "diagnostics"),
            ("use inspection agent:", "inspection"),
            ("use the inspection agent:", "inspection"),
            ("use diagnostic agent:", "inspection"),
            ("use the diagnostic agent:", "inspection"),
            ("use device agent:", "device"),
            ("use the device agent:", "device"),
            ("use security agent:", "security"),
            ("use the security agent:", "security"),
        )
        for prefix, agent in explicit_routes:
            if lowered.startswith(prefix) or lowered.startswith(prefix[:-1] + " to"):
                return AgentRoute(agent, "explicit_agent_directive")

        if lowered.startswith(("speak ", "say ", "read aloud ")):
            return AgentRoute("device", "explicit_device_speech_directive")

        # Clear teaching requests should not be hijacked by incidental domain
        # keywords such as "debug", "search the web", or "inspect backend".
        instructional_phrases = (
            "teach me",
            "walk me through",
            "help me understand",
            "quiz me",
        )
        if any(phrase in lowered for phrase in instructional_phrases):
            return AgentRoute("tutor", "explicit_instructional_intent")

        inspection_context = (
            "backend",
            "system",
            "runtime",
            "configuration",
            "capability",
            "capabilities",
            "memory",
            "attachment",
            "attachments",
            "model routing",
            "model-routing",
            "pipeline",
        )
        if (
            "inspect" in lowered
            and any(term in lowered for term in inspection_context)
        ):
            return AgentRoute("inspection", "keyword_match")

        for agent, keywords in self._routes:
            if any(keyword in lowered for keyword in keywords):
                return AgentRoute(agent, "keyword_match")
        return AgentRoute("analysis", "safe_default")

    def plan(self, message: str, *, agent: Optional[str] = None) -> AgentPlan:
        if not isinstance(message, str) or not message.strip():
            return AgentPlan("analysis", tuple(), 0, "No executable request.")

        route = agent or self.route(message).agent
        if route not in self._builders:
            raise ValueError("Unknown agent.")

        if route == "device":
            try:
                device_plan = plan_device_request(message)
                tool = (
                    "device_execute"
                    if device_plan.action in {"open_url", "open_app", "speak", "set_alarm", "share_text", "call_contact"}
                    else "device_plan"
                )
                steps = (
                    AgentStep(
                        tool,
                        {
                            "action": device_plan.action,
                            "arguments": device_plan.arguments,
                        },
                    ),
                )
            except ValueError:
                steps = (
                    AgentStep(
                        "device_plan",
                        {"action": "unsupported_device_request", "arguments": {}},
                    ),
                )
        elif route == "inspection":
            steps = (AgentStep("inspect", {"message": message.strip()}),)
        elif route == "diagnostics":
            steps = (AgentStep("diagnose", {"message": message.strip()}),)
        elif route == "security":
            steps = (AgentStep("security_audit"),)
        elif route == "engineering":
            tokens = message.strip().split()
            candidates = []
            for token in tokens:
                candidate = token.strip(",.!?;:()[]{}")
                if not candidate:
                    continue
                # Only treat tokens as explicit filesystem paths when they
                # contain a path separator or a file-like suffix. Sentence
                # punctuation such as the period in "backend." must never
                # become a source path candidate.
                if "/" in candidate or "\\" in candidate:
                    candidates.append(candidate)
                    continue
                if "." in candidate and candidate.rsplit(".", 1)[-1].lower() in {
                    "py", "json", "toml", "yaml", "yml", "md", "txt", "sh"
                }:
                    candidates.append(candidate)
            path = candidates[-1] if candidates else "."
            steps = [AgentStep("workspace_inspect", {"path": path})]

            # For source-grounded architecture questions, inspect the known
            # allowlisted source files instead of falling back to guesses.
            lowered_message = message.lower()
            needs_git_metadata = any(term in lowered_message for term in (
                "git commit", "current commit", "head commit", "git head",
                "commit sha", "commit hash",
            ))
            if path == ".":
                source_targets = []
                if any(term in lowered_message for term in (
                    "fastapi", "api entry", "application entry", "app entry",
                    "fastapi endpoint", "route handler",
                )):
                    source_targets.append("interfaces/api/server/app.py")
                if any(term in lowered_message for term in (
                    "web search", "search handler", "live web", "duckduckgo",
                    "web handler",
                )):
                    source_targets.append("core/handlers/web.py")

                for source_path in source_targets:
                    steps.extend([
                        AgentStep("workspace_read", {"path": source_path}),
                        AgentStep("workspace_verify", {"path": source_path}),
                    ])

                if needs_git_metadata:
                    steps.append(AgentStep("git_metadata"))

            elif path != ".":
                steps.extend([
                    AgentStep("workspace_read", {"path": path}),
                    AgentStep("workspace_verify", {"path": path}),
                ])

            if needs_git_metadata and len(steps) > 5:
                # Preserve the requested repository provenance while keeping
                # the hard five-step plan bound. Verification is secondary
                # to the explicitly requested commit metadata.
                for index in range(len(steps) - 1, -1, -1):
                    if steps[index].tool == "workspace_verify":
                        steps.pop(index)
                        if len(steps) <= 5:
                            break

            steps = steps[:5]
            steps = tuple(steps)
        elif route == "research":
            steps = (AgentStep("web_search", {"message": message.strip()}),)
        elif route == "tutor":
            steps = (AgentStep("study_prepare", {"message": message.strip()}),)
        else:
            steps = (AgentStep("analysis_prepare", {"message": message.strip()}),)

        return AgentPlan(
            agent=route,
            steps=steps,
            max_iterations=min(3, max(1, len(steps))),
            stop_condition="Stop on cancellation, permission denial, tool failure, timeout, or completed plan.",
        )

    def _finalize(self, plan: AgentPlan, result: AgentResult, *, correlation_id: str | None = None) -> AgentResult:
        result.agent = plan.agent
        result.planned_steps = len(plan.steps)
        result.stop_condition = plan.stop_condition
        result.evidence = {
            "route": plan.agent,
            "completed_steps": result.completed_steps,
            "planned_steps": len(plan.steps),
            "status": result.status,
            "autonomous": True,
            "iterations": result.completed_steps,
        }
        if correlation_id is not None:
            result.evidence["correlation_id"] = correlation_id
        return result

    def execute(self, message: str, *, cancellation: AgentCancellationToken | None = None, correlation_id: str | None = None) -> AgentResult:
        if not isinstance(message, str) or not message.strip():
            return AgentResult(status="success", completed_steps=0, results=[])
        route = self.route(message)
        # User-facing specialists should produce answers, not just structured
        # preparation payloads. Device actions remain on the dedicated path.
        return self.execute_for_agent(
            route.agent,
            message,
            cancellation=cancellation,
            correlation_id=correlation_id,
            direct=route.agent != "device",
        )

    def execute_for_agent(self, agent: str, message: str, *, cancellation: AgentCancellationToken | None = None, correlation_id: str | None = None, direct: bool = False, project_id: str | None = None) -> AgentResult:
        builders = self._direct_builders if direct else self._builders
        if agent not in builders:
            return AgentResult(status="error", error="Unknown agent.")
        if not isinstance(message, str) or not message.strip():
            return AgentResult(status="success", completed_steps=0, results=[])

        plan = self.plan(message, agent=agent)
        if agent == "engineering" and isinstance(project_id, str) and project_id:
            project_tools = {"workspace_inspect", "workspace_read", "workspace_verify", "workspace_edit", "workspace_execute"}
            steps = tuple(
                AgentStep(step.tool, {**dict(step.arguments), "project_id": project_id})
                if step.tool in project_tools else step
                for step in plan.steps
            )
            plan = AgentPlan(
                agent=plan.agent,
                steps=steps,
                max_iterations=plan.max_iterations,
                stop_condition=plan.stop_condition,
            )
        if direct and agent in {"tutor", "analysis"}:
            direct_tool = "tutor_answer" if agent == "tutor" else "analysis_answer"
            plan = AgentPlan(agent=agent, steps=(AgentStep(direct_tool, {"message": message.strip()}),), max_iterations=1, stop_condition=plan.stop_condition)
            _, manager = self._direct_builders[agent]()
        else:
            _, manager = self._direct_builders[agent]() if direct else self._builders[agent]()
        agent_correlation_id = correlation_id or f"agent:{agent}"
        get_observability().emit({"event_type": "AGENT_STARTED", "severity": "INFO", "source": "backend", "component": agent, "summary": "Specialist agent started.", "status": "started", "correlation_id": agent_correlation_id, "metadata": {"planned_steps": len(plan.steps)}})
        result = manager.execute(list(plan.steps), cancellation=cancellation, correlation_id=agent_correlation_id)
        lifecycle_event = {
            "event_type": "AGENT_COMPLETED" if result.status == "success" else "AGENT_FAILED",
            "severity": "INFO" if result.status == "success" else "ERROR",
            "source": "backend",
            "component": agent,
            "summary": "Specialist agent completed." if result.status == "success" else "Specialist agent failed.",
            "status": result.status,
            "correlation_id": agent_correlation_id,
            "metadata": {"completed_steps": result.completed_steps},
        }
        if result.status == "success":
            get_observability().emit(lifecycle_event)
        else:
            get_observability().emit_failure(lifecycle_event, category="AGENT", confirmed_evidence=["Specialist agent execution failed."])
        return self._finalize(plan, result, correlation_id=agent_correlation_id)

    def execute_plan(self, agent: str, steps: list[AgentStep], *, cancellation: AgentCancellationToken | None = None, stop_condition: str = "Stop on cancellation, permission denial, tool failure, timeout, or completed plan.", correlation_id: str | None = None) -> AgentResult:
        """Execute an explicit, pre-authorized specialist plan without replanning."""
        if agent not in self._builders:
            return AgentResult(status="error", error="Unknown agent.")
        if not isinstance(steps, list) or not steps:
            return AgentResult(status="error", error="Agent plan must contain at least one step.")
        if len(steps) > 5:
            return AgentResult(status="error", error="Agent plan exceeds the maximum of 5 steps.")
        if not isinstance(stop_condition, str) or not stop_condition.strip():
            return AgentResult(status="error", error="Stop condition must be a non-empty string.")
        _, manager = self._builders[agent]()
        plan = AgentPlan(agent=agent, steps=tuple(steps), max_iterations=min(3, len(steps)), stop_condition=stop_condition.strip())
        agent_correlation_id = correlation_id or f"agent:{agent}"
        get_observability().emit({"event_type": "AGENT_STARTED", "severity": "INFO", "source": "backend", "component": agent, "summary": "Specialist agent started.", "status": "started", "correlation_id": agent_correlation_id, "metadata": {"planned_steps": len(plan.steps)}})
        result = manager.execute(steps, cancellation=cancellation, correlation_id=agent_correlation_id)
        get_observability().emit({
            "event_type": "AGENT_COMPLETED" if result.status == "success" else "AGENT_FAILED",
            "severity": "INFO" if result.status == "success" else "ERROR",
            "source": "backend",
            "component": agent,
            "summary": "Specialist agent completed." if result.status == "success" else "Specialist agent failed.",
            "status": result.status,
            "correlation_id": agent_correlation_id,
            "metadata": {"completed_steps": result.completed_steps},
        })
        return self._finalize(plan, result, correlation_id=agent_correlation_id)


__all__ = ["AgentOrchestrator", "AgentPlan", "AgentRoute"]
