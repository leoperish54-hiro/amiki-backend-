"""Schemas for Amiki's academic workspace."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class AcademicCourse(BaseModel):
    """A user-recorded course; Amiki must not invent course metadata."""

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AcademicStateResponse(BaseModel):
    institution: str
    program: str
    duration_years: int
    current_year: int
    current_semester: int
    current_session: str
    courses: list[dict[str, Any]]
    progress_policy: str


class AcademicStateUpdate(AcademicStateResponse):
    """Validated replacement state for the academic workspace."""


class AcademicCourseUpdate(BaseModel):
    """Mutable recorded study data for one course."""

    name: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AcademicCourseCreate(BaseModel):
    """Validated course data supplied explicitly by the user."""

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AcademicStudyRecord(BaseModel):
    """One user-recorded study activity; Amiki must not infer completion."""

    topic: str = Field(min_length=1)
    activity: str = Field(min_length=1)
    status: Literal["planned", "in_progress", "completed"] = "completed"
    notes: str = ""
