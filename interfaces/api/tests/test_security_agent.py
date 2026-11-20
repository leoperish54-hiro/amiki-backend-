from fastapi.testclient import TestClient
from interfaces.api.tests.uosa_test_client import UosaTestClient

from core.agents.orchestrator import AgentOrchestrator
from core.agents.specialized import build_security_agent
from interfaces.api.server.app import app

OWNER_TOKEN = "uosa-test-owner-sentinel-not-a-real-bearer-token"


client = UosaTestClient(app, OWNER_TOKEN, auto_owner_auth=True)


def test_security_agent_is_bounded_and_read_only():
    definition, manager = build_security_agent()

    assert definition.name == "security"
    assert definition.permissions == {"agent.security.audit"}
    assert definition.tools == {"security_audit"}
    assert set(manager._tools) == {"security_audit"}
    assert all(tool.read_only for tool in manager._tools.values())


def test_security_agent_audit_reports_scope_and_known_device_gap():
    result = AgentOrchestrator().execute_for_agent(
        "security",
        "Audit Amiki's security posture.",
        direct=True,
    )

    assert result.status == "success"
    assert result.completed_steps == 1
    report = result.results[0]
    assert report["scope"] == "bounded_runtime_and_source_policy_audit"
    assert report["status"] == "degraded"
    assert report["safe"] is True
    assert any(
        check["name"] == "device_execution_boundary"
        and check["status"] == "warning"
        for check in report["checks"]
    )
    assert report["limitations"]
    assert report["source_scan"]["scope"] == "bounded_backend_and_android_source_pattern_scan"
    assert report["source_scan"]["files_scanned"] > 0
    checks = {check["name"]: check for check in report["checks"]}
    assert "source_code_patterns" in checks
    assert checks["android_source_coverage"]["status"] in {"ok", "warning"}
    assert "android_scan" in report["source_scan"]
    assert report["source_scan"]["android_scan"]["status"] in {"pass", "findings", "not_available"}
    assert checks["device_action_approval_declarations"]["status"] == "ok"
    assert checks["backend_device_confirmation"]["status"] == "ok"
    assert checks["device_execution_permission"]["status"] == "ok"


def test_security_agent_is_exposed_through_direct_api():
    capabilities = client.get("/api/v1/agents/capabilities")
    assert capabilities.status_code == 200
    agents = {item["agent"]: item for item in capabilities.json()["agents"]}
    assert "security" in agents
    assert agents["security"]["direct_access"] is True
    assert agents["security"]["read_only"] is True

    response = client.post(
        "/api/v1/agents/run",
        json={
            "agent": "security",
            "input": "Audit Amiki's security posture.",
            "context": {"focus": "agent permissions", "scope": "runtime"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["agent"] == "security"
    assert payload["results"][0]["scope"] == "bounded_runtime_and_source_policy_audit"


def test_security_agent_rejects_unapproved_context_fields():
    response = client.post(
        "/api/v1/agents/run",
        json={
            "agent": "security",
            "input": "Audit Amiki.",
            "context": {"shell_command": "whoami"},
        },
    )
    assert response.status_code == 400
