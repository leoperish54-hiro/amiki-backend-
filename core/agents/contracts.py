"""Stable, bounded result contracts for specialist agents."""

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse


class AgentResultContractError(ValueError):
    """Raised when a specialist result cannot be normalized safely."""


_MAX_RESULT_CHARS = 12000


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentResultContractError(f"{name} result must be a mapping.")
    return value


def normalize_specialist_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentResultContractError("Specialist result must be a mapping.")

    try:
        encoded = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        raise AgentResultContractError("Specialist result must be JSON-serializable.") from None

    if len(encoded) > _MAX_RESULT_CHARS:
        raise AgentResultContractError("Specialist result exceeds the size limit.")

    status = value.get("status", "success")
    if not isinstance(status, str) or not status.strip():
        raise AgentResultContractError("Specialist result status must be a string.")

    message = value.get("message")
    if message is not None and not isinstance(message, str):
        raise AgentResultContractError("Specialist result message must be a string.")

    return {"status": status.strip(), "message": message, "data": dict(value)}


def normalize_research_result(value: Any) -> dict[str, Any]:
    result = _require_mapping(value, "Research")
    action = result.get("action")
    if not isinstance(action, str) or action not in ("live_web_search", "live_weather", "live_web_unavailable"):
        raise AgentResultContractError("Research result action is invalid.")

    evidence = result.get("evidence")
    if action in {"live_web_search", "live_weather"}:
        if not isinstance(evidence, Mapping):
            raise AgentResultContractError("Research evidence must be a mapping.")
        if evidence.get("trust_boundary") != "UNTRUSTED_EXTERNAL_EVIDENCE":
            raise AgentResultContractError("Research evidence trust boundary is invalid.")
        if not isinstance(evidence.get("provider"), str) or not evidence["provider"].strip():
            raise AgentResultContractError("Research evidence provider must be a non-empty string.")
        if not isinstance(evidence.get("query"), str) or not evidence["query"].strip():
            raise AgentResultContractError("Research evidence query must be a non-empty string.")

        sources = evidence.get("sources", [])
        facts = evidence.get("facts", [])
        untrusted_content = evidence.get("untrusted_content", [])
        if not isinstance(sources, list):
            raise AgentResultContractError("Research evidence sources must be a list.")
        if not isinstance(facts, list):
            raise AgentResultContractError("Research evidence facts must be a list.")
        if not isinstance(untrusted_content, list):
            raise AgentResultContractError("Research evidence untrusted content must be a list.")

        if action == "live_web_search":
            if not sources:
                raise AgentResultContractError("Successful web research must include at least one source.")
            for source in sources:
                if not isinstance(source, Mapping):
                    raise AgentResultContractError("Research source must be an object.")
                title = source.get("title")
                url = source.get("url")
                parsed_url = urlparse(url) if isinstance(url, str) else None
                if (
                    not isinstance(title, str)
                    or not title.strip()
                    or not isinstance(url, str)
                    or not url.strip()
                    or parsed_url is None
                    or parsed_url.scheme != "https"
                    or not parsed_url.hostname
                ):
                    raise AgentResultContractError("Research source must have a title and HTTPS URL.")

        if action == "live_weather":
            if not facts:
                raise AgentResultContractError("Successful weather research must include fact evidence.")
            for fact in facts:
                if (
                    not isinstance(fact, Mapping)
                    or not isinstance(fact.get("field"), str)
                    or not fact["field"].strip()
                    or not isinstance(fact.get("value"), str)
                ):
                    raise AgentResultContractError("Research fact must have a field and string value.")
    elif action == "live_web_unavailable":
        if result.get("status") != "unavailable":
            raise AgentResultContractError("Unavailable research result has invalid status.")
        if not isinstance(result.get("message"), str):
            raise AgentResultContractError("Unavailable research result needs a message.")

    return normalize_specialist_result(result)


def normalize_tutor_result(value: Any) -> dict[str, Any]:
    result = _require_mapping(value, "Tutor")
    if not isinstance(result.get("instruction"), str) or not result["instruction"].strip():
        raise AgentResultContractError("Tutor instruction must be a non-empty string.")
    if not isinstance(result.get("message"), str):
        raise AgentResultContractError("Tutor message must be a string.")
    return normalize_specialist_result(result)


def normalize_analysis_result(value: Any) -> dict[str, Any]:
    result = _require_mapping(value, "Analysis")
    if not isinstance(result.get("instruction"), str) or not result["instruction"].strip():
        raise AgentResultContractError("Analysis instruction must be a non-empty string.")
    if not isinstance(result.get("message"), str):
        raise AgentResultContractError("Analysis message must be a string.")
    return normalize_specialist_result(result)


def normalize_engineering_result(value: Any) -> dict[str, Any]:
    result = _require_mapping(value, "Engineering")
    if not isinstance(result.get("status", "success"), str):
        raise AgentResultContractError("Engineering status must be a string.")
    if "type" in result and not isinstance(result["type"], str):
        raise AgentResultContractError("Engineering type must be a string.")
    if "path" in result and not isinstance(result["path"], str):
        raise AgentResultContractError("Engineering path must be a string.")
    if "entries" in result and not isinstance(result["entries"], list):
        raise AgentResultContractError("Engineering entries must be a list.")
    if "content" in result and not isinstance(result["content"], str):
        raise AgentResultContractError("Engineering content must be a string.")
    return normalize_specialist_result(result)


def normalize_inspection_result(value: Any) -> dict[str, Any]:
    result = _require_mapping(value, "Inspection")
    status = result.get("status")
    if status not in {"green", "degraded"}:
        raise AgentResultContractError("Inspection status is invalid.")
    if result.get("safe") is not True:
        raise AgentResultContractError("Inspection safety marker is invalid.")

    checks = result.get("checks")
    findings = result.get("findings")
    if not isinstance(checks, list) or not checks:
        raise AgentResultContractError("Inspection checks must be a non-empty list.")
    if not isinstance(findings, list):
        raise AgentResultContractError("Inspection findings must be a list.")

    for check in checks:
        if not isinstance(check, Mapping):
            raise AgentResultContractError("Inspection check must be an object.")
        if (
            not isinstance(check.get("name"), str) or not check["name"].strip()
            or check.get("status") not in {"ok", "error"}
            or not isinstance(check.get("detail"), str) or not check["detail"].strip()
        ):
            raise AgentResultContractError("Inspection check evidence is invalid.")

    for finding in findings:
        if not isinstance(finding, Mapping):
            raise AgentResultContractError("Inspection finding must be an object.")
        if (
            finding.get("severity") not in {"info", "warning", "error", "critical"}
            or not isinstance(finding.get("check"), str) or not finding["check"].strip()
            or not isinstance(finding.get("finding"), str) or not finding["finding"].strip()
        ):
            raise AgentResultContractError("Inspection finding evidence is invalid.")

    failed_checks = any(check["status"] != "ok" for check in checks)
    error_findings = any(finding["severity"] in {"error", "critical"} for finding in findings)
    if status == "green" and (failed_checks or findings):
        raise AgentResultContractError("Inspection green status contradicts its evidence.")
    if status == "degraded" and not (failed_checks or findings):
        raise AgentResultContractError("Inspection degraded status has no supporting evidence.")
    if failed_checks and not error_findings:
        raise AgentResultContractError("Failed inspection checks require error findings.")

    return normalize_specialist_result(result)


__all__ = [
    "AgentResultContractError",
    "normalize_specialist_result",
    "normalize_research_result",
    "normalize_tutor_result",
    "normalize_analysis_result",
    "normalize_engineering_result",
    "normalize_inspection_result",
]
