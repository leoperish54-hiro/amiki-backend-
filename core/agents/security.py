"""Read-only, bounded security posture audit for Amiki V1.

This agent reports evidence about the runtime's declared tool boundaries and the
currently exposed device-plan contract. It is not a vulnerability scanner and
must never be treated as proof that the entire application is secure.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
import re
import tokenize
from typing import Any

from core.agents.device import device_capabilities
from core.integrations.cipher_client import CipherClient, CipherIntegrationError
from core.agents.manager import AgentExecutionContext, AgentTool


_SECRET_ASSIGNMENT = re.compile(
    r"""(?i)\b(?:api[_-]?key|access[_-]?token|secret(?:[_-]?key)?|password)\b\s*[:=]\s*(['"])([^'"]{8,})\1"""
)
_SCAN_EXTENSIONS = {".py", ".sh", ".yml", ".yaml", ".toml", ".txt"}
_SKIP_PARTS = {".git", ".venv", "__pycache__", "tests", "test", "node_modules"}
_MAX_SCAN_FILES = 300
_MAX_SCAN_FILE_BYTES = 256_000
_MAX_SCAN_TOTAL_BYTES = 2_000_000
_MAX_REPORTED_FINDINGS = 30


_ANDROID_SCAN_EXTENSIONS = {
    ".java", ".kt", ".xml", ".gradle", ".kts", ".properties", ".yml", ".yaml",
}


def _scan_android_tree(
    context: AgentExecutionContext,
    android_root: Path | None,
) -> dict[str, Any]:
    """Inspect Android production source and manifest/configuration when available."""
    if android_root is None or not android_root.is_dir():
        return {
            "status": "not_available",
            "scope": "bounded_android_source_pattern_scan",
            "files_scanned": 0,
            "bytes_scanned": 0,
            "truncated": False,
            "findings": [],
            "reason": "Android source repository was not found; set AMIKI_ANDROID_ROOT if it is stored elsewhere.",
        }

    roots = [
        android_root / "app" / "src" / "main",
        android_root / ".github" / "workflows",
    ]
    candidates: set[Path] = set()
    for name in (
        "build.gradle.kts", "settings.gradle.kts", "gradle.properties",
        "app/build.gradle.kts",
    ):
        path = android_root / name
        if path.is_file():
            candidates.add(path)
    for scan_root in roots:
        if not scan_root.is_dir():
            continue
        try:
            for path in scan_root.rglob("*"):
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.suffix.lower() in _ANDROID_SCAN_EXTENSIONS
                    and not any(part in _SKIP_PARTS or part in {"build", ".gradle"} for part in path.parts)
                ):
                    candidates.add(path)
        except OSError:
            continue

    findings: list[dict[str, Any]] = []
    files_scanned = 0
    bytes_scanned = 0
    truncated = False
    manifest_seen = False

    def report(category: str, severity: str, path: Path, line_number: int, summary: str):
        findings.append({
            "category": category,
            "severity": severity,
            "file": "amiki-android/" + path.relative_to(android_root).as_posix(),
            "line": line_number,
            "finding": summary,
        })

    for path in sorted(candidates):
        context.raise_if_cancelled()
        if files_scanned >= 200:
            truncated = True
            break
        try:
            size = path.stat().st_size
            if size > _MAX_SCAN_FILE_BYTES or bytes_scanned + size > 1_500_000:
                truncated = True
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            truncated = True
            continue

        files_scanned += 1
        bytes_scanned += size
        if path.name == "AndroidManifest.xml":
            manifest_seen = True
        relative_path = "amiki-android/" + path.relative_to(android_root).as_posix()
        domain_values = re.findall(
            r"<domain\b[^>]*>\s*([^<]+?)\s*</domain>",
            source,
            flags=re.IGNORECASE,
        )
        local_only_cleartext = (
            path.name == "network_security_config.xml"
            and bool(domain_values)
            and all(value.strip().lower() in {"127.0.0.1", "localhost", "::1"} for value in domain_values)
        )

        for line_number, line in enumerate(source.splitlines(), 1):
            stripped = line.lstrip()
            if not stripped or stripped.startswith(("#", "//", "<!--")):
                continue

            secret_match = _SECRET_ASSIGNMENT.search(line)
            if secret_match:
                literal = secret_match.group(2).strip().lower()
                if literal not in {
                    "changeme", "change-me", "your_api_key", "your-secret",
                    "placeholder", "none", "null", "example",
                } and not literal.startswith(("${", "<your")):
                    report(
                        "credential_literal",
                        "high",
                        path,
                        line_number,
                        "Credential-like literal assignment detected; the value is suppressed.",
                    )

            if re.search(r"""android:debuggable\s*=\s*['"]true['"]""", line, re.IGNORECASE):
                report(
                    "android_debuggable",
                    "high",
                    path,
                    line_number,
                    "Android manifest explicitly enables debuggable mode.",
                )
            if re.search(r"""android:usesCleartextTraffic\s*=\s*['"]true['"]""", line, re.IGNORECASE):
                report(
                    "android_cleartext_traffic",
                    "high",
                    path,
                    line_number,
                    "Android manifest explicitly permits cleartext traffic globally.",
                )
            if (
                re.search(r"""cleartextTrafficPermitted\s*=\s*['"]true['"]""", line, re.IGNORECASE)
                and not local_only_cleartext
            ):
                report(
                    "android_cleartext_domain",
                    "high",
                    path,
                    line_number,
                    "A network security config permits cleartext traffic outside a verified loopback-only rule.",
                )
            if re.search(r"Runtime\.getRuntime\(\)\.exec\s*\(", line):
                report(
                    "android_runtime_exec",
                    "high",
                    path,
                    line_number,
                    "Runtime.exec() launches a process; review whether a fixed, permission-scoped API can be used instead.",
                )
            if re.search(r"new\s+ProcessBuilder\s*\(", line):
                report(
                    "android_process_builder",
                    "medium",
                    path,
                    line_number,
                    "ProcessBuilder launches a process; verify all arguments are fixed or safely separated.",
                )
            if re.search(r"setJavaScriptEnabled\s*\(\s*true\s*\)", line):
                report(
                    "android_webview_javascript",
                    "medium",
                    path,
                    line_number,
                    "WebView JavaScript is enabled; verify content is trusted and JavaScript bridges are constrained.",
                )
            if re.search(r"""android:allowBackup\s*=\s*['"]true['"]""", line, re.IGNORECASE):
                report(
                    "android_backup_enabled",
                    "medium",
                    path,
                    line_number,
                    "Android app backup is explicitly enabled; review whether private companion data should be backed up.",
                )

        if len(findings) >= _MAX_REPORTED_FINDINGS:
            truncated = True
            break

    if manifest_seen:
        manifest_path = android_root / "app" / "src" / "main" / "AndroidManifest.xml"
        try:
            manifest = manifest_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            manifest = ""
        if manifest and not re.search(r"android:allowBackup\s*=", manifest):
            report(
                "android_backup_policy_implicit",
                "medium",
                manifest_path,
                1,
                "Backup policy is implicit; review whether private companion data should be excluded from device backups.",
            )

    return {
        "status": "findings" if findings else "pass",
        "scope": "bounded_android_source_pattern_scan",
        "files_scanned": files_scanned,
        "bytes_scanned": bytes_scanned,
        "truncated": truncated,
        "findings": findings[:_MAX_REPORTED_FINDINGS],
    }


def _mask_python_non_code(source: str) -> str | None:
    """Mask Python comments and string literals in linear time.

    Line contents and absolute offsets are prepared once. This avoids repeatedly
    splitting the source and summing preceding line lengths for every token.
    Return None if tokenization fails so callers retain their fallback behavior.
    """
    masked = list(source)
    lines = source.splitlines(keepends=True)
    line_lengths = [len(line.rstrip("\r\n")) for line in lines]
    line_starts: list[int] = []
    offset = 0
    for line in lines:
        line_starts.append(offset)
        offset += len(line)

    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            if token.type not in (tokenize.STRING, tokenize.COMMENT):
                continue

            (start_row, start_col), (end_row, end_col) = token.start, token.end
            for row in range(start_row, min(end_row, len(lines)) + 1):
                line_index = row - 1
                start = start_col if row == start_row else 0
                end = end_col if row == end_row else line_lengths[line_index]
                line_start = line_starts[line_index]
                for col in range(start, min(end, line_lengths[line_index])):
                    index = line_start + col
                    if index < len(masked) and masked[index] not in "\r\n":
                        masked[index] = " "
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None

    return "".join(masked)


def _scan_source_tree(
    context: AgentExecutionContext,
    root: Path | None = None,
    android_root: Path | None = None,
) -> dict[str, Any]:
    """Scan bounded production-source files for a small set of high-signal patterns."""
    root = root or Path(__file__).resolve().parents[2]
    roots = [
        root / "core",
        root / "interfaces" / "api",
        root / "scripts",
        root / ".github" / "workflows",
    ]
    candidates: set[Path] = set()
    for scan_root in roots:
        if not scan_root.is_dir():
            continue
        try:
            for path in scan_root.rglob("*"):
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.suffix.lower() in _SCAN_EXTENSIONS
                    and not any(part in _SKIP_PARTS for part in path.parts)
                    and not path.name.startswith(".env")
                ):
                    candidates.add(path)
        except OSError:
            continue

    for path in root.iterdir() if root.is_dir() else []:
        if (
            path.is_file()
            and not path.is_symlink()
            and (path.name.startswith("requirements") or path.name == "pyproject.toml")
            and path.suffix.lower() in _SCAN_EXTENSIONS
        ):
            candidates.add(path)

    findings: list[dict[str, Any]] = []
    files_scanned = 0
    bytes_scanned = 0
    truncated = False

    def report(category: str, severity: str, relative_path: str, line_number: int, summary: str):
        findings.append({
            "category": category,
            "severity": severity,
            "file": relative_path,
            "line": line_number,
            "finding": summary,
        })

    for path in sorted(candidates):
        context.raise_if_cancelled()
        if files_scanned >= _MAX_SCAN_FILES:
            truncated = True
            break
        try:
            size = path.stat().st_size
            if size > _MAX_SCAN_FILE_BYTES or bytes_scanned + size > _MAX_SCAN_TOTAL_BYTES:
                truncated = True
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            truncated = True
            continue

        files_scanned += 1
        bytes_scanned += size
        relative_path = path.relative_to(root).as_posix()
        source_lines = source.splitlines()

        # Mask comments and string contents in Python code before applying
        # executable-code patterns. Keep original lines for credential checks.
        code_lines = source_lines
        if path.suffix.lower() == ".py":
            masked_source = _mask_python_non_code(source)
            if masked_source is not None:
                code_lines = masked_source.splitlines()

        for line_number, line in enumerate(source_lines, 1):
            stripped = line.lstrip()
            if not stripped or stripped.startswith(("#", "//")):
                continue

            code_line = (
                code_lines[line_number - 1]
                if line_number <= len(code_lines)
                else line
            )

            secret_match = _SECRET_ASSIGNMENT.search(line)
            if secret_match:
                literal = secret_match.group(2).strip().lower()
                if literal not in {
                    "changeme", "change-me", "your_api_key", "your-secret",
                    "placeholder", "none", "null", "example",
                } and not literal.startswith(("${", "<your")):
                    report(
                        "credential_literal",
                        "high",
                        relative_path,
                        line_number,
                        "Credential-like literal assignment detected; the value is suppressed.",
                    )

            if re.search(r"\bshell\s*=\s*True\b", code_line):
                report(
                    "shell_execution",
                    "high",
                    relative_path,
                    line_number,
                    "A subprocess call enables shell=True; review input handling and command boundaries.",
                )
            if re.search(r"\bos\.system\s*\(", code_line):
                report(
                    "os_system",
                    "high",
                    relative_path,
                    line_number,
                    "os.system() invokes a shell command; prefer a fixed argument-array subprocess call.",
                )
            if re.search(r"\bpickle\.(?:loads|load)\s*\(", code_line):
                report(
                    "unsafe_deserialization",
                    "high",
                    relative_path,
                    line_number,
                    "Pickle deserialization can execute code when input is untrusted.",
                )
            if re.search(r"\byaml\.load\s*\(", code_line) and "SafeLoader" not in code_line:
                report(
                    "unsafe_yaml_load",
                    "high",
                    relative_path,
                    line_number,
                    "yaml.load() appears without an inline SafeLoader declaration.",
                )
            if re.search(r"\bverify\s*=\s*False\b", code_line):
                report(
                    "tls_verification_disabled",
                    "high",
                    relative_path,
                    line_number,
                    "TLS certificate verification is disabled for a request.",
                )
            if "FastAPI(" in line and re.search(r"\bdebug\s*=\s*True\b", code_line):
                report(
                    "debug_mode",
                    "medium",
                    relative_path,
                    line_number,
                    "FastAPI debug mode appears enabled in source.",
                )

            if len(findings) >= _MAX_REPORTED_FINDINGS:
                truncated = True
                break
        if len(findings) >= _MAX_REPORTED_FINDINGS:
            break

    if android_root is None:
        configured_android_root = os.environ.get("AMIKI_ANDROID_ROOT")
        android_root = (
            Path(configured_android_root).expanduser()
            if configured_android_root
            else root.parent / "amiki-android"
        )
    android_scan = _scan_android_tree(context, android_root)
    combined_findings = (findings + android_scan["findings"])[:_MAX_REPORTED_FINDINGS]
    repositories_scanned = ["backend"]
    if android_scan["status"] != "not_available":
        repositories_scanned.append("android")

    return {
        "status": "findings" if combined_findings else "pass",
        "scope": "bounded_backend_and_android_source_pattern_scan",
        "repositories_scanned": repositories_scanned,
        "files_scanned": files_scanned + android_scan["files_scanned"],
        "bytes_scanned": bytes_scanned + android_scan["bytes_scanned"],
        "truncated": truncated or android_scan["truncated"],
        "findings": combined_findings,
        "android_scan": android_scan,
        "patterns": [
            "credential-like literal assignments",
            "shell=True and os.system",
            "pickle and unsafe YAML deserialization",
            "disabled TLS verification",
            "FastAPI debug mode",
            "Android cleartext/debug settings",
            "Android process launches and WebView JavaScript",
            "Android backup policy",
        ],
    }


def security_audit(_arguments: dict[str, Any], context: AgentExecutionContext) -> dict[str, Any]:
    """Inspect registered agent boundaries and expose known coverage gaps."""
    context.raise_if_cancelled()

    # Import lazily to avoid a module cycle: specialized agents register this
    # audit tool, while the audit itself inspects those same registrations.
    from core.agents.specialized import (
        build_analysis_agent,
        build_device_agent,
        build_direct_analysis_agent,
        build_direct_engineering_agent,
        build_direct_tutor_agent,
        build_engineering_agent,
        build_inspection_agent,
        build_research_agent,
        build_security_agent,
        build_tutor_agent,
    )

    builders = {
        "research": build_research_agent,
        "tutor": build_tutor_agent,
        "engineering": build_engineering_agent,
        "analysis": build_analysis_agent,
        "inspection": build_inspection_agent,
        "device": build_device_agent,
        "security": build_security_agent,
    }
    direct_builders = {
        "research": build_research_agent,
        "tutor": build_direct_tutor_agent,
        "engineering": build_direct_engineering_agent,
        "analysis": build_direct_analysis_agent,
        "inspection": build_inspection_agent,
        "security": build_security_agent,
    }

    checks: list[dict[str, str]] = []
    findings: list[dict[str, str]] = []

    def add_check(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})
        if status in {"warning", "error"}:
            findings.append({
                "severity": "high" if status == "error" else "medium",
                "check": name,
                "finding": detail,
            })

    boundary_errors: list[str] = []
    for agent_name, builder in builders.items():
        try:
            definition, manager = builder()
            for tool_name, tool in manager._tools.items():
                if tool_name not in manager._allowed_tools:
                    boundary_errors.append(f"{agent_name}.{tool_name}: tool is not allowlisted")
                if tool.permission not in manager._allowed_permissions:
                    boundary_errors.append(f"{agent_name}.{tool_name}: permission is not allowlisted")
                if not tool.read_only and tool.permission in {
                    "agent.read", "agent.web.read", "agent.study.read",
                    "agent.analysis.read", "agent.diagnostics.read",
                    "agent.security.audit",
                }:
                    boundary_errors.append(f"{agent_name}.{tool_name}: mutating tool has a read-style permission")
            if definition.tools != manager._allowed_tools:
                boundary_errors.append(f"{agent_name}: declared tools differ from manager allowlist")
            if definition.permissions != manager._allowed_permissions:
                boundary_errors.append(f"{agent_name}: declared permissions differ from manager allowlist")
        except Exception:
            boundary_errors.append(f"{agent_name}: agent policy could not be inspected safely")

    if boundary_errors:
        add_check("agent_tool_boundaries", "error", "; ".join(boundary_errors[:12]))
    else:
        add_check(
            "agent_tool_boundaries",
            "ok",
            f"Inspected tool and permission declarations for {len(builders)} registered agent builders.",
        )

    direct_mutations: list[str] = []
    for agent_name, builder in direct_builders.items():
        try:
            _definition, manager = builder()
            for tool_name, tool in manager._tools.items():
                if not tool.read_only:
                    direct_mutations.append(f"{agent_name}.{tool_name}")
        except Exception:
            direct_mutations.append(f"{agent_name}.<policy unavailable>")

    if direct_mutations:
        add_check(
            "direct_agent_read_only",
            "error",
            "Direct-access agents expose non-read-only tools: " + ", ".join(direct_mutations[:12]),
        )
    else:
        add_check(
            "direct_agent_read_only",
            "ok",
            f"All {len(direct_builders)} direct-access agent builders expose read-only tools.",
        )

    device = device_capabilities()
    actions = device.get("actions", {})
    expected_approval = ("open_url", "open_app", "speak", "share_text", "call_contact", "set_alarm")
    missing_approval = [
        action for action in expected_approval
        if not isinstance(actions.get(action), dict)
        or actions[action].get("user_approval") is not True
    ]
    if missing_approval:
        add_check(
            "device_action_approval_declarations",
            "error",
            "Approval is not declared for: " + ", ".join(missing_approval),
        )
    else:
        add_check(
            "device_action_approval_declarations",
            "ok",
            "Interactive URL, app-launch, speech, sharing, contact-call, and alarm plans declare user approval.",
        )

    backend_actions = device.get("backend_approved_execution", {})
    expected_backend_actions = ("open_url", "open_app", "speak", "set_alarm", "share_text", "call_contact")
    missing_confirmation = [
        action for action in expected_backend_actions
        if not isinstance(backend_actions.get(action), dict)
        or backend_actions[action].get("requires_user_confirmation") is not True
    ]
    if missing_confirmation:
        add_check(
            "backend_device_confirmation",
            "error",
            "Backend device actions lack an explicit confirmation declaration: "
            + ", ".join(missing_confirmation),
        )
    else:
        add_check(
            "backend_device_confirmation",
            "ok",
            "URL, app-launch, speech, and alarm execution declare on-device confirmation.",
        )

    try:
        _device_definition, device_manager = build_device_agent()
        execution_tool = device_manager._tools.get("device_execute")
        execution_policy_ok = (
            execution_tool is not None
            and execution_tool.read_only is False
            and execution_tool.permission == "agent.device.execute"
        )
    except Exception:
        execution_policy_ok = False
    add_check(
        "device_execution_permission",
        "ok" if execution_policy_ok else "error",
        (
            "Interactive execution uses a dedicated non-read-only permission."
            if execution_policy_ok
            else "Interactive device execution is missing its dedicated execution permission."
        ),
    )

    add_check(
        "device_execution_boundary",
        "warning",
        "A persistent device-bridge heartbeat is not maintained, and command acceptance does not independently verify the final device outcome; Android may still restrict some activity launches.",
    )

    context.raise_if_cancelled()
    source_scan = _scan_source_tree(context)
    source_findings = source_scan["findings"]
    if source_scan["status"] == "unavailable" or source_scan["files_scanned"] == 0:
        add_check(
            "source_scan_coverage",
            "warning",
            "No production source files could be scanned; source-level security coverage is unknown.",
        )
    elif source_findings:
        severity = "error" if any(item["severity"] == "high" for item in source_findings) else "warning"
        add_check(
            "source_code_patterns",
            severity,
            f"Detected {len(source_findings)} potential issue(s) in the bounded source scan.",
        )
        for item in source_findings:
            findings.append({
                "severity": item["severity"],
                "check": "source_code_patterns",
                "finding": f"{item['file']}:{item['line']} {item['category']}: {item['finding']}",
            })
    else:
        add_check(
            "source_code_patterns",
            "ok",
            f"Scanned {source_scan['files_scanned']} production source files; no configured patterns matched.",
        )

    android_scan = source_scan["android_scan"]
    if android_scan["status"] == "not_available":
        add_check(
            "android_source_coverage",
            "warning",
            android_scan.get(
                "reason",
                "Android source repository was not found; Android source security coverage is unknown.",
            ),
        )
    else:
        add_check(
            "android_source_coverage",
            "ok",
            f"Scanned {android_scan['files_scanned']} Android source/configuration files.",
        )

    if source_scan["truncated"]:
        add_check(
            "source_scan_coverage",
            "warning",
            "The source scan reached a file, size, or finding limit; not all eligible source files were checked.",
        )

    # Optional remote Cipher inspection. The local audit remains independent.
    cipher_service: dict[str, Any] | None = None
    cipher_url = os.environ.get("CIPHER_API_URL")
    cipher_token = os.environ.get("CIPHER_API_TOKEN")
    cipher_root_id = os.environ.get("CIPHER_ROOT_ID")
    if any((cipher_url, cipher_token, cipher_root_id)):
        if not all((cipher_url, cipher_token, cipher_root_id)):
            cipher_service = {"status": "configuration_incomplete"}
            add_check("cipher_service", "warning", "Cipher integration settings are incomplete; remote inspection was skipped.")
        else:
            try:
                context.raise_if_cancelled()
                cipher = CipherClient(cipher_url, cipher_token, timeout=3.0)
                health = cipher.health()
                if health.get("status") != "ok" or health.get("audit_chain_valid") is not True:
                    cipher_service = {"status": "degraded", "health": health}
                    add_check("cipher_service", "warning", "Cipher is degraded or its audit chain is not verified; remote inspection was skipped.")
                else:
                    report = cipher.inspect(cipher_root_id)
                    cipher_service = {
                        "status": "complete" if report["complete"] else "partial",
                        "root_id": cipher_root_id,
                        "files_scanned": report["files_scanned"],
                        "bytes_scanned": report["bytes_scanned"],
                        "findings": report["findings"][:30],
                        "complete": report["complete"],
                        "limit_reason": report.get("limit_reason"),
                    }
                    add_check(
                        "cipher_service",
                        "ok" if report["complete"] else "warning",
                        f"Cipher inspected registered root {cipher_root_id}; {len(report['findings'])} finding(s) returned."
                        if report["complete"]
                        else "Cipher inspection was partial; the result is not a clean pass.",
                    )
            except (CipherIntegrationError, ValueError):
                cipher_service = {"status": "unavailable"}
                add_check("cipher_service", "warning", "Cipher could not complete the configured remote inspection; no fallback was attempted.")
    context.raise_if_cancelled()

    has_errors = any(check["status"] == "error" for check in checks)
    has_warnings = any(check["status"] == "warning" for check in checks)
    return {
        "status": "degraded" if has_errors or has_warnings else "pass",
        "scope": "bounded_runtime_and_source_policy_audit",
        "source_scan": source_scan,
        "cipher_service": cipher_service,
        "checks": checks,
        "findings": findings,
        "limitations": [
            "The source scan uses a small set of high-signal patterns; it is not a full SAST engine or dependency vulnerability database audit.",
            "It does not prove that all Android permissions or approval prompts are enforced on-device.",
            "A clean result is not proof that Amiki is secure.",
        ],
        "safe": True,
    }


def build_security_tool() -> AgentTool:
    return AgentTool(
        handler=security_audit,
        permission="agent.security.audit",
        read_only=True,
        timeout_seconds=10.0,
        pass_context=True,
    )
