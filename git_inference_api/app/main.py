from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db
from .config import settings
from .logging_config import configure_logging
from .models import (
    AcceptedResponse,
    ChatRequest,
    ChatResponse,
    GenerateRequest,
    GenerateResponse,
    JobStatusResponse,
)
from .worker import worker

configure_logging()
logger = logging.getLogger("git_inference_api.api")
TERMINAL_JOB_STATUSES = {"completed", "failed", "expired"}
NON_TERMINAL_VISIBLE_STATUSES = {"needs_clarification"}
STAGEABLE_STATUSES = {
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
}
_HEARTBEAT_LAST_ACK_HASH: str | None = None
_HEARTBEAT_LAST_ACK_AT_MONO: float = 0.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    recovery_summary = db.recover_inflight_jobs()
    worker.start()
    logger.info("application started", extra={"startup_recovery": recovery_summary})
    yield
    worker.stop()
    logger.info("application stopped")


app = FastAPI(title=settings.app_name, lifespan=lifespan)


def ollama_error_payload(detail: Any, status_code: int) -> dict[str, Any]:
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("error") or detail.get("detail") or f"HTTP {status_code} error"
        payload: dict[str, Any] = {"error": str(message)}
        code = detail.get("code")
        if code is not None:
            payload["code"] = str(code)
        passthrough_keys = (
            "limits",
            "word_count",
            "chunk_count",
            "job_id",
            "status",
            "intent_type",
            "task_type",
            "current_stage",
        )
        for key in passthrough_keys:
            if key in detail:
                payload[key] = detail[key]
        extra_details = {
            key: value
            for key, value in detail.items()
            if key not in {"message", "error", "detail", "code", *passthrough_keys}
        }
        if extra_details:
            payload["details"] = extra_details
        return payload
    if isinstance(detail, str):
        return {"error": detail}
    if detail is None:
        return {"error": f"HTTP {status_code} error"}
    return {"error": f"HTTP {status_code} error", "details": detail}


@app.exception_handler(StarletteHTTPException)
async def ollama_http_exception_handler(_: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content=ollama_error_payload(exc.detail, exc.status_code))


@app.exception_handler(RequestValidationError)
async def ollama_validation_exception_handler(_: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={
            "error": "Invalid request body",
            "code": "INVALID_REQUEST",
            "details": exc.errors(),
        },
    )


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "app": settings.app_name}


def require_admin_access(admin_key: str | None) -> None:
    configured = (settings.admin_api_key or "").strip()
    if not configured:
        logger.warning("admin endpoint called without ADMIN_API_KEY configured")
        return
    if not admin_key or admin_key.strip() != configured:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "invalid admin key"})


@app.post("/api/admin/queue/purge")
def purge_queue(
    include_queued: bool = Query(default=True),
    terminal_status: Literal["failed", "expired"] = Query(default="failed"),
    reason: str | None = Query(default="manual purge"),
    dry_run: bool = Query(default=False),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    require_admin_access(x_admin_key)
    summary = db.purge_inflight_jobs(
        include_queued=include_queued,
        terminal_status=terminal_status,
        reason=reason,
        dry_run=dry_run,
    )
    summary["active_job_id"] = db.get_active_job_id()
    logger.warning("queue purge invoked", extra={"summary": summary})
    return summary


@app.post("/api/chat", response_model=ChatResponse | AcceptedResponse)
def chat(
    request: ChatRequest,
    async_mode: bool = Query(default=False, alias="async"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    effective_model = resolve_openclaw_effective_model(request.model)
    if effective_model != request.model:
        logger.info(
            "openclaw model override applied",
            extra={"requested_model": request.model, "effective_model": effective_model},
        )
        request = ChatRequest(
            model=effective_model,
            messages=request.messages,
            stream=request.stream,
            format=request.format,
            options=request.options,
        )
    normalized_request = normalize_chat_request(request)
    return handle_submission(
        normalized_request=normalized_request,
        model=effective_model,
        stream=request.stream,
        response_type="chat",
        async_mode=async_mode,
        idempotency_key=idempotency_key,
        combined_in_message=wants_combined_in_message(request.options, request.format),
    )


@app.post("/v1/chat/completions")
def openai_chat_completions(
    request: dict[str, Any],
    async_mode: bool = Query(default=False, alias="async"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    model = resolve_openclaw_effective_model(str(request.get("model") or "").strip())
    if not model:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_REQUEST", "message": "model is required (or configure OPENCLAW_DEFAULT_MODEL)"},
        )

    raw_messages = request.get("messages")
    if not isinstance(raw_messages, list):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_REQUEST", "message": "messages must be an array"},
        )

    chat_req = ChatRequest(
        model=model,
        messages=raw_messages,
        stream=False,  # force non-stream for OpenAI/Aider compatibility
        format=request.get("format"),
        options=request.get("options") if isinstance(request.get("options"), dict) else None,
    )

    result = chat(chat_req, async_mode=async_mode, idempotency_key=idempotency_key)

    if isinstance(result, JSONResponse):
        body = result.body.decode("utf-8") if isinstance(result.body, (bytes, bytearray)) else str(result.body)
        try:
            data = json.loads(body)
        except Exception:
            data = {"response": body}
    elif hasattr(result, "model_dump"):
        data = result.model_dump()  # pragma: no cover
    elif isinstance(result, dict):
        data = result
    else:
        data = {"response": str(result)}

    content = extract_assistant_content(data if isinstance(data, dict) else {"response": data})
    finish_reason = "stop"

    if isinstance(data, dict):
        status = str(data.get("status") or "").strip().lower()
        done_flag = bool(data.get("done", True))
        if status in {"failed", "expired"}:
            raise HTTPException(
                status_code=500,
                detail={"code": "MODEL_EXECUTION_ERROR", "message": content or f"job {status}"},
            )
        if not done_flag and status:
            finish_reason = None

    response_payload: dict[str, Any] = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": finish_reason,
            }
        ],
    }

    if isinstance(data, dict):
        if data.get("job_id"):
            response_payload["job_id"] = data.get("job_id")
        if data.get("status"):
            response_payload["status"] = data.get("status")
        if data.get("done") is not None:
            response_payload["done"] = data.get("done")

    return JSONResponse(status_code=200, content=response_payload)


@app.post("/api/generate", response_model=GenerateResponse | AcceptedResponse)
def generate(
    request: GenerateRequest,
    async_mode: bool = Query(default=False, alias="async"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    normalized_request = normalize_generate_request(request)
    return handle_submission(
        normalized_request=normalized_request,
        model=request.model,
        stream=request.stream,
        response_type="generate",
        async_mode=async_mode,
        idempotency_key=idempotency_key,
    )


@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return build_job_status(job)


@app.get("/api/tags")
def get_tags() -> dict[str, Any]:
    models = []
    for name in settings.available_models():
        models.append(
            {
                "name": name,
                "model": name,
                "modified_at": db.utcnow_iso(),
                "size": 0,
                "digest": "",
                "details": {
                    "format": "unknown",
                    "family": "git-inference",
                    "families": ["git-inference"],
                    "parameter_size": "unknown",
                    "quantization_level": "unknown",
                },
            }
        )
    return {"models": models}


def is_supported_model(model_name: str) -> bool:
    normalized = str(model_name or "").strip().lower()
    if not normalized:
        return False
    available = [name.strip().lower() for name in settings.available_models() if str(name).strip()]
    if normalized in available:
        return True
    tail = normalized.split("/")[-1]
    available_tails = {name.split("/")[-1] for name in available}
    return tail in available_tails


def ensure_supported_model_or_raise(model_name: str) -> None:
    if is_supported_model(model_name):
        return
    raise HTTPException(
        status_code=400,
        detail={
            "code": "INVALID_MODEL",
            "message": f"Unsupported model '{model_name}'.",
            "supported_models": settings.available_models(),
        },
    )


def handle_submission(
    normalized_request: dict[str, Any],
    model: str,
    stream: bool,
    response_type: Literal["chat", "generate"],
    async_mode: bool,
    idempotency_key: str | None,
    combined_in_message: bool = False,
):
    ensure_supported_model_or_raise(model)
    request_hash = sha256_json(normalized_request)
    resolved_idempotency_key = resolve_idempotency_key(idempotency_key, request_hash)

    existing = db.get_job_by_idempotency_key(resolved_idempotency_key)
    if existing:
        if existing["request_hash"] != request_hash:
            raise HTTPException(status_code=409, detail="Idempotency key was reused with a different request body")
        return build_response_for_mode(
            job=existing,
            model=model,
            stream=stream,
            response_type=response_type,
            async_mode=async_mode,
            combined_in_message=combined_in_message,
        )

    heartbeat_response = maybe_short_circuit_heartbeat(
        normalized_request=normalized_request,
        request_hash=request_hash,
        model=model,
        response_type=response_type,
        stream=stream,
    )
    if heartbeat_response is not None:
        return heartbeat_response

    route_hint = classify_route_hint(normalized_request)
    routing_metadata = {
        "schema_version": "1.0",
        "intent_type": route_hint,
        "task_type": suggest_task_type(normalized_request, route_hint),
        "route_state": "received",
        "requires_local_execution": route_hint == "job",
    }

    job_id = db.create_job(
        idempotency_key=resolved_idempotency_key,
        request_hash=request_hash,
        request_json={
            **normalized_request,
            "routing_metadata": routing_metadata,
        },
    )
    worker.notify()
    queued = db.get_job(job_id)

    if async_mode:
        accepted = build_accepted_response(queued)
        return JSONResponse(status_code=202, content=accepted.model_dump())

    if stream:
        return stream_response_for_job(
            job_id=job_id,
            model=model,
            response_type=response_type,
            combined_in_message=combined_in_message,
        )

    waited = wait_for_visible_or_terminal_state(job_id, timeout_seconds=settings.api_wait_timeout_seconds)
    return build_response_for_job(
        job=waited,
        model=model,
        stream=stream,
        response_type=response_type,
        combined_in_message=combined_in_message,
    )


def build_response_for_mode(
    job: dict[str, Any],
    model: str,
    stream: bool,
    response_type: Literal["chat", "generate"],
    async_mode: bool,
    combined_in_message: bool = False,
):
    status = str(job.get("status") or "")
    if async_mode or status in TERMINAL_JOB_STATUSES or status in NON_TERMINAL_VISIBLE_STATUSES:
        return build_response_for_job(
            job=job,
            model=model,
            stream=stream,
            response_type=response_type,
            combined_in_message=combined_in_message,
        )
    if stream:
        return stream_response_for_job(
            job_id=job["job_id"],
            model=model,
            response_type=response_type,
            combined_in_message=combined_in_message,
        )
    waited = wait_for_visible_or_terminal_state(job["job_id"], timeout_seconds=settings.api_wait_timeout_seconds)
    return build_response_for_job(
        job=waited,
        model=model,
        stream=stream,
        response_type=response_type,
        combined_in_message=combined_in_message,
    )


def build_response_for_job(
    job: dict[str, Any],
    model: str,
    stream: bool,
    response_type: Literal["chat", "generate"],
    combined_in_message: bool = False,
):
    status = str(job.get("status") or "")
    if status == "completed":
        return build_success_response(
            job=job,
            model=model,
            stream=stream,
            response_type=response_type,
            combined_in_message=combined_in_message,
        )
    if status == "needs_clarification":
        return build_clarification_response(job=job, model=model, response_type=response_type, stream=stream)
    if status in {"failed", "expired"}:
        error_payload = job.get("error_json") or {"message": f"job {status}"}
        raise HTTPException(
            status_code=http_status_for_job_error(status=status, error_payload=error_payload),
            detail=error_payload,
        )
    accepted = build_accepted_response(job)
    return JSONResponse(status_code=202, content=accepted.model_dump())


def build_success_response(
    job: dict[str, Any],
    model: str,
    stream: bool,
    response_type: Literal["chat", "generate"],
    combined_in_message: bool = False,
):
    response_json = job.get("response_json") or {}
    combined_payload = load_combined_payload(job["job_id"], response_json=response_json)
    assistant_source = combined_payload if isinstance(combined_payload, dict) else response_json
    assistant_content = extract_assistant_content(assistant_source)
    created_at = job.get("completed_at") or db.utcnow_iso()

    routing_metadata = extract_routing_metadata(job)
    execution = extract_execution_payload(job)
    stages = extract_stage_payload(job)

    if response_type == "chat":
        payload = ChatResponse(
            model=model,
            created_at=created_at,
            message={"role": "assistant", "content": assistant_content},
            done=True,
            job_id=job["job_id"],
            combined=combined_payload if not combined_in_message else None,
        )
        dumped = payload.model_dump()
    else:
        payload = GenerateResponse(
            model=model,
            created_at=created_at,
            response=assistant_content,
            done=True,
            job_id=job["job_id"],
            combined=combined_payload if not combined_in_message else None,
        )
        dumped = payload.model_dump()

    dumped["intent_type"] = routing_metadata.get("intent_type")
    dumped["task_type"] = routing_metadata.get("task_type")
    dumped["current_stage"] = routing_metadata.get("route_state") or job.get("status")
    if execution is not None:
        dumped["execution"] = execution
    if stages is not None:
        dumped["stages"] = stages
    if combined_in_message and isinstance(combined_payload, dict):
        if response_type == "chat":
            dumped["message"]["content"] = serialize_combined_payload(combined_payload)
        else:
            dumped["response"] = serialize_combined_payload(combined_payload)

    if stream:
        return ndjson_response(dumped)
    return JSONResponse(status_code=200, content=dumped)


def build_clarification_response(
    job: dict[str, Any],
    model: str,
    response_type: Literal["chat", "generate"],
    stream: bool = False,
):
    response_json = job.get("response_json") or {}
    content = extract_assistant_content(response_json) or "I need a bit more information before I can do that."
    created_at = db.utcnow_iso()
    routing_metadata = extract_routing_metadata(job)
    payload = {
        "model": model,
        "created_at": created_at,
        "done": False,
        "job_id": job["job_id"],
        "status": "needs_clarification",
        "intent_type": routing_metadata.get("intent_type"),
        "task_type": routing_metadata.get("task_type"),
        "current_stage": "needs_clarification",
    }
    if response_type == "chat":
        payload["message"] = {"role": "assistant", "content": content}
    else:
        payload["response"] = content
    if stream:
        return ndjson_response(payload)
    return JSONResponse(status_code=202, content=payload)


def build_accepted_response(job: dict[str, Any]) -> AcceptedResponse:
    position = db.count_queue_position(job["job_id"])
    active_job_id = db.get_active_job_id()
    return AcceptedResponse(
        job_id=job["job_id"],
        status=job["status"],
        done=False,
        position=position,
        active_job_id=active_job_id,
    )


def build_job_status(job: dict[str, Any]) -> JobStatusResponse:
    result = job["response_json"] if job["status"] in {"completed", "needs_clarification"} else None
    combined = load_combined_payload(job["job_id"], response_json=result) if job["status"] == "completed" else None
    if job["status"] == "completed":
        include_combined_in_message = request_wants_combined_in_message(job["request_json"])
        result = merge_combined_into_result_payload(result, combined, include_combined_in_message)
    error = job["error_json"] if job["status"] in {"failed", "expired"} else None
    payload = JobStatusResponse(
        job_id=job["job_id"],
        status=job["status"],
        done=job["status"] in TERMINAL_JOB_STATUSES,
        position=db.count_queue_position(job["job_id"]),
        active_job_id=db.get_active_job_id(),
        model=job["request_json"].get("model"),
        created_at=job["created_at"],
        started_at=job["started_at"],
        completed_at=job["completed_at"],
        result=result,
        combined=combined,
        error=error,
    )
    dumped = payload.model_dump()
    routing_metadata = extract_routing_metadata(job)
    dumped["intent_type"] = routing_metadata.get("intent_type")
    dumped["task_type"] = routing_metadata.get("task_type")
    dumped["current_stage"] = routing_metadata.get("route_state") or job.get("status")
    execution = extract_execution_payload(job)
    if execution is not None:
        dumped["execution"] = execution
    stages = extract_stage_payload(job)
    if stages is not None:
        dumped["stages"] = stages
    return JSONResponse(status_code=200, content=dumped)


ALLOWED_CHAT_ROLES = {"system", "user", "assistant", "tool"}


def is_openclaw_compat_model(model_name: str) -> bool:
    model = str(model_name or "").strip().lower()
    if not model:
        return False
    compat = {name.strip().lower() for name in settings.openclaw_compat_models() if str(name).strip()}
    if model in compat:
        return True
    tail = model.split("/")[-1]
    return tail in compat


def resolve_openclaw_effective_model(model_name: str) -> str:
    requested = str(model_name or "").strip()
    configured_default = str(settings.openclaw_default_model or "").strip()
    if not requested:
        return configured_default
    if not settings.openclaw_force_default_model:
        return requested
    if not is_openclaw_compat_model(requested):
        return requested
    return configured_default or requested


def normalize_chat_role(raw_role: Any, compat_mode: bool) -> str:
    role = str(raw_role or "").strip().lower()
    if role in ALLOWED_CHAT_ROLES:
        return role
    if compat_mode and role == "developer":
        return "system"
    if compat_mode and role:
        return "user"
    raise HTTPException(
        status_code=400,
        detail={"code": "INVALID_REQUEST", "message": f"Unsupported chat role: {raw_role!r}"},
    )


def extract_text_content(raw_content: Any) -> str:
    if raw_content is None:
        return ""
    if isinstance(raw_content, str):
        return raw_content.strip()
    if isinstance(raw_content, (int, float, bool)):
        return str(raw_content).strip()
    if isinstance(raw_content, list):
        parts: list[str] = []
        for item in raw_content:
            text = extract_text_content(item)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()
    if isinstance(raw_content, dict):
        if "text" in raw_content:
            return extract_text_content(raw_content.get("text"))
        if "content" in raw_content:
            return extract_text_content(raw_content.get("content"))
        if "message" in raw_content and isinstance(raw_content["message"], dict):
            nested = raw_content["message"]
            if "content" in nested:
                return extract_text_content(nested.get("content"))
        return json.dumps(raw_content, ensure_ascii=False).strip()
    return str(raw_content).strip()


def normalize_chat_request(request: ChatRequest) -> dict[str, Any]:
    compat_mode = is_openclaw_compat_model(request.model)
    messages: list[dict[str, str]] = []

    for idx, message in enumerate(request.messages):
        role = normalize_chat_role(message.role, compat_mode=compat_mode)
        raw_content = message.content
        if not compat_mode and not isinstance(raw_content, str):
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_REQUEST", "message": f"messages[{idx}].content must be a string"},
            )
        content = extract_text_content(raw_content)
        if not content:
            if compat_mode or role in {"assistant", "tool", "system"}:
                continue
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_REQUEST", "message": f"messages[{idx}].content must not be empty"},
            )
        messages.append({"role": role, "content": content})

    if not messages:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_REQUEST", "message": "No usable non-empty messages were provided"},
        )

    target_idx = next((idx for idx in range(len(messages) - 1, -1, -1) if messages[idx]["role"] == "user"), len(messages) - 1)
    chunked = chunk_prompt_if_needed(messages[target_idx]["content"])

    normalized = {
        "request_type": "chat",
        "model": request.model,
        "messages": messages,
        "stream": bool(request.stream),
        "combined_in_message": wants_combined_in_message(request.options, request.format),
        "user_prompt": chunked["original"],
        "transport": {
            "user_prompt_chunks": chunked["chunks"],
            "chunking": {
                "enabled": chunked["chunk_count"] > 1,
                "chunk_size_words": settings.prompt_chunk_words,
                "max_chunks": settings.prompt_max_chunks,
                "word_count": chunked["word_count"],
                "chunk_count": chunked["chunk_count"],
            },
        },
    }
    if request.format is not None:
        normalized["format"] = request.format
    if request.options is not None:
        normalized["options"] = request.options
    return normalized


def normalize_generate_request(request: GenerateRequest) -> dict[str, Any]:
    chunked = chunk_prompt_if_needed(request.prompt)
    messages: list[dict[str, str]] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    messages.append({"role": "user", "content": chunked["original"]})

    normalized = {
        "request_type": "generate",
        "model": request.model,
        "messages": messages,
        "stream": bool(request.stream),
        "prompt": chunked["original"],
        "transport": {
            "prompt_chunks": chunked["chunks"],
            "chunking": {
                "enabled": chunked["chunk_count"] > 1,
                "chunk_size_words": settings.prompt_chunk_words,
                "max_chunks": settings.prompt_max_chunks,
                "word_count": chunked["word_count"],
                "chunk_count": chunked["chunk_count"],
            },
        },
    }
    optional_fields = ("suffix", "template", "context", "raw", "keep_alive", "options")
    for field_name in optional_fields:
        value = getattr(request, field_name)
        if value is not None:
            normalized[field_name] = value
    return normalized


def extract_primary_prompt_text(normalized_request: dict[str, Any]) -> str:
    return str(normalized_request.get("user_prompt") or normalized_request.get("prompt") or "").strip()


def is_heartbeat_control_prompt(prompt_text: str) -> bool:
    lowered = (prompt_text or "").strip().lower()
    if not lowered:
        return False

    strong_markers = (
        "read heartbeat.md if it exists",
        "if nothing needs attention, reply heartbeat_ok",
        "heartbeat prompt:",
    )
    if any(marker in lowered for marker in strong_markers):
        return True

    markers = (
        "heartbeat.md",
        "heartbeat_ok",
        "do not infer or repeat old tasks",
        "if nothing needs attention",
    )
    score = sum(1 for marker in markers if marker in lowered)
    return score >= 2


def build_heartbeat_ack_response(
    *,
    model: str,
    response_type: Literal["chat", "generate"],
    stream: bool,
    request_hash: str,
    active_job_id: str | None,
    reason: str,
) -> JSONResponse | StreamingResponse:
    created_at = db.utcnow_iso()
    synthetic_job_id = f"heartbeat_{request_hash[:12]}"
    execution_meta = {"mode": "heartbeat_short_circuit", "reason": reason}

    if response_type == "chat":
        payload = {
            "model": model,
            "created_at": created_at,
            "message": {"role": "assistant", "content": settings.heartbeat_ack_text},
            "done": True,
            "job_id": synthetic_job_id,
            "intent_type": "heartbeat",
            "task_type": "heartbeat_poll",
            "current_stage": "heartbeat_short_circuit",
            "active_job_id": active_job_id,
            "execution": execution_meta,
        }
    else:
        payload = {
            "model": model,
            "created_at": created_at,
            "response": settings.heartbeat_ack_text,
            "done": True,
            "job_id": synthetic_job_id,
            "intent_type": "heartbeat",
            "task_type": "heartbeat_poll",
            "current_stage": "heartbeat_short_circuit",
            "active_job_id": active_job_id,
            "execution": execution_meta,
        }

    if stream:
        return ndjson_response(payload)
    return JSONResponse(status_code=200, content=payload)


def maybe_short_circuit_heartbeat(
    *,
    normalized_request: dict[str, Any],
    request_hash: str,
    model: str,
    response_type: Literal["chat", "generate"],
    stream: bool,
) -> JSONResponse | StreamingResponse | None:
    if not settings.heartbeat_short_circuit_enabled:
        return None

    prompt_text = extract_primary_prompt_text(normalized_request)
    if not is_heartbeat_control_prompt(prompt_text):
        return None

    global _HEARTBEAT_LAST_ACK_HASH, _HEARTBEAT_LAST_ACK_AT_MONO
    now_mono = time.monotonic()
    cooldown_seconds = max(0, int(settings.heartbeat_cooldown_seconds))
    active_job_id = db.get_active_job_id()

    reason = "short_circuit"
    if settings.heartbeat_defer_when_busy and active_job_id:
        reason = "deferred_busy_queue"
    elif (
        cooldown_seconds > 0
        and _HEARTBEAT_LAST_ACK_HASH == request_hash
        and (now_mono - _HEARTBEAT_LAST_ACK_AT_MONO) < cooldown_seconds
    ):
        reason = "deduped_within_cooldown"

    _HEARTBEAT_LAST_ACK_HASH = request_hash
    _HEARTBEAT_LAST_ACK_AT_MONO = now_mono
    logger.info(
        "heartbeat short-circuited",
        extra={"reason": reason, "active_job_id": active_job_id, "model": model},
    )
    return build_heartbeat_ack_response(
        model=model,
        response_type=response_type,
        stream=stream,
        request_hash=request_hash,
        active_job_id=active_job_id,
        reason=reason,
    )


def classify_route_hint(normalized_request: dict[str, Any]) -> str | None:
    user_text = extract_primary_prompt_text(normalized_request).lower()
    if not user_text:
        return None
    # Cron weather executions should follow the normal question lane,
    # so they get the same retry/fallback behavior as Telegram asks.
    if "[cron:" in user_text and any(k in user_text for k in ("weather", "forecast", "temperature")):
        return "question"
    system_test_markers = ("reply with exactly", "respond with exactly", "return exactly", "output exactly")
    if any(marker in user_text for marker in system_test_markers):
        return "system_test"
    research_markers = ("research", "investigate", "deeply compare", "build a report", "from many angles")
    if any(marker in user_text for marker in research_markers):
        return "research"
    job_markers = ("set up", "schedule", "remind me", "configure", "create", "cron", "job", "run this daily")
    if any(marker in user_text for marker in job_markers):
        return "job"
    question_markers = ("what", "which", "when", "where", "why", "how", "?")
    if any(marker in user_text for marker in question_markers):
        return "question"
    return None


def suggest_task_type(normalized_request: dict[str, Any], route_hint: str | None) -> str:
    text = str(normalized_request.get("user_prompt") or normalized_request.get("prompt") or "").lower()
    if route_hint == "system_test":
        return "system_command"
    if route_hint == "question":
        if any(k in text for k in ("weather", "forecast", "temperature")):
            return "information"
        return "general_question"
    if route_hint == "job":
        if "weather" in text and ("daily" in text or "every" in text):
            return "scheduled_weather_report"
        if "remind" in text:
            return "reminder"
        if "file" in text or "write" in text:
            return "file_write"
        return "generic_job"
    if route_hint == "research":
        return "root_topic_research"
    if "pizza" in text:
        return "recommendation_lookup"
    return "general_question"


def sha256_json(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_combined_payload(payload: dict[str, Any]) -> bool:
    return isinstance(payload, dict) and "response" in payload and ("evaluation" in payload or "score_summary" in payload)


def load_combined_payload(job_id: str, response_json: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if isinstance(response_json, dict) and is_combined_payload(response_json):
        return response_json
    combined_path = settings.repo_path / settings.combined_dir / f"{job_id}.json"
    if not combined_path.exists():
        return None
    try:
        payload = json.loads(combined_path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("combined artifact was not valid json", extra={"job_id": job_id, "path": str(combined_path)})
        return None
    return payload if isinstance(payload, dict) else None


def extract_assistant_content(response_json: dict[str, Any]) -> str:
    if is_combined_payload(response_json):
        nested = response_json.get("response")
        if isinstance(nested, dict):
            return extract_assistant_content(nested)
        if isinstance(nested, str):
            return nested
    if "message" in response_json and isinstance(response_json["message"], dict):
        return str(response_json["message"].get("content", ""))
    if "response" in response_json:
        if isinstance(response_json["response"], dict):
            nested = response_json["response"]
            if "message" in nested and isinstance(nested["message"], dict):
                return str(nested["message"].get("content", ""))
            if "content" in nested:
                return str(nested["content"])
            return json.dumps(nested)
        return str(response_json["response"])
    if "content" in response_json:
        return str(response_json["content"])
    return json.dumps(response_json)


def as_bool_flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    return None


def wants_combined_in_message(options: dict[str, Any] | None, format_value: Any = None) -> bool:
    if isinstance(options, dict):
        for key in ("return_combined", "include_combined", "combined_in_message"):
            flag = as_bool_flag(options.get(key))
            if flag is not None:
                return flag
        mode = options.get("response_mode")
        if isinstance(mode, str) and mode.strip().lower() in {"combined", "combined_json", "full_combined"}:
            return True
    if isinstance(format_value, str) and format_value.strip().lower() in {"combined", "combined_json", "full_combined"}:
        return True
    return False


def request_wants_combined_in_message(request_json: dict[str, Any] | None) -> bool:
    if not isinstance(request_json, dict):
        return False
    explicit = as_bool_flag(request_json.get("combined_in_message"))
    if explicit is not None:
        return explicit
    options = request_json.get("options")
    options_obj = options if isinstance(options, dict) else None
    return wants_combined_in_message(options_obj, request_json.get("format"))


def serialize_combined_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def merge_combined_into_result_payload(
    result: dict[str, Any] | None,
    combined: dict[str, Any] | None,
    include_combined_in_message: bool,
) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return result
    if not include_combined_in_message or not isinstance(combined, dict):
        return result
    merged = dict(result)
    combined_text = serialize_combined_payload(combined)
    message = merged.get("message")
    if isinstance(message, dict):
        new_message = dict(message)
        new_message["content"] = combined_text
        merged["message"] = new_message
    elif "response" in merged:
        merged["response"] = combined_text
    else:
        merged["message"] = {"role": "assistant", "content": combined_text}
    merged["combined"] = combined
    return merged


def extract_routing_metadata(job: dict[str, Any]) -> dict[str, Any]:
    request_json = job.get("request_json") or {}
    routing = request_json.get("routing_metadata")
    if isinstance(routing, dict):
        return routing
    return {}


def extract_execution_payload(job: dict[str, Any]) -> dict[str, Any] | None:
    response_json = job.get("response_json") or {}
    if isinstance(response_json, dict) and isinstance(response_json.get("execution"), dict):
        return response_json["execution"]
    combined = load_combined_payload(job.get("job_id", ""), response_json=response_json)
    if isinstance(combined, dict):
        execution = combined.get("execution")
        if isinstance(execution, dict):
            return execution
    return None


def extract_stage_payload(job: dict[str, Any]) -> dict[str, Any] | None:
    response_json = job.get("response_json") or {}
    if isinstance(response_json, dict) and isinstance(response_json.get("stages"), dict):
        return response_json["stages"]
    combined = load_combined_payload(job.get("job_id", ""), response_json=response_json)
    if isinstance(combined, dict):
        stages = combined.get("stages")
        if isinstance(stages, dict):
            return stages
    return None


def resolve_idempotency_key(idempotency_key: str | None, request_hash: str) -> str:
    if idempotency_key and idempotency_key.strip():
        return idempotency_key.strip()
    return f"auto_{request_hash[:12]}_{uuid.uuid4().hex[:12]}"


def wait_for_visible_or_terminal_state(job_id: str, timeout_seconds: int) -> dict[str, Any]:
    timeout_seconds = max(0, int(timeout_seconds))
    deadline = time.monotonic() + timeout_seconds
    while True:
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        status = str(job.get("status") or "")
        if status in TERMINAL_JOB_STATUSES or status in NON_TERMINAL_VISIBLE_STATUSES:
            return job
        if time.monotonic() >= deadline:
            return job
        time.sleep(min(0.5, settings.result_poll_interval_seconds))


def stream_response_for_job(
    job_id: str,
    model: str,
    response_type: Literal["chat", "generate"],
    combined_in_message: bool = False,
) -> StreamingResponse:
    poll_interval = max(0.2, float(settings.result_poll_interval_seconds))
    heartbeat_interval = max(1.0, poll_interval)
    timeout_seconds = max(0, int(settings.api_wait_timeout_seconds))
    deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None

    def _iter() -> Any:
        next_heartbeat = 0.0
        while True:
            job = db.get_job(job_id)
            if not job:
                payload = {"error": "job not found", "code": "JOB_NOT_FOUND", "job_id": job_id, "done": True}
                if response_type == "chat":
                    payload["message"] = {"role": "assistant", "content": "job not found"}
                else:
                    payload["response"] = "job not found"
                yield json.dumps(payload) + "\n"
                return

            status = str(job.get("status") or "")
            if status in TERMINAL_JOB_STATUSES or status in NON_TERMINAL_VISIBLE_STATUSES:
                final = build_response_for_job(
                    job=job,
                    model=model,
                    stream=False,
                    response_type=response_type,
                    combined_in_message=combined_in_message,
                )
                if hasattr(final, "body"):
                    try:
                        body = final.body.decode("utf-8")
                        payload = json.loads(body)
                    except Exception:
                        payload = {"job_id": job_id, "status": status, "done": status in TERMINAL_JOB_STATUSES}
                else:
                    payload = final.model_dump() if hasattr(final, "model_dump") else final
                yield json.dumps(payload) + "\n"
                return

            now = time.monotonic()
            if now >= next_heartbeat:
                routing_metadata = extract_routing_metadata(job)
                payload = {
                    "model": model,
                    "created_at": db.utcnow_iso(),
                    "done": False,
                    "job_id": job_id,
                    "status": status,
                    "intent_type": routing_metadata.get("intent_type"),
                    "task_type": routing_metadata.get("task_type"),
                    "current_stage": routing_metadata.get("route_state") or status,
                }
                if response_type == "chat":
                    payload["message"] = {"role": "assistant", "content": " "}
                else:
                    payload["response"] = " "
                yield json.dumps(payload) + "\n"
                next_heartbeat = now + heartbeat_interval

            if deadline is not None and now >= deadline:
                message_text = "Job did not reach a visible or terminal state before API wait timeout."
                payload = {
                    "error": message_text,
                    "code": "WAIT_TIMEOUT",
                    "job_id": job_id,
                    "status": status or None,
                    "done": True,
                }
                if response_type == "chat":
                    payload["message"] = {"role": "assistant", "content": message_text}
                else:
                    payload["response"] = message_text
                yield json.dumps(payload) + "\n"
                return

            time.sleep(min(0.5, poll_interval))

    return StreamingResponse(_iter(), media_type="application/x-ndjson")


def ndjson_response(payload: dict[str, Any]) -> StreamingResponse:
    def _iter() -> Any:
        yield json.dumps(payload) + "\n"
    return StreamingResponse(_iter(), media_type="application/x-ndjson")


def http_status_for_job_error(status: str, error_payload: dict[str, Any]) -> int:
    if status == "expired":
        return 504
    code = str(error_payload.get("code", "")).upper()
    if code == "PROMPT_TOO_LARGE":
        return 400
    if code == "WEB_SEARCH_UNAVAILABLE":
        return 503
    if code in {"PIPELINE_EXECUTION_ERROR", "MODEL_EXECUTION_ERROR"}:
        return 502
    if code in {"JOB_TIMEOUT", "TIMEOUT", "TIMED_OUT"}:
        return 504
    return 500


def chunk_prompt_if_needed(text: str) -> dict[str, Any]:
    words = count_words(text)
    max_words_total = settings.prompt_chunk_words * settings.prompt_max_chunks
    if words > max_words_total:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PROMPT_TOO_LARGE",
                "message": (
                    f"Prompt has {words} words, which exceeds the configured maximum of "
                    f"{max_words_total} words ({settings.prompt_max_chunks} chunks x "
                    f"{settings.prompt_chunk_words} words)."
                ),
                "limits": {
                    "prompt_chunk_words": settings.prompt_chunk_words,
                    "prompt_max_chunks": settings.prompt_max_chunks,
                    "prompt_max_words": max_words_total,
                },
                "word_count": words,
            },
        )
    if words <= settings.prompt_chunk_words:
        return {"original": text, "chunks": [text], "word_count": words, "chunk_count": 1}
    chunks = split_text_by_word_limit(text, settings.prompt_chunk_words)
    if len(chunks) > settings.prompt_max_chunks:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PROMPT_TOO_LARGE",
                "message": f"Prompt produced {len(chunks)} chunks, which exceeds max_chunks={settings.prompt_max_chunks}.",
                "limits": {
                    "prompt_chunk_words": settings.prompt_chunk_words,
                    "prompt_max_chunks": settings.prompt_max_chunks,
                    "prompt_max_words": max_words_total,
                },
                "word_count": words,
                "chunk_count": len(chunks),
            },
        )
    return {"original": text, "chunks": chunks, "word_count": words, "chunk_count": len(chunks)}


def count_words(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def split_text_by_word_limit(text: str, words_per_chunk: int) -> list[str]:
    if words_per_chunk < 1:
        raise ValueError("words_per_chunk must be >= 1")
    parts = re.findall(r"\S+\s*", text or "")
    if not parts:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_count = 0
    for part in parts:
        current.append(part)
        current_count += 1
        if current_count >= words_per_chunk:
            chunks.append("".join(current).strip())
            current = []
            current_count = 0
    if current:
        chunks.append("".join(current).strip())
    return chunks
