"""Read-only Git metadata handler for bounded engineering inspection.

Only the repository HEAD commit is exposed. This deliberately avoids
arbitrary Git commands, mutation, branch operations, or shell execution.
"""

import subprocess

from core.handlers.codebase import BASE_DIR


GIT_TIMEOUT_SECONDS = 2.0


def get_head_commit():
    """Return the current repository HEAD commit without allowing mutations."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=BASE_DIR,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "status": "error",
            "type": "git_metadata",
            "error": "Git metadata is unavailable.",
        }

    if completed.returncode != 0:
        return {
            "status": "error",
            "type": "git_metadata",
            "error": "Git metadata is unavailable.",
        }

    commit = completed.stdout.strip()
    if not commit:
        return {
            "status": "error",
            "type": "git_metadata",
            "error": "Git HEAD commit is unavailable.",
        }

    return {
        "status": "success",
        "type": "git_metadata",
        "metadata": "head_commit",
        "commit": commit,
    }
