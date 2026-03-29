from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


Role = Literal["system", "user", "assistant", "tool"]


class ChatMessage(BaseModel):
    role: Role
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False

    @property
    def system_prompt(self) -> Optional[str]:
        for m in self.messages:
            if m.role == "system":
                return m.content
        return None

    @property
    def user_prompt(self) -> str:
        for m in reversed(self.messages):
            if m.role == "user":
                return m.content
        return self.messages[-1].content


class ChatResponse(BaseModel):
    model: str
    created_at: str
    message: ChatMessage
    done: bool = True
    job_id: str
    combined: dict[str, Any] | None = None


class GenerateRequest(BaseModel):
    model: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    stream: bool = False
    suffix: str | None = None
    system: str | None = None
    template: str | None = None
    context: list[int] | None = None
    raw: bool | None = None
    keep_alive: str | None = None
    options: dict[str, Any] | None = None


class GenerateResponse(BaseModel):
    model: str
    created_at: str
    response: str
    done: bool = True
    job_id: str
    combined: dict[str, Any] | None = None


class AcceptedResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running"]
    done: bool = False
    position: int | None = None
    active_job_id: str | None = None


class JobStatusResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed", "expired"]
    done: bool
    position: int | None = None
    active_job_id: str | None = None
    model: str | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    result: dict | None = None
    combined: dict[str, Any] | None = None
    error: dict | None = None
