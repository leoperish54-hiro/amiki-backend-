"""Workspace capability handler for Amiki."""

from pathlib import Path
from threading import RLock
import re
import heapq


BASE_DIR = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = (BASE_DIR / "workspace").resolve()

_PROJECT_DEFINITIONS = {
    "workspace": ("Workspace", WORKSPACE_ROOT),
    "amiki-backend": ("Amiki Backend", BASE_DIR),
    "amiki-android": ("Amiki Android", BASE_DIR.parent / "amiki-android"),
    "aethel-hawk": ("Aethel-Hawk Flight Sim", BASE_DIR.parent / "Aethel-Hawk-Flight-Sim"),
}
_PROTECTED_SEGMENTS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "secrets",
    "credentials", "memory", "database", "local.properties", ".gradle",
    "build", "dist", "target", ".idea", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
_PROTECTED_SUFFIXES = {".pem", ".key", ".jks", ".keystore", ".p12", ".pfx"}
_WORKSPACE_WRITE_LOCK = RLock()


def list_workspace_projects():
    """List configured and locally-created project roots without exposing absolute paths."""
    projects = []
    for project_id, (name, configured_root) in _PROJECT_DEFINITIONS.items():
        root = WORKSPACE_ROOT if project_id == "workspace" else Path(configured_root)
        projects.append({"id": project_id, "name": name, "available": Path(root).is_dir()})

    projects_root = (WORKSPACE_ROOT / "projects").resolve()
    try:
        projects_root.relative_to(WORKSPACE_ROOT)
    except ValueError:
        return projects
    if projects_root.is_dir():
        for entry in sorted(projects_root.iterdir(), key=lambda item: item.name.lower()):
            slug = entry.name
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", slug) or _is_protected_path(slug):
                continue
            try:
                entry.resolve().relative_to(projects_root)
            except (OSError, ValueError):
                continue
            if entry.is_dir():
                projects.append({
                    "id": "local-" + slug,
                    "name": slug.replace("-", " ").replace("_", " ").title(),
                    "available": True,
                })
    return projects


def resolve_workspace_project(project_id="workspace"):
    """Return a configured or validated local project root, if it exists."""
    definition = _PROJECT_DEFINITIONS.get(project_id)
    if definition is not None:
        configured_root = WORKSPACE_ROOT if project_id == "workspace" else definition[1]
        root = Path(configured_root).resolve()
        return root if root.is_dir() else None

    if not isinstance(project_id, str) or not project_id.startswith("local-"):
        return None
    slug = project_id[len("local-"):]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", slug) or _is_protected_path(slug):
        return None

    projects_root = (WORKSPACE_ROOT / "projects").resolve()
    try:
        projects_root.relative_to(WORKSPACE_ROOT)
        root = (projects_root / slug).resolve()
        root.relative_to(projects_root)
    except ValueError:
        return None
    return root if root.is_dir() else None


def inspect_workspace_git(root=None, timeout=5):
    """Return bounded Git status and diff summaries without changing repository state."""
    import shutil
    import subprocess

    root = Path(root or WORKSPACE_ROOT).resolve()
    git = shutil.which("git")
    if git is None:
        return {
            "status": "success",
            "git_available": False,
            "message": "Git is not installed in the backend environment.",
        }

    try:
        probe = subprocess.run(
            [git, "rev-parse", "--show-toplevel"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "status": "success",
            "git_available": False,
            "message": "This project is not an accessible Git working tree.",
        }

    if probe.returncode != 0:
        return {
            "status": "success",
            "git_available": False,
            "message": "This project is not a Git working tree.",
        }

    commands = {
        "branch_status": [git, "status", "--short", "--branch", "--untracked-files=no"],
        "unstaged_diff_stat": [git, "diff", "--stat", "--"],
        "staged_diff_stat": [git, "diff", "--cached", "--stat", "--"],
    }
    output = {}
    for key, command in commands.items():
        try:
            completed = subprocess.run(
                command,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "error": "Git inspection timed out.",
            }
        except OSError:
            return {
                "status": "error",
                "error": "Git inspection could not be started.",
            }
        if completed.returncode != 0:
            return {
                "status": "error",
                "error": "Git inspection failed.",
            }
        output[key] = (completed.stdout or "").strip()[:8_000]

    return {
        "status": "success",
        "git_available": True,
        "branch_status": output["branch_status"],
        "unstaged_diff_stat": output["unstaged_diff_stat"],
        "staged_diff_stat": output["staged_diff_stat"],
        "read_only": True,
    }


def create_workspace_project(name, template="python"):
    """Create a local starter project under workspace/projects using a fixed template."""
    import shutil

    if template not in {"python", "empty"}:
        return {"status": "error", "error": "Unsupported project template."}
    if not isinstance(name, str) or not name.strip():
        return {"status": "error", "error": "Project name is required."}

    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug or len(slug) > 40:
        return {"status": "error", "error": "Project name must normalize to 1-40 lowercase letters, numbers, or hyphens."}
    if _is_protected_path(slug):
        return {"status": "error", "error": "Project name is reserved."}

    projects_root = (WORKSPACE_ROOT / "projects").resolve()
    try:
        projects_root.relative_to(WORKSPACE_ROOT)
    except ValueError:
        return {"status": "error", "error": "Project storage path is outside the workspace."}

    try:
        projects_root.mkdir(parents=True, exist_ok=True)
        target = (projects_root / slug).resolve()
        target.relative_to(projects_root)
    except (OSError, ValueError):
        return {"status": "error", "error": "Project storage could not be prepared safely."}

    if target.exists():
        return {"status": "error", "error": "Project already exists."}

    created = False
    try:
        target.mkdir()
        created = True
        readme = "# " + slug.replace("-", " ").title() + "\n\nCreated by Amiki Workspace.\n"
        (target / "README.md").write_text(readme, encoding="utf-8")
        if template == "python":
            starter = (
                "def main():\n"
                "    print('Hello from your Amiki project!')\n"
                "\n"
                "if __name__ == '__main__':\n"
                "    main()\n"
            )
            (target / "main.py").write_text(starter, encoding="utf-8")
    except OSError:
        if created:
            try:
                shutil.rmtree(target)
            except OSError:
                pass
        return {"status": "error", "error": "Project could not be safely created."}

    return {
        "status": "success",
        "project_id": "local-" + slug,
        "project_name": slug.replace("-", " ").replace("_", " ").title(),
        "template": template,
        "path": "projects/" + slug,
    }


def read_workspace_git_diff(root=None, staged=False, timeout=5, max_chars=12_000):
    """Return a bounded unified diff for safe paths only; never mutate Git state."""
    import shutil
    import subprocess

    root = Path(root or WORKSPACE_ROOT).resolve()
    git = shutil.which("git")
    if git is None:
        return {"status": "success", "git_available": False, "message": "Git is not installed."}

    try:
        probe = subprocess.run(
            [git, "rev-parse", "--show-toplevel"],
            cwd=str(root), capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "success", "git_available": False, "message": "This project is not an accessible Git working tree."}
    if probe.returncode != 0:
        return {"status": "success", "git_available": False, "message": "This project is not a Git working tree."}

    name_command = [git, "diff", "--name-only", "-z"]
    if staged:
        name_command.insert(2, "--cached")
    try:
        names = subprocess.run(
            name_command, cwd=str(root), capture_output=True, timeout=timeout, check=False,
        )
        if names.returncode != 0:
            return {"status": "error", "error": "Git diff inspection failed."}
        paths = [
            path.decode("utf-8", errors="replace")
            for path in names.stdout.split(bytes([0])) if path
        ]
        safe_paths = [path for path in paths if not _is_protected_path(path)]
        if not safe_paths:
            return {
                "status": "success", "git_available": True, "staged": bool(staged),
                "diff": "", "changed_files": 0, "excluded_sensitive_files": len(paths),
                "truncated": False, "read_only": True,
            }
        command = [git, "diff", "--no-ext-diff", "--no-color"]
        if staged:
            command.append("--cached")
        command.extend(["--", *safe_paths])
        result = subprocess.run(
            command, cwd=str(root), capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Git diff inspection timed out."}
    except OSError:
        return {"status": "error", "error": "Git diff inspection could not be started."}
    if result.returncode != 0:
        return {"status": "error", "error": "Git diff inspection failed."}
    diff = result.stdout or ""
    return {
        "status": "success",
        "git_available": True,
        "staged": bool(staged),
        "diff": diff[:max_chars],
        "changed_files": len(safe_paths),
        "excluded_sensitive_files": len(paths) - len(safe_paths),
        "truncated": len(diff) > max_chars,
        "read_only": True,
    }


def _is_protected_path(relative_path):
    """Keep credentials, VCS internals, caches, and private runtime stores out of Workspace."""
    parts = [part.casefold() for part in str(relative_path).replace("\\", "/").split("/") if part]
    for part in parts:
        if part in _PROTECTED_SEGMENTS or part == ".env" or part.startswith(".env."):
            return True
        if Path(part).suffix.casefold() in _PROTECTED_SUFFIXES:
            return True
    return False


def _safe_path(relative_path=".", root=None):
    """Resolve a project-relative path while enforcing root and sensitive-path boundaries."""
    root = Path(root or WORKSPACE_ROOT).resolve()
    candidate = (root / relative_path).resolve()

    try:
        relative_candidate = candidate.relative_to(root)
    except ValueError:
        return None

    if _is_protected_path(relative_candidate.as_posix()):
        return None

    return candidate


def inspect_workspace(relative_path=".", root=None):
    """Safely inspect a file or directory inside a configured project root."""
    root = Path(root or WORKSPACE_ROOT).resolve()
    target = _safe_path(relative_path, root=root)

    if target is None:
        return {
            "status": "error",
            "error": "Path is outside the workspace.",
        }

    if not target.exists():
        return {
            "status": "error",
            "error": "Path does not exist.",
            "path": relative_path,
        }

    if target.is_dir():
        entry_count = 0

        def visible_entries():
            nonlocal entry_count
            for entry in target.iterdir():
                try:
                    entry_relative = entry.resolve().relative_to(root).as_posix()
                except (OSError, ValueError):
                    continue
                if _is_protected_path(entry_relative):
                    continue
                entry_count += 1
                yield entry

        # Keep only the first 101 visible entries in sorted order. Count the
        # rest without materializing an arbitrarily large directory listing.
        bounded_entries = heapq.nsmallest(
            101, visible_entries(), key=lambda item: item.name.casefold()
        )
        entries = [
            {
                "name": entry.name,
                "type": "directory" if entry.is_dir() else "file",
            }
            for entry in bounded_entries
        ]

        return {
            "status": "success",
            "type": "directory",
            "path": relative_path,
            "entries": entries,
            "entry_count": entry_count,
            "truncated": entry_count > 100,
        }

    return {
        "status": "success",
        "type": "file",
        "path": relative_path,
        "size": target.stat().st_size,
    }


def read_workspace(relative_path, max_bytes=100_000, root=None):
    """Safely read a UTF-8 text file inside a configured project root."""
    root = Path(root or WORKSPACE_ROOT).resolve()
    target = _safe_path(relative_path, root=root)

    if target is None:
        return {
            "status": "error",
            "error": "Path is outside the workspace.",
        }

    if not target.exists():
        return {
            "status": "error",
            "error": "Path does not exist.",
            "path": relative_path,
        }

    if not target.is_file():
        return {
            "status": "error",
            "error": "Path is not a file.",
            "path": relative_path,
        }

    size = target.stat().st_size

    if size > max_bytes:
        return {
            "status": "error",
            "error": "File exceeds the maximum read size.",
            "path": relative_path,
            "size": size,
            "max_bytes": max_bytes,
        }

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {
            "status": "error",
            "error": "File is not valid UTF-8 text.",
            "path": relative_path,
        }

    return {
        "status": "success",
        "type": "file",
        "path": relative_path,
        "size": size,
        "content": content,
    }




def check_workspace_python(relative_path, max_bytes=50_000, root=None):
    """Parse one bounded Python source file without executing it."""
    import ast

    if Path(relative_path).suffix.lower() != ".py":
        return {
            "status": "error",
            "error": "Only Python files can be syntax checked.",
            "path": relative_path,
        }

    result = read_workspace(relative_path, max_bytes=max_bytes, root=root)
    if result.get("status") != "success":
        return result

    try:
        ast.parse(result["content"], filename=relative_path)
    except SyntaxError as exc:
        return {
            "status": "success",
            "check_status": "failed",
            "syntax_valid": False,
            "path": relative_path,
            "line": exc.lineno or 0,
            "column": exc.offset or 0,
            "message": exc.msg,
        }

    return {
        "status": "success",
        "check_status": "passed",
        "syntax_valid": True,
        "path": relative_path,
        "line": 0,
        "column": 0,
        "message": "Python syntax is valid.",
    }


def create_workspace_file(relative_path, content, max_bytes=50_000, root=None):
    """Create a new UTF-8 file inside a configured project root without overwriting."""
    import os

    root = Path(root or WORKSPACE_ROOT).resolve()
    target = _safe_path(relative_path, root=root)
    if target is None:
        return {"status": "error", "error": "Path is outside the workspace."}
    if target.exists():
        return {"status": "error", "error": "Path already exists.", "path": relative_path}
    if not target.parent.exists() or not target.parent.is_dir():
        return {"status": "error", "error": "Parent directory does not exist.", "path": relative_path}
    if not isinstance(content, str):
        return {"status": "error", "error": "File content must be text.", "path": relative_path}
    if len(content.encode("utf-8")) > max_bytes:
        return {
            "status": "error",
            "error": "File content exceeds the maximum write size.",
            "path": relative_path,
            "max_bytes": max_bytes,
        }

    created = False
    try:
        with target.open("x", encoding="utf-8", newline="") as new_file:
            created = True
            new_file.write(content)
            new_file.flush()
            os.fsync(new_file.fileno())
    except FileExistsError:
        return {"status": "error", "error": "Path already exists.", "path": relative_path}
    except OSError:
        if created:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        return {"status": "error", "error": "File could not be safely created.", "path": relative_path}

    return {
        "status": "success",
        "type": "file",
        "path": relative_path,
        "size": target.stat().st_size,
        "created": True,
    }


def _write_workspace_unlocked(relative_path, expected_content, new_content, max_bytes=50_000, root=None):
    """Atomically replace a project file only if its current content matches."""
    import os
    import tempfile

    root = Path(root or WORKSPACE_ROOT).resolve()
    target = _safe_path(relative_path, root=root)
    if target is None:
        return {"status": "error", "error": "Path is outside the workspace."}
    if not target.exists():
        return {"status": "error", "error": "Path does not exist.", "path": relative_path}
    if not target.is_file():
        return {"status": "error", "error": "Path is not a file.", "path": relative_path}
    if not isinstance(expected_content, str) or not isinstance(new_content, str):
        return {"status": "error", "error": "File content must be text.", "path": relative_path}
    if len(new_content.encode("utf-8")) > max_bytes:
        return {
            "status": "error",
            "error": "File content exceeds the maximum write size.",
            "path": relative_path,
            "max_bytes": max_bytes,
        }

    try:
        current_content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"status": "error", "error": "File is not valid UTF-8 text.", "path": relative_path}

    if current_content != expected_content:
        return {
            "status": "error",
            "error": "File changed since it was opened.",
            "path": relative_path,
        }

    temporary_path = None
    try:
        original_mode = target.stat().st_mode & 0o777
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=".amiki-workspace-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(new_content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.chmod(temporary_path, original_mode)

        # Re-check immediately before replacement. The module-level lock
        # serializes Amiki-owned writes; this second read also catches most
        # edits made by external tools while the replacement file was staged.
        try:
            latest_content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {
                "status": "error",
                "error": "File changed since it was opened.",
                "path": relative_path,
            }
        if latest_content != expected_content:
            return {
                "status": "error",
                "error": "File changed since it was opened.",
                "path": relative_path,
            }

        os.replace(temporary_path, target)
        temporary_path = None
    except OSError:
        return {
            "status": "error",
            "error": "File could not be safely written.",
            "path": relative_path,
        }
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    return {
        "status": "success",
        "type": "file",
        "path": relative_path,
        "size": target.stat().st_size,
        "written": True,
    }



def write_workspace(relative_path, expected_content, new_content, max_bytes=50_000, root=None):
    """Serialize Amiki-owned workspace writes and reject stale snapshots."""
    with _WORKSPACE_WRITE_LOCK:
        return _write_workspace_unlocked(
            relative_path,
            expected_content,
            new_content,
            max_bytes=max_bytes,
            root=root,
        )


def edit_workspace(relative_path, old_text, new_text):
    """Replace one exact text occurrence through the shared stale-safe writer."""
    if not isinstance(old_text, str) or not old_text:
        return {
            "status": "error",
            "error": "Old text cannot be empty.",
            "path": relative_path,
        }
    if not isinstance(new_text, str):
        return {
            "status": "error",
            "error": "New text must be a string.",
            "path": relative_path,
        }

    snapshot = read_workspace(relative_path, max_bytes=50_000)
    if snapshot.get("status") != "success":
        return snapshot

    content = snapshot.get("content", "")
    occurrences = content.count(old_text)
    if occurrences == 0:
        return {
            "status": "error",
            "error": "Old text was not found.",
            "path": relative_path,
        }
    if occurrences > 1:
        return {
            "status": "error",
            "error": "Old text occurs multiple times; edit is ambiguous.",
            "path": relative_path,
            "occurrences": occurrences,
        }

    updated_content = content.replace(old_text, new_text, 1)
    result = write_workspace(
        relative_path,
        expected_content=content,
        new_content=updated_content,
        max_bytes=50_000,
    )
    if result.get("status") != "success":
        return result

    return {
        "status": "success",
        "type": "file",
        "path": relative_path,
        "replacement_count": 1,
        "old_text": old_text,
        "new_text": new_text,
    }


_MAX_SUBPROCESS_OUTPUT_BYTES = 64 * 1024


def _run_bounded_subprocess(command, *, cwd, timeout):
    """Run a child process with a hard combined stdout/stderr byte budget."""
    import os
    import signal
    import subprocess
    import threading
    import time

    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        bufsize=0,
        start_new_session=(os.name == "posix"),
    )
    output_lock = threading.Lock()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    total_bytes = 0
    output_exceeded = threading.Event()

    def stop_process():
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass

    def drain(name, stream):
        nonlocal total_bytes
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                with output_lock:
                    remaining = max(0, _MAX_SUBPROCESS_OUTPUT_BYTES - total_bytes)
                    kept = chunk[:remaining]
                    if kept:
                        output[name].extend(kept)
                        total_bytes += len(kept)
                    if len(chunk) > remaining:
                        output_exceeded.set()
                if output_exceeded.is_set():
                    stop_process()
        finally:
            try:
                stream.close()
            except OSError:
                pass

    readers = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + max(0.01, float(timeout))
    timed_out = False
    while process.poll() is None:
        if output_exceeded.is_set():
            stop_process()
            break
        if time.monotonic() >= deadline:
            timed_out = True
            stop_process()
            break
        time.sleep(0.01)

    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        stop_process()
        process.wait()

    for reader in readers:
        reader.join(timeout=2)
    if any(reader.is_alive() for reader in readers):
        stop_process()
        for reader in readers:
            reader.join(timeout=1)

    return {
        "return_code": process.returncode,
        "stdout": bytes(output["stdout"]).decode("utf-8", errors="replace"),
        "stderr": bytes(output["stderr"]).decode("utf-8", errors="replace"),
        "timed_out": timed_out,
        "output_truncated": output_exceeded.is_set(),
    }


def execute_workspace(command, args=None, timeout=10, root=None):
    """Execute an allowlisted development command inside a configured project root."""
    import subprocess

    root = Path(root or WORKSPACE_ROOT).resolve()
    allowed_commands = {"python"}

    if not isinstance(command, str) or not command:
        return {
            "status": "error",
            "error": "Command must be a non-empty string.",
        }

    if command not in allowed_commands:
        return {
            "status": "error",
            "error": "Command is not allowed.",
            "command": command,
        }

    if args is None:
        args = []

    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return {
            "status": "error",
            "error": "Command arguments must be a list of strings.",
        }

    if not args:
        return {
            "status": "error",
            "error": "A Python script path is required.",
        }

    if args[0].startswith("-"):
        return {
            "status": "error",
            "error": "Python options are not allowed.",
        }

    script_path = _safe_path(args[0], root=root)

    if script_path is None:
        return {
            "status": "error",
            "error": "Script path is outside the workspace.",
            "path": args[0],
        }

    if not script_path.exists():
        return {
            "status": "error",
            "error": "Script path does not exist.",
            "path": args[0],
        }

    if not script_path.is_file():
        return {
            "status": "error",
            "error": "Script path is not a file.",
            "path": args[0],
        }

    if script_path.suffix.lower() != ".py":
        return {
            "status": "error",
            "error": "Only Python scripts can be executed.",
            "path": args[0],
        }

    try:
        completed = _run_bounded_subprocess(
            [command, *args],
            cwd=root,
            timeout=timeout,
        )
    except OSError as exc:
        return {
            "status": "error",
            "error": "Command could not be started.",
            "details": str(exc),
            "command": command,
        }

    if completed["timed_out"]:
        return {
            "status": "error",
            "error": "Command timed out.",
            "command": command,
            "timeout": timeout,
        }
    if completed["output_truncated"]:
        return {
            "status": "error",
            "error": "Command output exceeded the maximum allowed size.",
            "command": command,
            "max_output_bytes": _MAX_SUBPROCESS_OUTPUT_BYTES,
        }

    return {
        "status": "success",
        "command": command,
        "args": args,
        "return_code": completed["return_code"],
        "stdout": completed["stdout"],
        "stderr": completed["stderr"],
        "output_truncated": False,
    }



def run_workspace_tests(relative_path, timeout=30, root=None):
    """Run one explicitly approved pytest file without a shell, with bounded time/output."""
    import subprocess
    import sys

    root = Path(root or WORKSPACE_ROOT).resolve()
    target = _safe_path(relative_path, root=root)
    if target is None:
        return {"status": "error", "error": "Script path is outside the workspace.", "path": relative_path}
    if not target.exists():
        return {"status": "error", "error": "Script path does not exist.", "path": relative_path}
    if not target.is_file():
        return {"status": "error", "error": "Script path is not a file.", "path": relative_path}
    if target.suffix.lower() != ".py":
        return {"status": "error", "error": "Only Python test files can be run.", "path": relative_path}
    try:
        target.relative_to(root)
    except ValueError:
        return {"status": "error", "error": "Script path is outside the workspace.", "path": relative_path}

    try:
        completed = _run_bounded_subprocess(
            [
                sys.executable, "-m", "pytest", "-q", "--noconftest",
                "-p", "no:cacheprovider", "--import-mode=importlib",
                str(target.relative_to(root)),
            ],
            cwd=root,
            timeout=timeout,
        )
    except OSError:
        return {"status": "error", "error": "Tests could not be started."}

    if completed["timed_out"]:
        return {"status": "error", "error": "Tests timed out.", "timeout": timeout}
    if completed["output_truncated"]:
        return {
            "status": "error",
            "error": "Tests output exceeded the maximum allowed size.",
            "max_output_bytes": _MAX_SUBPROCESS_OUTPUT_BYTES,
        }

    output = completed["stdout"] + (
        ("\\n" if completed["stdout"] and completed["stderr"] else "")
        + completed["stderr"]
    )
    # Avoid returning absolute backend paths to the mobile client.
    output = output.replace(str(root), "project://workspace")
    return {
        "status": "success",
        "return_code": completed["return_code"],
        "stdout": output[-8_000:],
        "output_truncated": len(output) > 8_000,
        "timeout_seconds": timeout,
        "test_status": "passed" if completed["return_code"] == 0 else "failed",
    }


def _extract_workspace_path(message):
    """Extract a likely workspace-relative path from a user message."""
    tokens = message.strip().split()

    if not tokens:
        return "."

    ignored = {
        "open",
        "read",
        "view",
        "show",
        "list",
        "inspect",
        "the",
        "my",
        "file",
        "workspace",
        "please",
    }

    # Prefer concrete path-like tokens over trailing conversational filler.
    for token in reversed(tokens):
        cleaned = token.strip(".,!?;:()[]{}\"'")

        if not cleaned:
            continue

        if cleaned.lower() in ignored:
            continue

        if (
            "." in cleaned
            or "/" in cleaned
            or "\\" in cleaned
        ):
            return cleaned

    # Preserve the existing fallback for non-path-like targets.
    for token in reversed(tokens):
        cleaned = token.strip(".,!?;:()[]{}\"'")

        if not cleaned:
            continue

        if cleaned.lower() in ignored:
            continue

        return cleaned

    return "."


def handle(message):
    """Prepare a structured workspace capability result."""
    message_lower = message.lower().strip()
    first_word = message_lower.split(maxsplit=1)[0] if message_lower else ""

    if first_word in ("run", "execute", "test"):
        action = "execute"
    elif first_word in ("change", "edit", "modify"):
        action = "edit"
    elif first_word in ("show", "list", "inspect"):
        action = "inspect"
    elif first_word in ("open", "read", "view"):
        action = "read"
    else:
        action = "unknown"

    result = {
        "capability": "workspace",
        "action": action,
        "instruction": "Help the user work with their projects, files, code, and development tasks clearly and carefully.",
        "message": message,
    }

    if action == "inspect":
        result["workspace_result"] = inspect_workspace()

    elif action == "read":
        relative_path = _extract_workspace_path(message)
        result["workspace_result"] = read_workspace(relative_path)

    elif action == "execute":
        tokens = message.strip().split()

        if len(tokens) < 2:
            result["workspace_result"] = execute_workspace("python")
        else:
            relative_path = _extract_workspace_path(message)
            result["workspace_result"] = execute_workspace(
                "python",
                [relative_path],
            )

    return result
