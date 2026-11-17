from core.handlers import workspace


def test_execute_workspace_stops_when_combined_output_exceeds_limit(tmp_path):
    script = tmp_path / "noisy.py"
    script.write_text(
        "while True:\n"
        "    print('x' * 8192, flush=True)\n",
        encoding="utf-8",
    )

    result = workspace.execute_workspace(
        "python",
        ["noisy.py"],
        timeout=5,
        root=tmp_path,
    )

    assert result["status"] == "error"
    assert result["error"] == "Command output exceeded the maximum allowed size."
    assert result["max_output_bytes"] == workspace._MAX_SUBPROCESS_OUTPUT_BYTES


def test_workspace_test_runner_caps_pytest_failure_output(tmp_path):
    script = tmp_path / "test_noisy.py"
    script.write_text(
        "def test_failing_with_large_output():\n"
        "    print('x' * 200000)\n"
        "    assert False\n",
        encoding="utf-8",
    )

    result = workspace.run_workspace_tests(
        "test_noisy.py",
        timeout=10,
        root=tmp_path,
    )

    assert result["status"] == "error"
    assert result["error"] == "Tests output exceeded the maximum allowed size."
