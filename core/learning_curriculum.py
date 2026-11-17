"""Validated, versioned curriculum catalog for Amiki's Learning Engine.

The catalog is instructional content, not an official university syllabus.
Progress and mastery decisions are deliberately kept outside this module.
"""
from copy import deepcopy
import json
from pathlib import Path
from typing import Any


CURRICULUM_DIR = Path(__file__).resolve().parent.parent / "curricula"
DEFAULT_CURRICULUM_ID = "software-engineering-foundations"
CURRICULUM_FILE = CURRICULUM_DIR / f"{DEFAULT_CURRICULUM_ID.replace('-', '_')}.json"


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def validate_curriculum(document: Any) -> dict:
    """Validate curriculum structure, unique IDs, references, and acyclic prerequisites."""
    if not isinstance(document, dict):
        raise ValueError("curriculum must be an object")
    if document.get("schema_version") != 1:
        raise ValueError("unsupported curriculum schema_version")

    _require_text(document.get("curriculum_id"), "curriculum_id")
    _require_text(document.get("title"), "title")
    policy = document.get("completion_policy", {})
    if not isinstance(policy, dict):
        raise ValueError("completion_policy must be an object")
    threshold = policy.get("provisional_pass_threshold", 0.8)
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0 < threshold <= 1
    ):
        raise ValueError("provisional_pass_threshold must be between 0 and 1")

    courses = document.get("courses")
    if not isinstance(courses, list) or not courses:
        raise ValueError("curriculum must contain at least one course")

    seen_ids = set()
    lesson_ids = set()
    lesson_graph = {}
    lesson_count = 0

    def register_id(value, field):
        identifier = _require_text(value, field)
        if identifier in seen_ids:
            raise ValueError(f"duplicate curriculum id: {identifier}")
        seen_ids.add(identifier)
        return identifier

    for course_index, course in enumerate(courses):
        if not isinstance(course, dict):
            raise ValueError(f"courses[{course_index}] must be an object")
        register_id(course.get("id"), f"courses[{course_index}].id")
        _require_text(course.get("title"), f"courses[{course_index}].title")
        modules = course.get("modules")
        if not isinstance(modules, list) or not modules:
            raise ValueError("each course must contain at least one module")

        for module_index, module in enumerate(modules):
            if not isinstance(module, dict):
                raise ValueError("module must be an object")
            register_id(module.get("id"), "module.id")
            _require_text(module.get("title"), "module.title")
            lessons = module.get("lessons")
            if not isinstance(lessons, list) or not lessons:
                raise ValueError("each module must contain at least one lesson")

            for lesson in lessons:
                if not isinstance(lesson, dict):
                    raise ValueError("lesson must be an object")
                lesson_id = register_id(lesson.get("id"), "lesson.id")
                lesson_ids.add(lesson_id)
                lesson_count += 1
                _require_text(lesson.get("title"), f"{lesson_id}.title")
                _require_text(lesson.get("summary"), f"{lesson_id}.summary")
                objectives = lesson.get("objectives")
                if (
                    not isinstance(objectives, list)
                    or not objectives
                    or any(not isinstance(item, str) or not item.strip() for item in objectives)
                ):
                    raise ValueError(f"{lesson_id} must have non-empty objectives")
                prerequisites = lesson.get("prerequisites", [])
                if (
                    not isinstance(prerequisites, list)
                    or any(not isinstance(item, str) or not item.strip() for item in prerequisites)
                ):
                    raise ValueError(f"{lesson_id} prerequisites must be a list of IDs")
                if len(set(prerequisites)) != len(prerequisites):
                    raise ValueError(f"{lesson_id} has duplicate prerequisites")
                if lesson_id in prerequisites:
                    raise ValueError(f"{lesson_id} cannot depend on itself")
                minutes = lesson.get("estimated_minutes")
                if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1:
                    raise ValueError(f"{lesson_id} estimated_minutes must be a positive integer")
                assessment = lesson.get("assessment")
                if (
                    not isinstance(assessment, list)
                    or not assessment
                    or any(not isinstance(item, str) or not item.strip() for item in assessment)
                ):
                    raise ValueError(f"{lesson_id} must define assessment methods")
                lesson_graph[lesson_id] = list(prerequisites)

    for lesson_id, prerequisites in lesson_graph.items():
        for prerequisite in prerequisites:
            if prerequisite not in lesson_ids:
                raise ValueError(
                    f"{lesson_id} references unknown prerequisite: {prerequisite}"
                )

    visiting = set()
    visited = set()

    def visit(lesson_id):
        if lesson_id in visiting:
            raise ValueError("curriculum prerequisites contain a cycle")
        if lesson_id in visited:
            return
        visiting.add(lesson_id)
        for prerequisite in lesson_graph[lesson_id]:
            visit(prerequisite)
        visiting.remove(lesson_id)
        visited.add(lesson_id)

    for lesson_id in lesson_graph:
        visit(lesson_id)

    if lesson_count == 0:
        raise ValueError("curriculum must contain at least one lesson")
    return deepcopy(document)


def load_curriculum(path: Path | str | None = None) -> dict:
    """Load and validate the bundled curriculum without exposing mutable globals."""
    source = Path(path) if path is not None else CURRICULUM_FILE
    try:
        with source.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Learning curriculum could not be loaded safely") from exc
    return validate_curriculum(document)


def get_curricula() -> list[dict]:
    """Return all valid curriculum packs, ordered by stable curriculum ID."""
    if not CURRICULUM_DIR.is_dir():
        raise RuntimeError("Learning curriculum directory is unavailable")
    catalogs = [
        load_curriculum(path)
        for path in sorted(CURRICULUM_DIR.glob("*.json"))
        if path.is_file()
    ]
    ids = [catalog["curriculum_id"] for catalog in catalogs]
    if len(ids) != len(set(ids)):
        raise ValueError("curriculum IDs must be unique across curriculum packs")
    return catalogs


def get_curriculum(curriculum_id: str | None = None) -> dict:
    """Return one validated pack; preserve the original default for legacy callers."""
    if curriculum_id is not None and (
        not isinstance(curriculum_id, str) or not curriculum_id.strip()
    ):
        raise ValueError("curriculum_id must be a non-empty string")
    selected = curriculum_id if curriculum_id is not None else DEFAULT_CURRICULUM_ID
    for catalog in get_curricula():
        if catalog["curriculum_id"] == selected:
            return catalog
    raise ValueError(f"unknown curriculum_id: {selected}")


def get_lessons(curriculum_id: str | None = None) -> list[dict]:
    """Return lessons enriched with their course and module context."""
    catalog = get_curriculum(curriculum_id)
    lessons = []
    for course in catalog["courses"]:
        for module in course["modules"]:
            for lesson in module["lessons"]:
                item = deepcopy(lesson)
                item["course_id"] = course["id"]
                item["course_title"] = course["title"]
                item["module_id"] = module["id"]
                item["module_title"] = module["title"]
                lessons.append(item)
    return lessons


def get_lesson(lesson_id: str, curriculum_id: str | None = None) -> dict | None:
    """Return one lesson by stable ID, or None when it is not in the catalog."""
    if not isinstance(lesson_id, str) or not lesson_id.strip():
        return None
    for lesson in get_lessons(curriculum_id):
        if lesson["id"] == lesson_id:
            return lesson
    return None


def get_available_lessons(completed_lesson_ids=(), curriculum_id: str | None = None) -> list[dict]:
    """Return lessons whose prerequisites are all provisionally completed."""
    completed = set(completed_lesson_ids)
    return [
        lesson
        for lesson in get_lessons(curriculum_id)
        if set(lesson.get("prerequisites", ())).issubset(completed)
    ]


__all__ = [
    "get_available_lessons",
    "get_curricula",
    "get_curriculum",
    "get_lesson",
    "get_lessons",
    "load_curriculum",
    "validate_curriculum",
]
