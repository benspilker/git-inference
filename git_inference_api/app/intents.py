from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class IntentType(str, Enum):
    QUESTION = "question"
    JOB = "job"
    RESEARCH = "research"
    NEEDS_CLARIFICATION = "needs_clarification"


class IntentEnvelope(BaseModel):
    schema_version: str = "1.0"
    intent_type: IntentType
    task_type: str
    confidence: float = Field(ge=0.0, le=1.0)
    requires_local_execution: bool = False
    reason: str = ""


class PlannerEnvelope(BaseModel):
    schema_version: str = "1.0"
    intent_type: IntentType
    mode: str | None = None
    task_type: str
    confidence: float = Field(ge=0.0, le=1.0)
    requires_local_execution: bool = True
    parameters: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    question: str | None = None
    success_condition: str = ""
