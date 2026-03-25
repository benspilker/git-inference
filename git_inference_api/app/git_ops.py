from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import settings
from .db import utcnow_iso

logger = logging.getLogger("git_inference_api.git")


class GitError(RuntimeError):
    pass


class JobTimedOutError(RuntimeError):
    pass


class JobFailedError(RuntimeError):
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        super().__init__(payload.get("message", "job failed"))


class ResultNotFoundError(RuntimeError):
    pass


class RepoFileLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self) -> "RepoFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+", encoding="utf-8")
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"pid={os.getpid()}\n")
        self.handle.flush()
        import fcntl

        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        logger.info("repo lock acquired", extra={"path": str(self.path)})
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.handle is None:
            return
        import fcntl

        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        logger.info("repo lock released", extra={"path": str(self.path)})
        self.handle.close()
        self.handle = None


REPO_LOCK = RepoFileLock(settings.repo_lock_path)


def run_git(*args: str, check: bool = True, retryable: bool = True) -> subprocess.CompletedProcess[str]:
    last_result: subprocess.CompletedProcess[str] | None = None
    max_attempts = settings.git_max_retries if retryable else 1

    for attempt in range(1, max_attempts + 1):
        cmd = ["git", *args]
        result = subprocess.run(
            cmd,
            cwd=settings.repo_path,
            text=True,
            capture_output=True,
            env={
                **dict(os.environ),
                "GIT_AUTHOR_NAME": settings.git_author_name,
                "GIT_AUTHOR_EMAIL": settings.git_author_email,
                "GIT_COMMITTER_NAME": settings.git_author_name,
                "GIT_COMMITTER_EMAIL": settings.git_author_email,
            },
        )
        last_result = result
        if result.returncode == 0:
            if attempt > 1:
                logger.info(
                    "git command succeeded after retry",
                    extra={"operation": " ".join(args), "attempt": attempt, "max_attempts": max_attempts},
                )
            return result

        message = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        logger.warning(
            "git command failed",
            extra={"operation": " ".join(args), "attempt": attempt, "max_attempts": max_attempts},
        )
        if not check and attempt == max_attempts:
            return result
        if attempt < max_attempts:
            time.sleep(settings.git_retry_delay_seconds * attempt)
            continue
        if check:
            raise GitError(f"git {' '.join(args)} failed after {max_attempts} attempts: {message}")

    assert last_result is not None
    return last_result


def ensure_repo_ready() -> None:
    settings.ensure_directories()
    if settings.auto_init_repo and not (settings.repo_path / ".git").exists():
        run_git("init", "-b", settings.branch, retryable=False)
        logger.info("initialized repo", extra={"path": str(settings.repo_path)})


def sync_repo_to_remote_head() -> None:
    run_git("fetch", "origin", settings.branch)
    run_git("reset", "--hard", f"origin/{settings.branch}")
    run_git("clean", "-fd")


def write_request_artifact(job_id: str, request_json: dict[str, Any]) -> Path:
    request_path = settings.repo_path / settings.requests_dir / f"{job_id}.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "job_id": job_id,
        "created_at": utcnow_iso(),
        "type": str(request_json.get("request_type", "chat")),
        "system_prompt": _extract_system_prompt(request_json),
        "user_prompt": _extract_user_prompt(request_json),
        "request": request_json,
    }
    chunking = request_json.get("chunking")
    if isinstance(chunking, dict):
        payload["chunking"] = chunking
    if isinstance(request_json.get("user_prompt_chunks"), list):
        payload["user_prompt_chunks"] = request_json["user_prompt_chunks"]
    if isinstance(request_json.get("prompt_chunks"), list):
        payload["prompt_chunks"] = request_json["prompt_chunks"]
    request_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    logger.info("wrote request artifact", extra={"job_id": job_id, "path": str(request_path)})
    return request_path


def commit_and_push_request(job_id: str, request_path: Path) -> None:
    run_git("add", str(request_path.relative_to(settings.repo_path)))
    commit_result = run_git("commit", "-m", f"submit inference job {job_id}", check=False, retryable=False)
    combined = (commit_result.stdout + commit_result.stderr).lower()
    if commit_result.returncode != 0 and "nothing to commit" not in combined:
        raise GitError(commit_result.stderr.strip() or commit_result.stdout.strip())
    run_git("push", "origin", settings.branch)
    logger.info("pushed request commit", extra={"job_id": job_id})


def wait_for_result(job_id: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        sync_repo_to_remote_head()

        failure_payload = try_read_failure(job_id)
        if failure_payload is not None:
            raise JobFailedError(failure_payload)

        response_payload = try_read_success(job_id)
        if response_payload is not None:
            return response_payload

        time.sleep(settings.result_poll_interval_seconds)

    raise JobTimedOutError(f"job {job_id} did not finish within {timeout_seconds} seconds")


def try_read_result(job_id: str) -> dict[str, Any]:
    sync_repo_to_remote_head()

    failure_payload = try_read_failure(job_id)
    if failure_payload is not None:
        raise JobFailedError(failure_payload)

    response_payload = try_read_success(job_id)
    if response_payload is not None:
        return response_payload

    raise ResultNotFoundError(job_id)


def try_read_success(job_id: str) -> dict[str, Any] | None:
    response_path = settings.repo_path / settings.responses_dir / f"{job_id}.json"
    status_path = settings.repo_path / settings.status_dir / f"{job_id}.json"

    if response_path.exists():
        logger.info("result artifact found", extra={"job_id": job_id, "path": str(response_path)})
        return json.loads(response_path.read_text(encoding="utf-8"))

    if status_path.exists():
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        if is_success_status_payload(payload):
            logger.info("success status artifact found", extra={"job_id": job_id, "path": str(status_path)})
            return payload

    return None


def try_read_failure(job_id: str) -> dict[str, Any] | None:
    candidates = [
        settings.repo_path / settings.errors_dir / f"{job_id}.json",
        settings.repo_path / settings.status_dir / f"{job_id}.json",
        settings.repo_path / settings.responses_dir / f"{job_id}.json",
    ]

    for path in candidates:
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        failure = parse_pipeline_failure_artifact(payload)
        if failure is not None:
            logger.warning("failure artifact found", extra={"job_id": job_id, "path": str(path)})
            return failure

    return None


def parse_pipeline_failure_artifact(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return {
            "code": "PIPELINE_ERROR",
            "message": "Pipeline failure artifact was not a JSON object",
            "details": {"raw": payload},
        }

    if "error" in payload and isinstance(payload["error"], dict):
        return normalize_failure_payload(payload["error"], outer_payload=payload)

    if payload.get("failed") is True:
        return normalize_failure_payload(payload, outer_payload=payload)

    state = str(payload.get("state", payload.get("status", ""))).lower()
    if state in {"failed", "error", "errored", "expired", "timeout", "timed_out", "cancelled", "canceled"}:
        return normalize_failure_payload(payload, outer_payload=payload)

    if payload.get("done") is False and "message" in payload and not payload.get("response"):
        return normalize_failure_payload(payload, outer_payload=payload)

    return None


def normalize_failure_payload(error_payload: dict[str, Any], outer_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    code = str(
        error_payload.get("code")
        or error_payload.get("type")
        or error_payload.get("state")
        or error_payload.get("status")
        or "PIPELINE_ERROR"
    ).upper()
    message = str(
        error_payload.get("message")
        or error_payload.get("detail")
        or error_payload.get("reason")
        or "Pipeline reported a failure"
    )

    normalized: dict[str, Any] = {
        "code": code,
        "message": message,
    }

    details = {}
    for key in ("job_id", "state", "status", "failed", "done", "completed_at", "updated_at"):
        if key in error_payload:
            details[key] = error_payload[key]
    if outer_payload is not None and outer_payload is not error_payload:
        details["artifact"] = outer_payload
    if details:
        normalized["details"] = details

    return normalized


def is_success_status_payload(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    if parse_pipeline_failure_artifact(payload) is not None:
        return False
    state = str(payload.get("state", payload.get("status", ""))).lower()
    return bool(
        payload.get("done") is True
        or state in {"completed", "succeeded", "success", "finished", "done"}
        or "message" in payload
        or "response" in payload
    )


def _extract_system_prompt(request_json: dict[str, Any]) -> str | None:
    for message in request_json.get("messages", []):
        if message.get("role") == "system":
            return message.get("content")
    return None


def _extract_user_prompt(request_json: dict[str, Any]) -> str:
    if isinstance(request_json.get("user_prompt"), str):
        return str(request_json["user_prompt"])
    if isinstance(request_json.get("prompt"), str):
        return str(request_json["prompt"])
    for message in reversed(request_json.get("messages", [])):
        if message.get("role") == "user":
            return message.get("content", "")
    return ""
