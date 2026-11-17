"""Amiki Learning Engine (ALE).

Stores explicit feedback, reviewed knowledge, provisionally completed curriculum
lessons, verified skills, and evaluations separately from conversational memory.
This module does not modify model weights.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from threading import RLock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
with open(CONFIG_FILE, "r", encoding="utf-8") as _file:
    _CONFIG = json.load(_file)
LEARNING_CONFIG = _CONFIG.get("learning", {})
LEARNING_ENABLED = LEARNING_CONFIG.get("enabled", True)
STORE_FILE = os.path.join(
    BASE_DIR, LEARNING_CONFIG.get("store_file", "memory/learning_store.json")
)
MAX_RECORDS = max(50, int(LEARNING_CONFIG.get("max_records", 2000)))
SCHEMA_VERSION = 4
DEFAULT_PASS_THRESHOLD = 0.8
_LOCK = RLock()
_CORRECTION_MARKERS = (
    "that's incorrect", "that is incorrect", "you are wrong", "you're wrong",
    "you made a mistake", "incorrect answer", "the correct answer is",
    "correction:", "you misunderstood",
)
_STOPWORDS = {
    "about", "after", "again", "also", "and", "are", "can", "could",
    "does", "from", "have", "help", "into", "just", "more", "please",
    "that", "the", "their", "there", "this", "what", "when", "where",
    "which", "with", "would", "your",
}
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_+-]{2,}")


class LearningStoreError(RuntimeError):
    """The persistent learning store is unreadable or has an unsupported schema."""


class LearningCapacityError(RuntimeError):
    """A bounded learning collection is full and cannot safely discard protected data."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _empty_store():
    return {
        "schema_version": SCHEMA_VERSION,
        "experiences": [],
        "knowledge": [],
        "claims": [],
        "projects": [],
        "skills": [],
        "evaluations": [],
        "training_examples": [],
        "lesson_attempts": [],
    }


def _load():
    """Load the store without converting corruption into an empty learning history."""
    if not LEARNING_ENABLED or not os.path.exists(STORE_FILE):
        return _empty_store()

    try:
        with open(STORE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise LearningStoreError(
            "Learning store could not be read; the original file was left untouched."
        ) from exc

    if not isinstance(data, dict):
        raise LearningStoreError(
            "Learning store root must be an object; the original file was left untouched."
        )

    version = data.get("schema_version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise LearningStoreError("Learning store has an invalid schema version.")
    if version > SCHEMA_VERSION:
        raise LearningStoreError(
            f"Learning store schema {version} is newer than supported schema {SCHEMA_VERSION}."
        )

    # Preserve unknown top-level fields so a newer writer's data is not erased
    # by a reader that knows only the current collections.
    migrated = dict(data)
    defaults = _empty_store()
    for key in defaults:
        if key == "schema_version":
            continue
        value = migrated.get(key, [])
        if not isinstance(value, list) or any(
            not isinstance(record, dict) for record in value
        ):
            raise LearningStoreError(
                f"Learning store collection '{key}' is invalid; original file preserved."
            )
        migrated[key] = value
    migrated["schema_version"] = SCHEMA_VERSION
    return migrated


def _save(data):
    """Persist using an atomic replace; never expose a partially written JSON file."""
    if not LEARNING_ENABLED:
        return False
    directory = os.path.dirname(STORE_FILE)
    os.makedirs(directory, exist_ok=True)
    data["schema_version"] = SCHEMA_VERSION
    fd, temporary = tempfile.mkstemp(
        prefix=".learning-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, STORE_FILE)
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some mobile filesystems do not support directory fsync.
            pass
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return True


def _text(value, field, limit=4000):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return value


def _id(kind, content):
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return f"{kind}_{digest}"


def _append_bounded(data, collection, item, protected_statuses=()):
    """Append without silently evicting pending or approved learning records."""
    records = data[collection]
    if len(records) >= MAX_RECORDS:
        removable = next(
            (
                index for index, record in enumerate(records)
                if record.get("status") not in protected_statuses
            ),
            None,
        )
        if removable is None:
            raise LearningCapacityError(
                f"Learning collection '{collection}' reached its safe capacity."
            )
        del records[removable]
    records.append(item)


def _attempt_curriculum_id(record):
    """Read a namespaced curriculum ID; old records belong to the original pack."""
    from core.learning_curriculum import DEFAULT_CURRICULUM_ID

    value = record.get("curriculum_id", DEFAULT_CURRICULUM_ID)
    return value if isinstance(value, str) else None


def _append_lesson_attempt(data, item, threshold, valid_lesson_keys, pass_thresholds):
    """Bound attempt history while preserving each pack's only passing result."""
    records = data["lesson_attempts"]
    if len(records) >= MAX_RECORDS:
        passing_counts = {}
        for record in records:
            lesson_id = record.get("lesson_id")
            curriculum_id = _attempt_curriculum_id(record)
            score = record.get("score")
            record_threshold = pass_thresholds.get(curriculum_id, threshold)
            if (
                isinstance(lesson_id, str)
                and (curriculum_id, lesson_id) in valid_lesson_keys
                and isinstance(score, (int, float))
                and not isinstance(score, bool)
                and score >= record_threshold
            ):
                key = (curriculum_id, lesson_id)
                passing_counts[key] = passing_counts.get(key, 0) + 1

        removable = next(
            (
                index for index, record in enumerate(records)
                if (
                    not isinstance(record.get("lesson_id"), str)
                    or (
                        _attempt_curriculum_id(record),
                        record.get("lesson_id"),
                    ) not in valid_lesson_keys
                    or not isinstance(record.get("score"), (int, float))
                    or isinstance(record.get("score"), bool)
                    or record.get("score") < pass_thresholds.get(
                        _attempt_curriculum_id(record), threshold
                    )
                )
            ),
            None,
        )
        if removable is None:
            removable = next(
                (
                    index for index, record in enumerate(records)
                    if passing_counts.get(
                        (_attempt_curriculum_id(record), record.get("lesson_id")), 0
                    ) > 1
                ),
                None,
            )
        if removable is None:
            raise LearningCapacityError(
                "Lesson attempt history is full; all retained passing results are protected."
            )
        del records[removable]
    records.append(item)


def capture_feedback(message, source="conversation"):
    """Capture explicit correction signals as pending experiences, never auto-promote."""
    if not LEARNING_ENABLED or not isinstance(message, str):
        return None
    lowered = message.lower()
    if not any(marker in lowered for marker in _CORRECTION_MARKERS):
        return None
    text = _text(message, "message", 4000)
    record_id = _id("exp", text)
    with _LOCK:
        data = _load()
        if any(item.get("id") == record_id for item in data["experiences"]):
            return record_id
        _append_bounded(
            data, "experiences",
            {
                "id": record_id, "kind": "correction_signal", "content": text,
                "source": source, "status": "pending_review", "created_at": _now(),
            },
            protected_statuses={"pending_review"},
        )
        _save(data)
    return record_id


def add_experience(content, source, outcome="observed", metadata=None):
    """Record a bounded experience; raw conversation transcripts are not collected."""
    content = _text(content, "content", 16000)
    source = _text(source, "source", 200)
    outcome = _text(outcome, "outcome", 200)
    payload = json.dumps([content, source, outcome], ensure_ascii=False)
    record_id = _id("exp", payload)
    with _LOCK:
        data = _load()
        if any(item.get("id") == record_id for item in data["experiences"]):
            return record_id
        _append_bounded(
            data, "experiences",
            {
                "id": record_id, "kind": "experience", "content": content,
                "source": source, "outcome": outcome,
                "metadata": metadata if isinstance(metadata, dict) else {},
                "status": "recorded", "created_at": _now(),
            },
            protected_statuses={"pending_review"},
        )
        _save(data)
    return record_id


def propose_knowledge(topic, content, source, evidence=None, confidence=0.5):
    """Create a reviewable knowledge candidate. Evidence and provenance are retained."""
    topic = _text(topic, "topic", 200)
    content = _text(content, "content", 16000)
    source = _text(source, "source", 500)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    evidence = evidence if isinstance(evidence, list) else []
    evidence = [_text(item, "evidence item", 1000) for item in evidence[:20]]
    record_id = _id("know", json.dumps([topic, content, source], ensure_ascii=False))
    with _LOCK:
        data = _load()
        if not any(item.get("id") == record_id for item in data["knowledge"]):
            _append_bounded(
                data, "knowledge",
                {
                    "id": record_id, "topic": topic, "content": content,
                    "source": source, "evidence": evidence,
                    "confidence": float(confidence), "status": "pending_review",
                    "created_at": _now(), "approved_at": None,
                },
                protected_statuses={"pending_review", "approved"},
            )
            _save(data)
    return record_id


def approve_knowledge(record_id, reviewer="owner"):
    """Promote a reviewed candidate into usable knowledge."""
    with _LOCK:
        data = _load()
        for item in data["knowledge"]:
            if item.get("id") == record_id:
                if item.get("status") != "pending_review":
                    return False
                item["status"] = "approved"
                item["reviewer"] = _text(reviewer, "reviewer", 100)
                item["approved_at"] = _now()
                _save(data)
                return True
    return False


def approve_claim(record_id, reviewer="owner"):
    """Approve one extracted claim after owner review."""
    with _LOCK:
        data = _load()
        for item in data["claims"]:
            if item.get("id") == record_id:
                if item.get("status") != "pending_review":
                    return False
                item["status"] = "approved"
                item["reviewer"] = _text(reviewer, "reviewer", 100)
                item["approved_at"] = _now()
                _save(data)
                return True
    return False


def extract_claim_candidates(knowledge_id, max_claims=40):
    """Extract review-only claims from one approved source record.

    Repeated exact claims gain source provenance, but are never auto-approved.
    Similar wording is deliberately not treated as corroboration.
    """
    from core.learning_synthesis import extract_claims, normalize_claim

    if isinstance(max_claims, bool) or not isinstance(max_claims, int) or not 1 <= max_claims <= 100:
        raise ValueError("max_claims must be between 1 and 100")

    with _LOCK:
        data = _load()
        source_record = next(
            (item for item in data["knowledge"] if item.get("id") == knowledge_id),
            None,
        )
        if source_record is None:
            raise ValueError("approved source knowledge was not found")
        if source_record.get("status") != "approved":
            raise ValueError("claims can only be extracted from approved knowledge")

        statements = extract_claims(source_record.get("content", ""), max_claims=max_claims)
        source_key = f"knowledge_record:{knowledge_id}"
        source_evidence = source_record.get("evidence", [])
        if isinstance(source_evidence, list):
            from core.handlers import browser
            for entry in source_evidence:
                if not isinstance(entry, str) or not entry.startswith("source_url:"):
                    continue
                raw_url = entry.split("source_url:", 1)[1].strip()
                try:
                    safe_url = browser._normalise_target(raw_url)
                except (TypeError, ValueError):
                    safe_url = None
                if safe_url:
                    source_key = safe_url
                    break
        result_ids = []
        changed = False
        for statement in statements:
            normalized = normalize_claim(statement)
            if not normalized:
                continue
            claim_id = _id("claim", normalized)
            claim = next(
                (item for item in data["claims"] if item.get("id") == claim_id),
                None,
            )
            if claim is None:
                confidence = source_record.get("confidence", 0.35)
                if (
                    isinstance(confidence, bool)
                    or not isinstance(confidence, (int, float))
                    or not 0 <= confidence <= 1
                ):
                    confidence = 0.35
                claim = {
                    "id": claim_id,
                    "topic": source_record.get("topic", "Unspecified"),
                    "claim": statement,
                    "supporting_knowledge_ids": [knowledge_id],
                    "supporting_source_keys": [source_key],
                    "source_count": 1,
                    "evidence": (
                        list(source_evidence[:20])
                        if isinstance(source_evidence, list) else []
                    ),
                    "confidence": min(float(confidence), 0.5),
                    "status": "pending_review",
                    "created_at": _now(),
                    "approved_at": None,
                }
                _append_bounded(
                    data, "claims", claim,
                    protected_statuses={"pending_review", "approved"},
                )
                changed = True
            else:
                supporting = claim.setdefault("supporting_knowledge_ids", [])
                source_keys = claim.setdefault(
                    "supporting_source_keys",
                    [f"knowledge_record:{item_id}" for item_id in supporting],
                )
                if knowledge_id not in supporting:
                    supporting.append(knowledge_id)
                    changed = True
                if source_key not in source_keys:
                    source_keys.append(source_key)
                    claim["source_count"] = len(source_keys)
                    changed = True
                evidence = claim.setdefault("evidence", [])
                for entry in source_evidence if isinstance(source_evidence, list) else []:
                    if entry not in evidence and len(evidence) < 20:
                        evidence.append(entry)
                        changed = True
            result_ids.append(claim_id)

        if changed:
            _save(data)
        return list(dict.fromkeys(result_ids))


def reject_learning(record_id):
    """Reject a pending knowledge, extracted claim, or experience candidate."""
    with _LOCK:
        data = _load()
        for collection in ("knowledge", "claims", "experiences"):
            for item in data[collection]:
                if item.get("id") == record_id:
                    if item.get("status") != "pending_review":
                        return False
                    item["status"] = "rejected"
                    item["reviewed_at"] = _now()
                    _save(data)
                    return True
    return False


def _has_trusted_skill_verification(item):
    """No generic trusted evaluator exists in this release; fail closed."""
    return False

def record_skill(name, procedure, source, verified=False, test_result=None):
    """Stage a skill; caller-supplied booleans/results can never activate it.

    The parameters remain for backwards compatibility, but ALE currently has
    no trusted general-purpose skill evaluator. Activation is intentionally
    unavailable until a sandbox/proctored evaluator can issue a receipt.
    """
    name = _text(name, "name", 200)
    procedure = _text(procedure, "procedure")
    source = _text(source, "source", 500)
    record_id = _id("skill", json.dumps([name, procedure, source], ensure_ascii=False))
    with _LOCK:
        data = _load()
        for item in data["skills"]:
            if item.get("id") == record_id:
                return record_id
        _append_bounded(
            data, "skills",
            {
                "id": record_id, "name": name, "procedure": procedure,
                "source": source, "status": "pending_verification",
                "verified": False, "test_result": None,
                "created_at": _now(), "verified_at": None,
            },
            protected_statuses={"active", "pending_verification"},
        )
        _save(data)
    return record_id


def record_evaluation(skill, score, baseline=None, task_id=None):
    """Record objective skill measurements to make improvement measurable."""
    skill = _text(skill, "skill", 200)
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
        raise ValueError("score must be between 0 and 1")
    if baseline is not None and (
        isinstance(baseline, bool)
        or not isinstance(baseline, (int, float))
        or not 0 <= baseline <= 1
    ):
        raise ValueError("baseline must be between 0 and 1")
    item = {
        "skill": skill, "score": float(score),
        "baseline": float(baseline) if baseline is not None else None,
        "task_id": task_id, "created_at": _now(),
    }
    with _LOCK:
        data = _load()
        _append_bounded(data, "evaluations", item)
        _save(data)
    return item


def create_learning_project(subject, goal=None):
    """Create or return an idempotent subject-directed learning project."""
    from core.learning_projects import build_research_queries, learning_stages

    subject = _text(subject, "subject", 180)
    goal = (
        _text(goal, "goal", 500)
        if goal is not None
        else "Build understanding from foundations through practical application."
    )
    queries = build_research_queries(subject)
    project_id = _id("project", subject.casefold())
    with _LOCK:
        data = _load()
        existing = next(
            (item for item in data["projects"] if item.get("id") == project_id),
            None,
        )
        if existing is not None:
            return deepcopy(existing)
        project = {
            "id": project_id,
            "subject": subject,
            "goal": goal,
            "status": "research_pending",
            "stages": learning_stages(subject),
            "stage_attempts": [],
            "research_queries": [
                {"query": query, "status": "planned", "source_count": 0}
                for query in queries
            ],
            "knowledge_ids": [],
            "extracted_knowledge_ids": [],
            "created_at": _now(),
            "updated_at": _now(),
        }
        _append_bounded(
            data, "projects", project,
            protected_statuses={"research_pending", "research_ready", "research_incomplete", "active"},
        )
        _save(data)
        return _learning_project_with_progress(project)


def _learning_project_with_progress(project):
    """Decorate a project copy with provisional, prerequisite-aware stage progress."""
    result = deepcopy(project)
    attempts = result.get("stage_attempts", [])
    completed = set()
    progress = []
    for stage in result.get("stages", []):
        stage_id = stage.get("id")
        stage_attempts = [
            item for item in attempts if item.get("stage_id") == stage_id
        ]
        scores = [
            item.get("score") for item in stage_attempts
            if isinstance(item.get("score"), (int, float))
            and not isinstance(item.get("score"), bool)
        ]
        passed = any(score >= DEFAULT_PASS_THRESHOLD for score in scores)
        index = next(
            (i for i, item in enumerate(result.get("stages", []))
             if item.get("id") == stage_id),
            0,
        )
        previous_ids = [
            item.get("id") for item in result.get("stages", [])[:index]
        ]
        prerequisites_met = all(item in completed for item in previous_ids)
        if passed:
            state = "provisional"
            completed.add(stage_id)
        elif prerequisites_met:
            state = "in_progress"
        else:
            state = "locked"
        progress.append({
            "stage_id": stage_id,
            "completion_state": state,
            "attempt_count": len(stage_attempts),
            "best_score": max(scores) if scores else None,
        })
    result["stage_progress"] = progress
    result["completed_stages"] = len(completed)
    result["total_stages"] = len(result.get("stages", []))
    result["completion_policy"] = "provisional_assessment_progress"
    result["disclaimer"] = (
        "A recorded pass is provisional progress, not proof of independent mastery."
    )
    # Internal attempt detail is available only through the owner API, not needed
    # in normal project summaries.
    return result


def record_project_stage_attempt(
    project_id, stage_id, score, assessment_id, evidence_ref=None
):
    """Record a provisional subject-project stage assessment with prerequisite checks."""
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
        raise ValueError("score must be a number between 0 and 1")
    assessment_id = _text(assessment_id, "assessment_id", 120)
    if evidence_ref is not None:
        evidence_ref = _text(evidence_ref, "evidence_ref", 1000)

    with _LOCK:
        data = _load()
        project = next(
            (item for item in data["projects"] if item.get("id") == project_id),
            None,
        )
        if project is None:
            raise ValueError("learning project was not found")
        stage_ids = [
            item.get("id") for item in project.get("stages", [])
            if isinstance(item, dict)
        ]
        if stage_id not in stage_ids:
            raise ValueError("stage was not found in this learning project")
        attempt_id = _id(
            "project_attempt",
            json.dumps([project_id, stage_id, assessment_id], ensure_ascii=False),
        )
        attempts = project.setdefault("stage_attempts", [])
        existing = next(
            (item for item in attempts if item.get("id") == attempt_id),
            None,
        )
        if existing is not None:
            return attempt_id

        stage_index = stage_ids.index(stage_id)
        previous_ids = stage_ids[:stage_index]
        for previous_id in previous_ids:
            if not any(
                item.get("stage_id") == previous_id
                and isinstance(item.get("score"), (int, float))
                and not isinstance(item.get("score"), bool)
                and item["score"] >= DEFAULT_PASS_THRESHOLD
                for item in attempts
            ):
                raise ValueError("stage prerequisite has not been provisionally completed")

        if len(attempts) >= 100:
            raise LearningCapacityError(
                "Learning project assessment history reached its safe capacity."
            )
        attempts.append({
            "id": attempt_id,
            "stage_id": stage_id,
            "assessment_id": assessment_id,
            "score": float(score),
            "evidence_ref": evidence_ref,
            "created_at": _now(),
            "verification_state": "provisional",
        })
        project["updated_at"] = _now()
        _save(data)
        return attempt_id


def record_learning_project_research(project_id, query_results, knowledge_ids):
    """Attach bounded research outcomes and valid knowledge record IDs to a project."""
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("project_id must be a non-empty string")
    if not isinstance(query_results, list) or len(query_results) > 3:
        raise ValueError("query_results must be a list of at most three entries")
    if not isinstance(knowledge_ids, list) or len(knowledge_ids) > 15:
        raise ValueError("knowledge_ids must be a list of at most fifteen IDs")

    normalized_results = []
    for item in query_results:
        if not isinstance(item, dict):
            raise ValueError("each query result must be an object")
        query = _text(item.get("query"), "query", 300)
        status = item.get("status")
        if status not in {"success", "unavailable", "empty"}:
            raise ValueError("query status must be success, unavailable, or empty")
        source_count = item.get("source_count", 0)
        if (
            isinstance(source_count, bool)
            or not isinstance(source_count, int)
            or not 0 <= source_count <= 5
        ):
            raise ValueError("source_count must be an integer between 0 and 5")
        normalized_results.append({
            "query": query,
            "status": status,
            "source_count": source_count,
        })

    unique_ids = list(dict.fromkeys(
        _text(item, "knowledge_id", 80)
        for item in knowledge_ids
    ))
    with _LOCK:
        data = _load()
        project = next(
            (item for item in data["projects"] if item.get("id") == project_id),
            None,
        )
        if project is None:
            raise ValueError("learning project was not found")
        records_by_id = {
            item.get("id"): item for item in data["knowledge"]
            if item.get("id") in unique_ids
        }
        if set(records_by_id) != set(unique_ids):
            raise ValueError("one or more knowledge records do not exist")
        extracted_ids = []
        for record_id, record in records_by_id.items():
            evidence = record.get("evidence", [])
            if isinstance(evidence, list) and "fetch_status: extracted" in evidence:
                extracted_ids.append(record_id)
        project["research_queries"] = normalized_results
        project["knowledge_ids"] = unique_ids
        project["extracted_knowledge_ids"] = extracted_ids
        project["status"] = "research_ready" if extracted_ids else "research_incomplete"
        project["updated_at"] = _now()
        _save(data)
        return deepcopy(project)


def get_learning_project(project_id):
    """Return one learning project without exposing mutable store internals."""
    if not isinstance(project_id, str) or not project_id:
        return None
    for item in _load()["projects"]:
        if item.get("id") == project_id:
            return _learning_project_with_progress(item)
    return None


def get_learning_projects(limit=50):
    """Return the newest bounded set of learning projects."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    items = _load()["projects"]
    items = sorted(items, key=lambda item: item.get("updated_at", ""), reverse=True)
    return [_learning_project_with_progress(item) for item in items[:limit]]


def get_learning_status():
    """Return counts and policy without exposing stored private content."""
    data = _load()
    return {
        "enabled": LEARNING_ENABLED,
        "mode": LEARNING_CONFIG.get("mode", "continuous"),
        "autonomy": LEARNING_CONFIG.get("autonomy", "balanced"),
        "training": LEARNING_CONFIG.get("training", "hybrid"),
        "pending_experiences": sum(x.get("status") == "pending_review" for x in data["experiences"]),
        "approved_knowledge": sum(x.get("status") == "approved" for x in data["knowledge"]),
        "pending_knowledge": sum(x.get("status") == "pending_review" for x in data["knowledge"]),
        "pending_claims": sum(x.get("status") == "pending_review" for x in data["claims"]),
        "approved_claims": sum(x.get("status") == "approved" for x in data["claims"]),
        "learning_projects": len(data["projects"]),
        "active_skills": sum(
            x.get("status") == "active" and _has_trusted_skill_verification(x)
            for x in data["skills"]
        ),
        "pending_verification_skills": sum(
            x.get("status") == "pending_verification"
            or (
                x.get("status") == "active"
                and not _has_trusted_skill_verification(x)
            )
            for x in data["skills"]
        ),
        "evaluation_count": len(data["evaluations"]),
        "curriculum_attempt_count": len(data["lesson_attempts"]),
        "fine_tuning_examples": len(data["training_examples"]),
        "model_weights_updated": False,
    }


def get_knowledge(topic=None):
    """Return approved knowledge only."""
    items = [x for x in _load()["knowledge"] if x.get("status") == "approved"]
    if topic is not None:
        items = [x for x in items if x.get("topic") == topic]
    return items


def get_approved_claims():
    """Return only owner-approved extracted claims."""
    return [item for item in _load()["claims"] if item.get("status") == "approved"]


def get_knowledge_map(topic):
    """Return a compact source-backed map for an exact or partial topic match."""
    topic = _text(topic, "topic", 200)
    lowered = topic.casefold()
    sources = [
        item for item in get_knowledge()
        if lowered in str(item.get("topic", "")).casefold()
        or str(item.get("topic", "")).casefold() in lowered
    ]
    source_ids = {item.get("id") for item in sources}
    claims = [
        item for item in get_approved_claims()
        if (
            lowered in str(item.get("topic", "")).casefold()
            or str(item.get("topic", "")).casefold() in lowered
            or source_ids.intersection(item.get("supporting_knowledge_ids", []))
        )
    ]
    from core.learning_claim_analysis import find_potential_conflicts
    from core.learning_evidence import (
        assess_source_provenance,
        summarize_claim_provenance,
    )

    potential_conflicts = find_potential_conflicts(claims)
    provenance_summary = summarize_claim_provenance(claims)
    term_counts = Counter()
    for item in sources:
        term_counts.update(_tokens(item.get("content", "")))
    return {
        "topic": topic,
        "source_count": len(sources),
        "claim_count": len(claims),
        "corroborated_claim_count": 0,
        "candidate_multi_source_claim_count": provenance_summary[
            "multi_host_candidate_claim_count"
        ],
        "provenance_summary": provenance_summary,
        "sources": [
            {
                "id": item.get("id"),
                "source": item.get("source"),
                "evidence": list(item.get("evidence", [])),
                "created_at": item.get("created_at"),
            }
            for item in sources
        ],
        "claims": [
            {
                "id": item.get("id"),
                "topic": item.get("topic"),
                "claim": item.get("claim"),
                "source_count": item.get("source_count", 1),
                "provenance": assess_source_provenance(item),
                "evidence": list(item.get("evidence", [])),
            }
            for item in claims
        ],
        "potential_conflicts": potential_conflicts,
        "claim_analysis_policy": (
            "Explicit-negation lexical screening only; potential conflicts require "
            "human review and are not semantic contradiction findings."
        ),
        "frequent_terms": [
            {"term": term, "count": count}
            for term, count in term_counts.most_common(15)
        ],
        "corroboration_policy": (
            "Distinct URLs and hostnames are provenance signals only. Different "
            "hostnames do not prove independent ownership, editorial independence, "
            "source quality, or truth. Independent corroboration is not yet verified."
        ),
    }


def analyze_knowledge_map_claims(topic, max_pairs=10):
    """Run an explicit, bounded model-assisted comparison over approved topic claims."""
    topic = _text(topic, "topic", 200)
    knowledge_map = get_knowledge_map(topic)
    approved_claims = [
        {**item, "status": "approved"}
        for item in knowledge_map["claims"]
    ]
    from core.learning_semantic_analysis import analyze_claim_pairs
    return analyze_claim_pairs(approved_claims, max_pairs=max_pairs)


def _tokens(value):
    return [
        token.lower()
        for token in _TOKEN_PATTERN.findall(str(value or ""))
        if token.lower() not in _STOPWORDS
    ]


def get_relevant_knowledge(message, limit=5):
    """Rank approved knowledge with bounded BM25-style lexical relevance."""
    if not isinstance(message, str) or not message.strip():
        return []
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return []

    query_terms = set(_tokens(message))
    if not query_terms:
        return []

    items = list(get_knowledge())
    for claim in get_approved_claims():
        items.append({
            "id": claim.get("id"),
            "topic": claim.get("topic", "Extracted claim"),
            "content": claim.get("claim", ""),
            "source": "Owner-approved extracted claim",
            "evidence": list(claim.get("evidence", [])),
            "confidence": claim.get("confidence", 0.35),
            "status": "approved",
            "record_type": "claim",
            "source_count": claim.get("source_count", 1),
        })
    if not items:
        return []

    documents = []
    for item in items:
        text = " ".join((item.get("topic", ""), item.get("content", "")))
        tokens = _tokens(text)
        documents.append((item, Counter(tokens), len(tokens)))

    document_count = len(documents)
    average_length = sum(length for _, _, length in documents) / document_count or 1.0
    document_frequency = {
        term: sum(1 for _, frequencies, _ in documents if frequencies.get(term, 0))
        for term in query_terms
    }

    scored = []
    k1 = 1.5
    b = 0.75
    for item, frequencies, length in documents:
        score = 0.0
        for term in query_terms:
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            df = document_frequency[term]
            inverse_frequency = math.log(
                1.0 + (document_count - df + 0.5) / (df + 0.5)
            )
            denominator = frequency + k1 * (
                1.0 - b + b * length / average_length
            )
            score += inverse_frequency * (
                frequency * (k1 + 1.0) / denominator
            )
        if score > 0:
            scored.append((score, item.get("created_at", ""), item))

    scored.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return [item for _, _, item in scored[:limit]]


def get_active_skills():
    return [
        item for item in _load()["skills"]
        if item.get("status") == "active"
        and _has_trusted_skill_verification(item)
    ]


def export_training_examples():
    """Export approved knowledge and verified skills as supervised examples."""
    data = _load()
    examples = []
    for item in data["knowledge"]:
        if item.get("status") == "approved":
            examples.append({
                "instruction": f"Explain the topic: {item['topic']}",
                "output": item["content"], "source": item["source"],
            })
    for item in data["skills"]:
        if (
            item.get("status") == "active"
            and _has_trusted_skill_verification(item)
        ):
            examples.append({
                "instruction": f"Perform the skill: {item['name']}",
                "output": item["procedure"], "source": item["source"],
            })
    return examples


def get_pending_reviews(kind="all", limit=100):
    """Return bounded pending review records with explicit collection metadata."""
    if kind not in {"all", "knowledge", "claim", "experience"}:
        raise ValueError("kind must be all, knowledge, claim, or experience")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 200
    ):
        raise ValueError("limit must be between 1 and 200")

    data = _load()
    records = []
    collections = {
        "knowledge": (("knowledge", data["knowledge"]),),
        "claim": (("claims", data["claims"]),),
        "experience": (("experiences", data["experiences"]),),
        "all": (
            ("knowledge", data["knowledge"]),
            ("claims", data["claims"]),
            ("experiences", data["experiences"]),
        ),
    }[kind]
    for collection, items in collections:
        for item in items:
            if item.get("status") != "pending_review":
                continue
            record = deepcopy(item)
            record["collection"] = collection
            if collection == "knowledge":
                record["kind"] = "knowledge"
            elif collection == "claims":
                record["kind"] = "claim"
            # Preserve the original experience subtype (for example,
            # "correction_signal") for backwards-compatible review responses.
            records.append(record)

    records.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return records[:limit]


def get_review_item(record_id):
    """Return a defensive copy of one learning record for an owner review action."""
    if not isinstance(record_id, str) or not record_id:
        return None
    data = _load()
    for collection in ("knowledge", "claims", "experiences"):
        for item in data[collection]:
            if item.get("id") == record_id:
                record = deepcopy(item)
                record["collection"] = collection
                if collection == "knowledge":
                    record["kind"] = "knowledge"
                elif collection == "claims":
                    record["kind"] = "claim"
                return record
    return None


def _curriculum_threshold(catalog):
    policy = catalog.get("completion_policy", {})
    threshold = policy.get("provisional_pass_threshold", DEFAULT_PASS_THRESHOLD)
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0 < threshold <= 1
    ):
        raise ValueError("curriculum pass threshold must be between 0 and 1")
    return float(threshold)


def get_curriculum_progress(curriculum_id=None):
    """Summarise one subject pack; passes remain provisional and never verify skills."""
    from core.learning_curriculum import get_curriculum, get_lessons

    catalog = get_curriculum(curriculum_id)
    curriculum_id = catalog["curriculum_id"]
    lessons = get_lessons(curriculum_id)
    threshold = _curriculum_threshold(catalog)
    data = _load()
    attempts_by_lesson = {}
    for attempt in data["lesson_attempts"]:
        lesson_id = attempt.get("lesson_id")
        if (
            not isinstance(lesson_id, str)
            or _attempt_curriculum_id(attempt) != curriculum_id
        ):
            continue
        attempts_by_lesson.setdefault(lesson_id, []).append(attempt)

    valid_lesson_ids = {lesson["id"] for lesson in lessons}
    completed_ids = {
        lesson_id
        for lesson_id, attempts in attempts_by_lesson.items()
        if lesson_id in valid_lesson_ids and any(
            isinstance(item.get("score"), (int, float))
            and not isinstance(item.get("score"), bool)
            and item["score"] >= threshold
            for item in attempts
        )
    }
    summaries = []
    total_attempts = sum(
        len(attempts_by_lesson.get(lesson["id"], []))
        for lesson in lessons
    )
    for lesson in lessons:
        attempts = attempts_by_lesson.get(lesson["id"], [])
        latest = max(
            attempts,
            key=lambda item: item.get("created_at", ""),
            default=None,
        )
        summaries.append({
            "lesson_id": lesson["id"],
            "title": lesson["title"],
            "module_id": lesson["module_id"],
            "module_title": lesson["module_title"],
            "prerequisites": list(lesson.get("prerequisites", [])),
            "prerequisites_met": set(lesson.get("prerequisites", [])).issubset(completed_ids),
            "attempt_count": len(attempts),
            "best_score": max(
                (float(item["score"]) for item in attempts
                 if isinstance(item.get("score"), (int, float))
                 and not isinstance(item.get("score"), bool)),
                default=None,
            ),
            "latest_score": latest.get("score") if latest else None,
            "last_attempt_at": latest.get("created_at") if latest else None,
            "completed": lesson["id"] in completed_ids,
            "completion_state": (
                "provisional" if lesson["id"] in completed_ids
                else "in_progress" if attempts
                else "not_started"
            ),
        })

    total_lessons = len(summaries)
    return {
        "curriculum_id": catalog["curriculum_id"],
        "curriculum_title": catalog["title"],
        "completion_threshold": threshold,
        "completion_policy": "provisional_assessment_progress",
        "total_lessons": total_lessons,
        "completed_lessons": len(completed_ids),
        "completion_ratio": (
            len(completed_ids) / total_lessons if total_lessons else 0.0
        ),
        "attempt_count": total_attempts,
        "lessons": summaries,
        "disclaimer": (
            "A recorded pass is provisional progress. It does not independently "
            "verify mastery, activate a skill, or change model weights."
        ),
    }


def record_lesson_attempt(
    lesson_id,
    score,
    assessment_id,
    assessment_type="knowledge_check",
    evidence_ref=None,
    curriculum_id=None,
):
    """Record provisional progress within one explicitly identified subject pack."""
    from core.learning_curriculum import (
        get_curricula, get_curriculum, get_lesson, get_lessons,
    )

    catalog = get_curriculum(curriculum_id)
    curriculum_id = catalog["curriculum_id"]
    threshold = _curriculum_threshold(catalog)
    packs = get_curricula()
    valid_lesson_keys = {
        (pack["curriculum_id"], lesson["id"])
        for pack in packs
        for lesson in get_lessons(pack["curriculum_id"])
    }
    pass_thresholds = {
        pack["curriculum_id"]: _curriculum_threshold(pack)
        for pack in packs
    }
    lesson = get_lesson(lesson_id, curriculum_id)
    if lesson is None:
        raise ValueError("lesson_id is not present in the active curriculum")
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not 0 <= score <= 1
    ):
        raise ValueError("score must be between 0 and 1")
    assessment_id = _text(assessment_id, "assessment_id", 120)
    allowed_types = {
        "knowledge_check", "coding_exercise",
        "written_explanation", "oral_explanation",
    }
    if not isinstance(assessment_type, str) or assessment_type not in allowed_types:
        raise ValueError("unsupported assessment_type")
    if evidence_ref is not None:
        evidence_ref = _text(evidence_ref, "evidence_ref", 1000)

    record_id = _id(
        "attempt",
        json.dumps([curriculum_id, lesson_id, assessment_id], ensure_ascii=False),
    )
    with _LOCK:
        data = _load()
        existing = next(
            (item for item in data["lesson_attempts"] if item.get("id") == record_id),
            None,
        )
        if existing is not None:
            return record_id

        completed = {
            item.get("lesson_id")
            for item in data["lesson_attempts"]
            if isinstance(item.get("lesson_id"), str)
            and _attempt_curriculum_id(item) == curriculum_id
            and (curriculum_id, item.get("lesson_id")) in valid_lesson_keys
            and isinstance(item.get("score"), (int, float))
            and not isinstance(item.get("score"), bool)
            and item.get("score") >= threshold
        }
        for prerequisite in lesson.get("prerequisites", []):
            if prerequisite not in completed:
                raise ValueError(
                    f"lesson prerequisite has not been completed: {prerequisite}"
                )

        attempt = {
            "id": record_id,
            "curriculum_id": curriculum_id,
            "lesson_id": lesson_id,
            "assessment_id": assessment_id,
            "assessment_type": assessment_type,
            "score": float(score),
            "passed": score >= threshold,
            "verification_state": "provisional",
            "evidence_ref": evidence_ref,
            "created_at": _now(),
        }
        _append_lesson_attempt(
            data, attempt, threshold, valid_lesson_keys, pass_thresholds
        )
        _save(data)
    return record_id


__all__ = [
    "LearningCapacityError",
    "LearningStoreError",
    "add_experience",
    "approve_knowledge",
    "capture_feedback",
    "export_training_examples",
    "get_active_skills",
    "get_curriculum_progress",
    "get_knowledge",
    "get_learning_status",
    "get_pending_reviews",
    "get_relevant_knowledge",
    "get_review_item",
    "propose_knowledge",
    "record_evaluation",
    "record_lesson_attempt",
    "record_skill",
    "reject_learning",
]
