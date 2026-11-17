"""Owner-authenticated review and curriculum controls for Amiki's Learning Engine.

The curriculum catalog is read-only. Personal progress and assessment writes require
owner authorization and remain provisional until a separate verification workflow exists.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StrictFloat, StrictInt

from core import learning_engine
from interfaces.api.routes.security import require_uosa_scope
from core.learning_curriculum import get_curricula, get_curriculum
from core.learning_evidence import assess_claim_evidence
from core.learning_evidence_evaluation import evaluate_claim_against_sources

router = APIRouter()


def _pending_record(record_id: str) -> dict:
    record = learning_engine.get_review_item(record_id)
    if record is None or record.get("status") != "pending_review":
        raise HTTPException(status_code=404, detail="Pending learning item was not found.")
    return record


class LearningProjectInput(BaseModel):
    subject: str = Field(min_length=2, max_length=180)
    goal: Optional[str] = Field(default=None, max_length=500)


class LessonAttemptInput(BaseModel):
    assessment_id: str = Field(min_length=1, max_length=120)
    assessment_type: Literal[
        "knowledge_check",
        "coding_exercise",
        "written_explanation",
        "oral_explanation",
    ] = "knowledge_check"
    score: StrictFloat = Field(ge=0, le=1)
    evidence_ref: Optional[str] = Field(default=None, max_length=1000)


class SemanticClaimAnalysisInput(BaseModel):
    topic: str = Field(min_length=1, max_length=200)
    max_pairs: StrictInt = Field(default=10, ge=1, le=20)


class ClaimEvidenceEvaluationInput(BaseModel):
    max_sources: StrictInt = Field(default=5, ge=1, le=5)


@router.get("/learning/curricula")
def learning_curricula():
    """List all validated subject-independent curriculum packs."""
    return {"status": "ok", "items": get_curricula()}


@router.get("/learning/curricula/{curriculum_id}")
def learning_curriculum_by_id(curriculum_id: str):
    """Return one validated learning pathway by stable ID."""
    try:
        return get_curriculum(curriculum_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Curriculum was not found.") from exc


@router.get("/learning/curricula/{curriculum_id}/progress", dependencies=[Depends(require_uosa_scope("ale.progress.read"))])
def learning_progress_by_curriculum(
    curriculum_id: str,
):
    """Return the authenticated learner's progress for one subject."""
    try:
        return learning_engine.get_curriculum_progress(curriculum_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Curriculum was not found.") from exc


@router.post("/learning/curricula/{curriculum_id}/progress/{lesson_id}/attempt", dependencies=[Depends(require_uosa_scope("ale.progress.write"))])
def record_curriculum_attempt_by_id(
    curriculum_id: str,
    lesson_id: str,
    payload: LessonAttemptInput,
):
    """Record provisional progress in the named subject, not another subject."""
    try:
        record_id = learning_engine.record_lesson_attempt(
            lesson_id=lesson_id,
            score=payload.score,
            assessment_id=payload.assessment_id,
            assessment_type=payload.assessment_type,
            evidence_ref=payload.evidence_ref,
            curriculum_id=curriculum_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "status": "recorded",
        "id": record_id,
        "curriculum_id": curriculum_id,
        "lesson_id": lesson_id,
        "verification_state": "provisional",
    }


@router.get("/learning/curriculum")
def learning_curriculum():
    """Return the validated, versioned public curriculum catalog."""
    return get_curriculum()


@router.get("/learning/progress", dependencies=[Depends(require_uosa_scope("ale.progress.read"))])
def learning_curriculum_progress(
):
    """Return the authenticated learner's provisional lesson progress."""
    return learning_engine.get_curriculum_progress()


@router.post("/learning/progress/{lesson_id}/attempt", dependencies=[Depends(require_uosa_scope("ale.progress.write"))])
def record_curriculum_attempt(
    lesson_id: str,
    payload: LessonAttemptInput,
):
    """Record an assessment result without claiming independently verified mastery."""
    try:
        record_id = learning_engine.record_lesson_attempt(
            lesson_id=lesson_id,
            score=payload.score,
            assessment_id=payload.assessment_id,
            assessment_type=payload.assessment_type,
            evidence_ref=payload.evidence_ref,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "status": "recorded",
        "id": record_id,
        "lesson_id": lesson_id,
        "verification_state": "provisional",
    }


@router.post("/learning/projects", dependencies=[Depends(require_uosa_scope("ale.projects.write"))])
def create_learning_project(
    payload: LearningProjectInput,
):
    """Create an owner-scoped subject learning project and its research plan."""
    try:
        return learning_engine.create_learning_project(
            subject=payload.subject,
            goal=payload.goal,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/learning/projects", dependencies=[Depends(require_uosa_scope("ale.projects.read"))])
def list_learning_projects(
    limit: int = Query(default=50, ge=1, le=100),
):
    """List the authenticated owner's bounded learning project history."""
    return {
        "status": "ok",
        "items": learning_engine.get_learning_projects(limit=limit),
    }


@router.get("/learning/projects/{project_id}", dependencies=[Depends(require_uosa_scope("ale.projects.read"))])
def get_learning_project(
    project_id: str,
):
    """Read one persisted subject learning project."""
    project = learning_engine.get_learning_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Learning project was not found.")
    return project


@router.post("/learning/projects/{project_id}/stages/{stage_id}/attempt", dependencies=[Depends(require_uosa_scope("ale.projects.write"))])
def record_learning_project_stage_attempt(
    project_id: str,
    stage_id: str,
    payload: LessonAttemptInput,
):
    """Record provisional progress for one stage in any subject project."""
    try:
        record_id = learning_engine.record_project_stage_attempt(
            project_id=project_id,
            stage_id=stage_id,
            score=payload.score,
            assessment_id=payload.assessment_id,
            evidence_ref=payload.evidence_ref,
        )
    except learning_engine.LearningCapacityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "status": "recorded",
        "id": record_id,
        "project_id": project_id,
        "stage_id": stage_id,
        "verification_state": "provisional",
    }


@router.get("/learning/knowledge-map", dependencies=[Depends(require_uosa_scope("ale.knowledge.read"))])
def learning_knowledge_map(
    topic: str = Query(min_length=1, max_length=200),
):
    """Return approved source-backed claims and their provenance for one topic."""
    try:
        return learning_engine.get_knowledge_map(topic)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/learning/knowledge-map/semantic-analysis", dependencies=[Depends(require_uosa_scope("ale.claims.analyze"))])
def analyze_semantic_claim_relationships(
    payload: SemanticClaimAnalysisInput,
):
    """Run a bounded, owner-triggered semantic comparison of approved claims."""
    try:
        result = learning_engine.analyze_knowledge_map_claims(
            topic=payload.topic,
            max_pairs=payload.max_pairs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result.get("status") == "unavailable":
        raise HTTPException(
            status_code=503,
            detail=result.get("message", "Semantic comparison is unavailable."),
        )
    return result


@router.get("/learning/claims/{claim_id}/evidence-assessment", dependencies=[Depends(require_uosa_scope("ale.claims.review"))])
def assess_learning_claim_evidence(
    claim_id: str,
):
    """Return a read-only evidence review checklist for one stored claim."""
    claim = learning_engine.get_review_item(claim_id)
    if claim is None or claim.get("kind") != "claim":
        raise HTTPException(status_code=404, detail="Learning claim was not found.")
    source_ids = claim.get("supporting_knowledge_ids", [])
    if not isinstance(source_ids, list):
        source_ids = []
    source_records = []
    for source_id in source_ids[:20]:
        source = learning_engine.get_review_item(source_id)
        if source is not None and source.get("kind") == "knowledge":
            source_records.append(source)
    return assess_claim_evidence(claim, source_records)


@router.post("/learning/claims/{claim_id}/evidence-evaluation", dependencies=[Depends(require_uosa_scope("ale.claims.evaluate"))])
def evaluate_learning_claim_evidence(
    claim_id: str,
    payload: ClaimEvidenceEvaluationInput,
):
    """Run a bounded model-assisted review against approved extracted passages."""
    claim = learning_engine.get_review_item(claim_id)
    if claim is None or claim.get("kind") != "claim":
        raise HTTPException(status_code=404, detail="Learning claim was not found.")
    if claim.get("status") != "approved":
        raise HTTPException(
            status_code=409,
            detail="Only owner-approved claims can be evaluated against source passages.",
        )
    source_ids = claim.get("supporting_knowledge_ids", [])
    if not isinstance(source_ids, list):
        source_ids = []
    source_records = []
    for source_id in source_ids[:20]:
        source = learning_engine.get_review_item(source_id)
        if (
            source is not None
            and source.get("kind") == "knowledge"
            and source.get("status") == "approved"
        ):
            source_records.append(source)
    try:
        result = evaluate_claim_against_sources(
            claim, source_records, max_sources=payload.max_sources,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result.get("status") == "unavailable":
        raise HTTPException(
            status_code=503,
            detail=result.get("message", "Claim evidence evaluation is unavailable."),
        )
    return result


@router.post("/learning/knowledge/{record_id}/extract-claims", dependencies=[Depends(require_uosa_scope("ale.claims.extract"))])
def extract_claims_from_knowledge(
    record_id: str,
    max_claims: int = Query(default=40, ge=1, le=100),
):
    """Extract review-only claims from an owner-approved source record."""
    record = learning_engine.get_review_item(record_id)
    if record is None or record.get("kind") != "knowledge":
        raise HTTPException(status_code=404, detail="Source knowledge was not found.")
    if record.get("status") != "approved":
        raise HTTPException(
            status_code=409,
            detail="Claims can only be extracted after source knowledge is approved.",
        )
    try:
        claim_ids = learning_engine.extract_claim_candidates(
            record_id, max_claims=max_claims
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "status": "queued_for_review",
        "source_knowledge_id": record_id,
        "count": len(claim_ids),
        "claim_ids": claim_ids,
    }


@router.get("/learning/review-queue", dependencies=[Depends(require_uosa_scope("ale.review.read"))])
def learning_review_queue(
    kind: Literal["all", "knowledge", "claim", "experience"] = Query(default="all"),
    limit: int = Query(default=100, ge=1, le=200),
):
    """List pending candidates with their source and evidence provenance."""
    items = learning_engine.get_pending_reviews(kind=kind, limit=limit)
    return {
        "status": "ok",
        "kind": kind,
        "count": len(items),
        "items": items,
    }


@router.post("/learning/review-queue/{record_id}/approve", dependencies=[Depends(require_uosa_scope("ale.review.approve"))])
def approve_learning_candidate(
    record_id: str,
):
    """Approve pending source knowledge or a separately reviewed extracted claim."""
    record = _pending_record(record_id)
    kind = record.get("kind")
    if kind == "knowledge":
        approved = learning_engine.approve_knowledge(record_id, reviewer="owner")
    elif kind == "claim":
        approved = learning_engine.approve_claim(record_id, reviewer="owner")
    else:
        raise HTTPException(
            status_code=409,
            detail="Correction experiences cannot be approved as knowledge.",
        )
    if not approved:
        raise HTTPException(status_code=409, detail="The learning item could not be approved.")
    return {"status": "approved", "id": record_id, "kind": kind}


@router.post("/learning/review-queue/{record_id}/reject", dependencies=[Depends(require_uosa_scope("ale.review.reject"))])
def reject_learning_candidate(
    record_id: str,
):
    """Reject a pending source, extracted claim, or explicit correction experience."""
    record = _pending_record(record_id)
    if not learning_engine.reject_learning(record_id):
        raise HTTPException(status_code=409, detail="The learning item could not be rejected.")
    return {"status": "rejected", "id": record_id, "kind": record.get("kind")}
