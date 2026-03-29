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


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    db.requeue_inflight_jobs()
    worker.start()
    logger.info("application started")
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

        passthrough_keys = ("limits", "word_count", "chunk_count", "job_id", "status")
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
    return {
        "status": "ok",
        "app": settings.app_name,
    }


@app.post("/api/chat", response_model=ChatResponse | AcceptedResponse)
def chat(
    request: ChatRequest,
    async_mode: bool = Query(default=False, alias="async"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return handle_submission(
        normalized_request=normalize_chat_request(request),
        model=request.model,
        stream=request.stream,
        response_type="chat",
        async_mode=async_mode,
        idempotency_key=idempotency_key,
    )


@app.post("/api/generate", response_model=GenerateResponse | AcceptedResponse)
def generate(
    request: GenerateRequest,
    async_mode: bool = Query(default=False, alias="async"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return handle_submission(
        normalized_request=normalize_generate_request(request),
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


def handle_submission(
    normalized_request: dict[str, Any],
    model: str,
    stream: bool,
    response_type: Literal["chat", "generate"],
    async_mode: bool,
    idempotency_key: str | None,
):
    request_hash = sha256_json(normalized_request)
    resolved_idempotency_key = resolve_idempotency_key(idempotency_key, request_hash)

    existing = db.get_job_by_idempotency_key(resolved_idempotency_key)
    if existing:
        if existing["request_hash"] != request_hash:
            raise HTTPException(status_code=409, detail="Idempotency key was reused with a different request body")
        logger.info(
            "idempotent request reused existing job",
            extra={
                "job_id": existing["job_id"],
                "idempotency_key": resolved_idempotency_key,
                "status": existing["status"],
            },
        )
        return build_response_for_mode(
            job=existing,
            model=model,
            stream=stream,
            response_type=response_type,
            async_mode=async_mode,
        )

    job_id = db.create_job(
        idempotency_key=resolved_idempotency_key,
        request_hash=request_hash,
        request_json=normalized_request,
    )
    worker.notify()
    queued = db.get_job(job_id)
    logger.info(
        "job created",
        extra={
            "job_id": job_id,
            "idempotency_key": resolved_idempotency_key,
            "status": queued["status"],
            "position": db.count_queue_position(job_id),
            "active_job_id": db.get_active_job_id(),
        },
    )
    if async_mode:
        accepted = build_accepted_response(queued)
        return JSONResponse(status_code=202, content=accepted.model_dump())

    waited = wait_for_terminal_state(job_id, timeout_seconds=settings.api_wait_timeout_seconds)
    return build_response_for_job(job=waited, model=model, stream=stream, response_type=response_type)


def build_response_for_mode(
    job: dict[str, Any],
    model: str,
    stream: bool,
    response_type: Literal["chat", "generate"],
    async_mode: bool,
):
    status = job["status"]
    if async_mode or status in {"completed", "failed", "expired"}:
        return build_response_for_job(job=job, model=model, stream=stream, response_type=response_type)

    waited = wait_for_terminal_state(job["job_id"], timeout_seconds=settings.api_wait_timeout_seconds)
    return build_response_for_job(job=waited, model=model, stream=stream, response_type=response_type)


def build_response_for_job(
    job: dict[str, Any],
    model: str,
    stream: bool,
    response_type: Literal["chat", "generate"],
):
    status = job["status"]
    if status == "completed":
        return build_success_response(job=job, model=model, stream=stream, response_type=response_type)
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
):
    response_json = job["response_json"] or {}
    assistant_content = extract_assistant_content(response_json)
    created_at = job["completed_at"] or db.utcnow_iso()

    if response_type == "chat":
        payload = ChatResponse(
            model=model,
            created_at=created_at,
            message={"role": "assistant", "content": assistant_content},
            done=True,
            job_id=job["job_id"],
        )
    else:
        payload = GenerateResponse(
            model=model,
            created_at=created_at,
            response=assistant_content,
            done=True,
            job_id=job["job_id"],
        )

    if stream:
        return ndjson_response(payload.model_dump())
    return payload


def build_accepted_response(job: dict[str, Any]) -> AcceptedResponse:
    position = db.count_queue_position(job["job_id"])
    active_job_id = db.get_active_job_id()
    logger.info(
        "returning accepted response",
        extra={"job_id": job["job_id"], "status": job["status"], "position": position, "active_job_id": active_job_id},
    )
    return AcceptedResponse(
        job_id=job["job_id"],
        status=job["status"],
        done=False,
        position=position,
        active_job_id=active_job_id,
    )


def build_job_status(job: dict[str, Any]) -> JobStatusResponse:
    result = job["response_json"] if job["status"] == "completed" else None
    error = job["error_json"] if job["status"] in {"failed", "expired"} else None
    return JobStatusResponse(
        job_id=job["job_id"],
        status=job["status"],
        done=job["status"] == "completed",
        position=db.count_queue_position(job["job_id"]),
        active_job_id=db.get_active_job_id(),
        model=job["request_json"].get("model"),
        created_at=job["created_at"],
        started_at=job["started_at"],
        completed_at=job["completed_at"],
        result=result,
        error=error,
    )


def normalize_chat_request(request: ChatRequest) -> dict[str, Any]:
    messages = [{"role": message.role, "content": message.content} for message in request.messages]
    target_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx]["role"] == "user":
            target_idx = idx
            break
    if target_idx == -1:
        target_idx = len(messages) - 1

    chunked = chunk_prompt_if_needed(messages[target_idx]["content"])
    if chunked["chunk_count"] > 1:
        chunk_messages = [{"role": "user", "content": chunk} for chunk in chunked["chunks"]]
        messages = messages[:target_idx] + chunk_messages + messages[target_idx + 1 :]

    return {
        "request_type": "chat",
        "model": request.model,
        "messages": messages,
        "stream": bool(request.stream),
        "user_prompt": chunked["original"],
        "user_prompt_chunks": chunked["chunks"],
        "chunking": {
            "enabled": chunked["chunk_count"] > 1,
            "chunk_size_words": settings.prompt_chunk_words,
            "max_chunks": settings.prompt_max_chunks,
            "word_count": chunked["word_count"],
            "chunk_count": chunked["chunk_count"],
        },
    }


def sha256_json(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def extract_assistant_content(response_json: dict[str, Any]) -> str:
    if "message" in response_json and isinstance(response_json["message"], dict):
        return str(response_json["message"].get("content", ""))
    if "response" in response_json:
        return str(response_json["response"])
    if "content" in response_json:
        return str(response_json["content"])
    return json.dumps(response_json)


def normalize_generate_request(request: GenerateRequest) -> dict[str, Any]:
    chunked = chunk_prompt_if_needed(request.prompt)

    messages: list[dict[str, str]] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    for chunk in chunked["chunks"]:
        messages.append({"role": "user", "content": chunk})

    normalized = {
        "request_type": "generate",
        "model": request.model,
        "messages": messages,
        "stream": bool(request.stream),
        "prompt": chunked["original"],
        "prompt_chunks": chunked["chunks"],
        "chunking": {
            "enabled": chunked["chunk_count"] > 1,
            "chunk_size_words": settings.prompt_chunk_words,
            "max_chunks": settings.prompt_max_chunks,
            "word_count": chunked["word_count"],
            "chunk_count": chunked["chunk_count"],
        },
    }

    optional_fields = (
        "suffix",
        "template",
        "context",
        "raw",
        "keep_alive",
        "options",
    )
    for field_name in optional_fields:
        value = getattr(request, field_name)
        if value is not None:
            normalized[field_name] = value

    return normalized


def resolve_idempotency_key(idempotency_key: str | None, request_hash: str) -> str:
    if idempotency_key and idempotency_key.strip():
        return idempotency_key.strip()
    return f"auto_{request_hash[:12]}_{uuid.uuid4().hex[:12]}"


def wait_for_terminal_state(job_id: str, timeout_seconds: int) -> dict[str, Any]:
    timeout_seconds = max(0, int(timeout_seconds))
    deadline = time.monotonic() + timeout_seconds

    while True:
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if job["status"] in {"completed", "failed", "expired"}:
            return job
        if time.monotonic() >= deadline:
            return job
        time.sleep(min(0.5, settings.result_poll_interval_seconds))


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
                "message": (
                    f"Prompt produced {len(chunks)} chunks, which exceeds max_chunks={settings.prompt_max_chunks}."
                ),
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

