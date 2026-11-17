"""Read-only Amiki source inspection handler.

This module exposes only an allowlisted subset of the Amiki source tree.
It is deliberately separate from the mutable workspace handler so source
inspection can never widen edit or execute permissions.
"""

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]

# Only application source is exposed. Secrets, config, memory, Android files,
# and arbitrary repository paths remain outside this boundary.
CODEBASE_ROOTS = (
    BASE_DIR / "core",
    BASE_DIR / "interfaces" / "api",
)
MAX_READ_BYTES = 100_000
MAX_DIRECTORY_ENTRIES = 100


def _safe_codebase_path(relative_path="."):
    """Resolve a path only when it stays inside an allowlisted source root."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        return None

    candidate = (BASE_DIR / relative_path).resolve()

    for root in CODEBASE_ROOTS:
        root = root.resolve()
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue

    return None


def _display_relative(path):
    try:
        return path.resolve().relative_to(BASE_DIR).as_posix()
    except ValueError:
        return None


def inspect_codebase(relative_path="."):
    """Safely inspect allowlisted Amiki source without exposing file contents."""
    if relative_path == ".":
        entries = [
            {"name": root.relative_to(BASE_DIR).as_posix(), "type": "directory"}
            for root in CODEBASE_ROOTS
            if root.exists()
        ]
        return {
            "status": "success",
            "type": "source_root",
            "path": ".",
            "scope": "allowlisted_amiki_source",
            "entries": entries,
        }

    target = _safe_codebase_path(relative_path)

    if target is None:
        return {
            "status": "error",
            "error": "Path is outside the allowlisted Amiki source.",
        }

    if not target.exists():
        return {
            "status": "error",
            "error": "Path does not exist.",
            "path": relative_path,
        }

    if target.is_dir():
        entries = []
        for entry in sorted(target.iterdir(), key=lambda item: item.name.lower()):
            if entry.name == "__pycache__":
                continue
            entries.append({
                "name": entry.name,
                "type": "directory" if entry.is_dir() else "file",
            })
            if len(entries) >= MAX_DIRECTORY_ENTRIES:
                break

        return {
            "status": "success",
            "type": "directory",
            "path": _display_relative(target),
            "scope": "allowlisted_amiki_source",
            "entries": entries,
        }

    return {
        "status": "success",
        "type": "file",
        "path": _display_relative(target),
        "scope": "allowlisted_amiki_source",
        "size": target.stat().st_size,
    }


def read_codebase(relative_path, max_bytes=MAX_READ_BYTES):
    """Safely read one UTF-8 text file from the allowlisted source tree."""
    target = _safe_codebase_path(relative_path)

    if target is None:
        return {
            "status": "error",
            "error": "Path is outside the allowlisted Amiki source.",
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
        "path": _display_relative(target),
        "scope": "allowlisted_amiki_source",
        "size": size,
        "content": content,
    }
