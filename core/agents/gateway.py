"""Safe gateway between normal chat and the bounded specialist-agent layer."""

import json

from core.agents.contracts import AgentResultContractError, normalize_specialist_result
from core.agents.collaboration import AgentCollaborationCoordinator, CollaborationTask
from core.agents.orchestrator import AgentOrchestrator


_AGENT_PREFIXES = (
    "agent:",
    "use agent:",
    "use the agent:",
    "use research agent:",
    "use the research agent:",
    "use tutor agent:",
    "use the tutor agent:",
    "use study agent:",
    "use the study agent:",
    "use engineering agent:",
    "use the engineering agent:",
    "use analysis agent:",
    "use the analysis agent:",
    "use inspection agent:",
    "use the inspection agent:",
    "use diagnostic agent:",
    "use the diagnostic agent:",
    "use device agent:",
    "use the device agent:",
    "use security agent:",
    "use the security agent:",
)

_EXPLICIT_AGENT_FORMS = (
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
    ("use inspection agent:", "inspection"),
    ("use the inspection agent:", "inspection"),
    ("use diagnostic agent:", "inspection"),
    ("use the diagnostic agent:", "inspection"),
    ("use device agent:", "device"),
    ("use the device agent:", "device"),
    ("use security agent:", "security"),
    ("use the security agent:", "security"),
)


class AgentGateway:
    """Single policy boundary for invoking specialist agents from chat."""

    def __init__(self, orchestrator=None, collaboration_coordinator=None):
        self._orchestrator = orchestrator or AgentOrchestrator()
        self._collaboration_coordinator = (
            collaboration_coordinator
            or AgentCollaborationCoordinator(self._orchestrator)
        )

    @staticmethod
    def _directive(message):
        if not isinstance(message, str):
            return None
        stripped = message.strip()
        lowered = stripped.lower()
        for prefix in _AGENT_PREFIXES:
            if lowered.startswith(prefix):
                request = stripped[len(prefix):].strip()
                return request or None
        for prefix, _agent in _EXPLICIT_AGENT_FORMS:
            natural_prefix = prefix[:-1] + " to"
            if lowered.startswith(natural_prefix):
                request = stripped[len(natural_prefix):].strip()
                return request or None
        return None

    @staticmethod
    def _explicit_agent(message):
        if not isinstance(message, str):
            return None
        lowered = message.strip().lower()
        for prefix, agent in _EXPLICIT_AGENT_FORMS:
            if lowered.startswith(prefix) or lowered.startswith(prefix[:-1] + " to"):
                return agent
        return None

    @staticmethod
    def _safe_payload(agent, reason, result):
        if not isinstance(agent, str) or not agent:
            return None
        if not isinstance(reason, str) or not reason:
            return None

        max_steps = 8 if agent == "collaboration" else 5
        max_results = 7 if agent == "collaboration" else 5
        completed_steps = result.completed_steps
        planned_steps = getattr(result, "planned_steps", completed_steps)
        if (
            isinstance(completed_steps, bool)
            or not isinstance(completed_steps, int)
            or completed_steps < 0
            or completed_steps > max_steps
        ):
            return None
        if (
            isinstance(planned_steps, bool)
            or not isinstance(planned_steps, int)
            or planned_steps < 0
            or planned_steps > max_steps
        ):
            return None
        if not isinstance(result.results, list) or len(result.results) > max_results:
            return None

        normalized_results = []
        try:
            for item in result.results:
                normalized_results.append(normalize_specialist_result(item))
            json.dumps(normalized_results, ensure_ascii=False)
        except (AgentResultContractError, TypeError, ValueError):
            return None

        return {
            "capability": "agent",
            "status": "handled",
            "result": {
                "agent": agent,
                "reason": reason,
                "completed_steps": result.completed_steps,
                "planned_steps": getattr(result, "planned_steps", result.completed_steps),
                "stop_condition": getattr(result, "stop_condition", None),
                "results": normalized_results,
                "evidence": dict(getattr(result, "evidence", {})),
            },
        }

    @staticmethod
    def _collaboration_directive(message):
        """Parse explicit bounded collaboration across any registered specialist."""
        if not isinstance(message, str):
            return None
        stripped = message.strip()
        prefix = "collaborate:"
        if not stripped.lower().startswith(prefix):
            return None

        body = stripped[len(prefix):].strip()
        if "|" not in body:
            return {"error": "Use collaborate: agent1, agent2 | request."}

        agents_text, request = body.split("|", 1)
        agents = [
            part.strip().lower().removesuffix(" agent")
            for part in agents_text.split(",")
        ]
        request = request.strip()
        if (
            not 2 <= len(agents) <= 7
            or any(not agent for agent in agents)
            or len(set(agents)) != len(agents)
            or not request
            or len(request) > 4000
        ):
            return {"error": "Use 2–7 distinct specialists and a non-empty request."}

        return {"agents": agents, "message": request}

    def auto_route(self, message):
        """Return an automatic specialist route, or None for ordinary chat."""
        if self._directive(message) is not None or self._explicit_agent(message) is not None:
            return None
        if not isinstance(message, str) or not message.strip():
            return None
        route = self._orchestrator.route(message)
        lowered = " ".join(message.lower().split())
        if any(lowered.startswith(prefix) for prefix in _AGENT_PREFIXES):
            return None
        if route.agent == "analysis" and not any(
            keyword in lowered
            for keyword in ("analyze", "analysis", "calculate", "reason", "compare")
        ):
            return None
        if route.reason in {"keyword_match", "explicit_instructional_intent"}:
            return route
        return None

    def execute(self, message, correlation_id=None):
        collaboration = self._collaboration_directive(message)
        if collaboration is not None:
            if "error" in collaboration:
                return {
                    "capability": "agent",
                    "status": "error",
                    "result": {
                        "agent": "collaboration",
                        "message": collaboration["error"],
                    },
                }

            tasks = [
                CollaborationTask(agent=agent, message=collaboration["message"])
                for agent in collaboration["agents"]
            ]
            try:
                result = self._collaboration_coordinator.execute(
                    tasks,
                    correlation_id=correlation_id,
                )
            except Exception:
                return {
                    "capability": "agent",
                    "status": "error",
                    "result": {
                        "agent": "collaboration",
                        "message": "The requested collaboration could not complete safely.",
                    },
                }

            if result.status != "success":
                return {
                    "capability": "agent",
                    "status": "error",
                    "result": {
                        "agent": "collaboration",
                        "message": "The requested collaboration could not complete safely.",
                        "completed_steps": result.completed_steps,
                        "planned_steps": getattr(result, "planned_steps", result.completed_steps),
                        "stop_condition": getattr(result, "stop_condition", None),
                        "evidence": dict(getattr(result, "evidence", {})),
                    },
                }

            safe_result = self._safe_payload(
                "collaboration",
                "explicit_collaboration_directive",
                result,
            )
            if safe_result is None:
                return {
                    "capability": "agent",
                    "status": "error",
                    "result": {
                        "agent": "collaboration",
                        "message": "The collaboration returned an invalid result contract.",
                    },
                }
            return safe_result

        request = self._directive(message)
        explicit_agent = self._explicit_agent(message)
        if request is None:
            if not isinstance(message, str) or not message.strip():
                return None
            route = self.auto_route(message)
            if route is None:
                return None
            request = message.strip()

        try:
            if explicit_agent:
                execute_kwargs = (
                    {"correlation_id": correlation_id}
                    if correlation_id is not None
                    else {}
                )
                if explicit_agent in {"tutor", "analysis", "engineering"}:
                    execute_kwargs["direct"] = True
                result = self._orchestrator.execute_for_agent(
                    explicit_agent,
                    request,
                    **execute_kwargs,
                )
                route = self._orchestrator.route(request)
            else:
                execute_kwargs = (
                    {"correlation_id": correlation_id}
                    if correlation_id is not None
                    else {}
                )
                result = self._orchestrator.execute(
                    request,
                    **execute_kwargs,
                )
                route = self._orchestrator.route(request)
        except Exception:
            return {
                "capability": "agent",
                "status": "error",
                "result": {
                    "agent": "unknown",
                    "message": "The requested agent could not complete safely.",
                },
            }

        agent = explicit_agent or getattr(route, "agent", "unknown")
        reason = "explicit_agent_directive" if explicit_agent else getattr(route, "reason", "unknown")

        if result.status != "success":
            return {
                "capability": "agent",
                "status": "error",
                "result": {
                    "agent": agent if isinstance(agent, str) else "unknown",
                    "message": "The requested agent could not complete safely.",
                    "completed_steps": result.completed_steps,
                    "planned_steps": getattr(result, "planned_steps", result.completed_steps),
                    "stop_condition": getattr(result, "stop_condition", None),
                    "evidence": dict(getattr(result, "evidence", {})),
                },
            }

        safe_result = self._safe_payload(agent, reason, result)
        if safe_result is None:
            return {
                "capability": "agent",
                "status": "error",
                "result": {
                    "agent": agent if isinstance(agent, str) else "unknown",
                    "message": "The agent returned an invalid result contract.",
                },
            }
        return safe_result


__all__ = ["AgentGateway"]
