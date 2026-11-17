"""Academic workspace API."""

from fastapi import APIRouter, HTTPException

from core.academic import get_academic_state, save_academic_state
from interfaces.api.schemas.academic import (
    AcademicCourse,
    AcademicCourseCreate,
    AcademicCourseUpdate,
    AcademicStateResponse,
    AcademicStateUpdate,
    AcademicStudyRecord,
)


router = APIRouter()


@router.get(
    "/academic",
    response_model=AcademicStateResponse,
)
def academic_state_endpoint():
    """Return the user's current academic state."""
    return AcademicStateResponse(**get_academic_state())


@router.put(
    "/academic",
    response_model=AcademicStateResponse,
)
def update_academic_state_endpoint(state: AcademicStateUpdate):
    """Replace the user's academic state with explicitly recorded data."""
    saved = state.model_dump()
    save_academic_state(saved)
    return AcademicStateResponse(**saved)


@router.post(
    "/academic/courses",
    response_model=AcademicCourse,
    status_code=201,
)
def add_academic_course_endpoint(course: AcademicCourseCreate):
    """Record one course explicitly supplied by the user."""
    state = get_academic_state()
    courses = list(state.get("courses", []))
    courses.append(course.model_dump())
    state["courses"] = courses
    save_academic_state(state)
    return AcademicCourse(**courses[-1])


@router.get(
    "/academic/courses/{course_code}",
    response_model=AcademicCourse,
)
def get_academic_course_endpoint(course_code: str):
    """Return one explicitly recorded course without inventing study data."""
    state = get_academic_state()
    courses = list(state.get("courses", []))
    for recorded in courses:
        if isinstance(recorded, dict) and recorded.get("code") == course_code:
            return AcademicCourse(**recorded)
    raise HTTPException(status_code=404, detail="Academic course not found")


@router.put(
    "/academic/courses/{course_code}",
    response_model=AcademicCourse,
)
def update_academic_course_endpoint(course_code: str, course: AcademicCourseUpdate):
    """Update one explicitly recorded course without changing other courses."""
    state = get_academic_state()
    courses = list(state.get("courses", []))
    for index, recorded in enumerate(courses):
        if isinstance(recorded, dict) and recorded.get("code") == course_code:
            updated = {"code": course_code, **course.model_dump()}
            courses[index] = updated
            state["courses"] = courses
            save_academic_state(state)
            return AcademicCourse(**updated)
    raise HTTPException(status_code=404, detail="Academic course not found")


@router.post(
    "/academic/courses/{course_code}/study-records",
    response_model=AcademicStudyRecord,
    status_code=201,
)
def add_academic_study_record_endpoint(
    course_code: str,
    record: AcademicStudyRecord,
):
    """Record one explicit study activity for an existing course."""
    state = get_academic_state()
    courses = list(state.get("courses", []))
    for index, recorded in enumerate(courses):
        if isinstance(recorded, dict) and recorded.get("code") == course_code:
            updated = dict(recorded)
            metadata = dict(updated.get("metadata") or {})
            study_records = list(metadata.get("study_records") or [])
            study_records.append(record.model_dump())
            metadata["study_records"] = study_records
            updated["metadata"] = metadata
            courses[index] = updated
            state["courses"] = courses
            save_academic_state(state)
            return AcademicStudyRecord(**study_records[-1])
    raise HTTPException(status_code=404, detail="Academic course not found")
