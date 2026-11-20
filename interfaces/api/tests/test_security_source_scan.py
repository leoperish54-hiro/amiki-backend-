from pathlib import Path
from types import SimpleNamespace

from core.agents.security import _scan_source_tree


def _context():
    return SimpleNamespace(raise_if_cancelled=lambda: None)


def test_source_scan_detects_high_signal_patterns_without_leaking_secret_values(tmp_path):
    root = Path(tmp_path)
    (root / "core").mkdir()
    (root / "core" / "unsafe.py").write_text(
        'API_KEY = "sk-live-secret-123456789"\n'
        "subprocess.run(command, shell=True)\n",
        encoding="utf-8",
    )
    (root / "core" / "tests").mkdir()
    (root / "core" / "tests" / "fixture.py").write_text(
        'PASSWORD = "test-secret-value-123456"\n',
        encoding="utf-8",
    )
    (root / ".env").write_text(
        'API_KEY="sk-live-secret-123456789"\n',
        encoding="utf-8",
    )

    report = _scan_source_tree(_context(), root)

    assert report["status"] == "findings"
    assert report["files_scanned"] == 1
    categories = {finding["category"] for finding in report["findings"]}
    assert categories == {"credential_literal", "shell_execution"}
    assert all(finding["file"] == "core/unsafe.py" for finding in report["findings"])
    assert "sk-live-secret-123456789" not in str(report)
    assert "test-secret-value-123456" not in str(report)


def test_source_scan_passes_clean_source_and_reports_scope(tmp_path):
    root = Path(tmp_path)
    (root / "core").mkdir()
    (root / "core" / "safe.py").write_text(
        "def add(left, right):\n    return left + right\n",
        encoding="utf-8",
    )

    report = _scan_source_tree(_context(), root)

    assert report["status"] == "pass"
    assert report["files_scanned"] == 1
    assert report["scope"] == "bounded_backend_and_android_source_pattern_scan"
    assert report["findings"] == []
    assert report["truncated"] is False


def test_android_scan_detects_cleartext_and_process_launch_but_allows_loopback_config(tmp_path):
    from core.agents.security import _scan_android_tree

    android_root = Path(tmp_path) / "amiki-android"
    manifest = android_root / "app" / "src" / "main" / "AndroidManifest.xml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '<manifest><application android:usesCleartextTraffic="true"></application></manifest>\n',
        encoding="utf-8",
    )
    java_file = android_root / "app" / "src" / "main" / "java" / "Example.java"
    java_file.parent.mkdir(parents=True)
    java_file.write_text(
        "class Example { void run() { Runtime.getRuntime().exec(command); } }\n",
        encoding="utf-8",
    )
    network_config = android_root / "app" / "src" / "main" / "res" / "xml" / "network_security_config.xml"
    network_config.parent.mkdir(parents=True)
    network_config.write_text(
        '<domain-config cleartextTrafficPermitted="true"><domain>127.0.0.1</domain></domain-config>\n',
        encoding="utf-8",
    )

    report = _scan_android_tree(_context(), android_root)

    assert report["status"] == "findings"
    categories = {finding["category"] for finding in report["findings"]}
    assert "android_cleartext_traffic" in categories
    assert "android_runtime_exec" in categories
    assert "android_backup_policy_implicit" in categories
    assert "android_cleartext_domain" not in categories
    assert all("source" not in finding for finding in report["findings"])


def test_source_scan_reports_android_repository_coverage_explicitly(tmp_path):
    report = _scan_source_tree(_context(), Path(tmp_path))

    assert report["android_scan"]["status"] == "not_available"
    assert "Android source repository was not found" in report["android_scan"]["reason"]


def test_source_scan_ignores_python_comments_and_strings_but_detects_real_calls(tmp_path):
    root = Path(tmp_path)
    (root / "core").mkdir()
    (root / "core" / "examples.py").write_text(
        'description = "shell=True and os.system( are patterns we detect"\n'
        '# subprocess.run(command, shell=True)\n'
        'message = """\n'
        'os.system("not executable")\n'
        'subprocess.run(command, shell=True)\n'
        '"""\n'
        'subprocess.run(command, shell=True)\n'
        'os.system(command)\n',
        encoding="utf-8",
    )

    report = _scan_source_tree(_context(), root)
    findings = report["findings"]

    assert {item["category"] for item in findings} == {
        "shell_execution", "os_system"
    }
    assert [item["line"] for item in findings] == [7, 8]

def test_source_scan_yaml_loader_in_comments_and_strings_does_not_hide_unsafe_load(tmp_path):
    root = Path(tmp_path)
    (root / "core").mkdir()
    (root / "core" / "yaml_example.py").write_text(
        'note = "SafeLoader is mentioned in this string"\n'
        'yaml.load(payload)\n'
        '# SafeLoader is mentioned in this comment\n'
        'yaml.load(other_payload)\n',
        encoding="utf-8",
    )

    report = _scan_source_tree(_context(), root)
    yaml_findings = [
        item for item in report["findings"]
        if item["category"] == "unsafe_yaml_load"
    ]

    assert [item["line"] for item in yaml_findings] == [2, 4]
