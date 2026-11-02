"""Small provider-neutral structural schema for model grading JSON."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GradingPointDecisionSchema(BaseModel):
    """Structural decision fields; scoring meaning remains in normalization."""

    model_config = ConfigDict(extra="allow")

    criterion_id: str | None = None
    decision: str | None = None
    suggested_score: float | int | str | None = None
    student_evidence: str | None = None
    evidence: dict[str, Any] | None = None
    reason: str | None = None
    needs_review: bool | None = None


class GradingSubQuestionSchema(BaseModel):
    """One returned sub-question; semantic scoring remains outside this schema."""

    model_config = ConfigDict(extra="allow")

    sub_question_id: str | int | None = None
    id: str | int | None = None
    criteria: list[GradingPointDecisionSchema] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_stable_question_id(self):
        if self.sub_question_id in (None, "") and self.id in (None, ""):
            raise ValueError("每个小题结果必须包含 sub_question_id 或 id")
        return self


class GradingResponseSchema(BaseModel):
    """Minimal common response shape shared by all OpenAI-compatible providers."""

    model_config = ConfigDict(extra="allow")

    sub_questions: list[GradingSubQuestionSchema]
    unmapped_answers: list[dict[str, Any]] = Field(default_factory=list)


def validate_grading_response(value: Any) -> dict[str, Any]:
    """Validate only JSON structure and preserve provider fields for normalization."""

    validated = GradingResponseSchema.model_validate(value)
    return validated.model_dump(mode="python", exclude_none=True)
