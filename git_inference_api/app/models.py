from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


JobLifecycleStatus = Literal[
    "received",
    "routing",
    "routed",
    "planning",
    "planned",
    "needs_clarification",
    "executing",
    "verifying",
    "completed",
    "failed",
    "expired",
]

IntentType = Literal["question", "job", "research", "needs_clarification"]


class RoutingMetadata(BaseModel):
    schema_version: str = "1.0"
    intent_type: IntentType | None = None
    task_type: str | None = None
    route_state: JobLifecycleStatus = "received"
    requires_local_execution: bool = False
    success_condition: str | None = None
    reason: str | None = None


class ExecutionMetadata(BaseModel):
    status: str | None = None
    verified: bool | None = None
    details: dict[str, Any] | None = None


class StagesMetadata(BaseModel):
    router: str | None = None
    planner: str | None = None
    answerer: str | None = None
    execution: str | None = None
    verification: str | None = None
    final_phraser: str | None = None


class ChatMessage(BaseModel):
    role: str
    content: Any


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    format: str | None = None
    options: dict[str, Any] | None = None


class GenerateRequest(BaseModel):
    model: str
    prompt: str
    system: str | None = None
    stream: bool = False
    suffix: str | None = None
    template: str | None = None
    context: str | None = None
    raw: bool | None = None
    keep_alive: str | None = None
    options: dict[str, Any] | None = None


class AcceptedResponse(BaseModel):
    job_id: str
    status: JobLifecycleStatus | str
    done: bool
    position: int | None = None
    active_job_id: str | None = None
    intent_type: str | None = None
    task_type: str | None = None
    current_stage: str | None = None


class ChatResponse(BaseModel):
    model: str
    created_at: str
    message: dict[str, Any]
    done: bool
    job_id: str
    combined: dict[str, Any] | None = None
    intent_type: str | None = None
    task_type: str | None = None
    current_stage: str | None = None
    execution: dict[str, Any] | None = None
    stages: dict[str, Any] | None = None


class GenerateResponse(BaseModel):
    model: str
    created_at: str
    response: str
    done: bool
    job_id: str
    combined: dict[str, Any] | None = None
    intent_type: str | None = None
    task_type: str | None = None
    current_stage: str | None = None
    execution: dict[str, Any] | None = None
    stages: dict[str, Any] | None = None


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobLifecycleStatus | str
    done: bool
    position: int | None = None
    active_job_id: str | None = None
    model: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    result: dict[str, Any] | None = None
    combined: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    intent_type: str | None = None
    task_type: str | None = None
    current_stage: str | None = None
    execution: dict[str, Any] | None = None
    stages: dict[str, Any] | None = None


class RouterEnvelope(BaseModel):
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


class LocalExecutionEnvelope(BaseModel):
    task_type: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    success_condition: str | None = None


class LocalExecutionResult(BaseModel):
    execution_status: str
    task_type: str
    verified: bool
    details: dict[str, Any] = Field(default_factory=dict)
