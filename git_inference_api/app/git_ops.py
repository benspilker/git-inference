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

VISIBLE_NONTERMINAL_STATES = {"needs_clarification"}
SUCCESS_STATES = {"completed", "succeeded", "success", "finished", "done"}
FAILURE_STATES = {"failed", "error", "errored", "expired", "timeout", "timed_out", "cancelled", "canceled"}


def _looks_like_source_repo(path: Path) -> bool:
    return (
        (path / ".github" / "workflows" / "process-requests.yml").exists()
        and (path / "git_inference_api" / "app" / "main.py").exists()
    )


def ensure_repo_path_safety() -> None:
    repo_path = settings.repo_path.resolve()
    if settings.allow_unsafe_repo_path:
        return
    if _looks_like_source_repo(repo_path):
        raise GitError(
            "Unsafe REPO_PATH detected. REPO_PATH appears to be the source repository checkout, "
            "but worker sync uses fetch/reset --hard/clean. Point REPO_PATH to a dedicated workrepo clone "
            "or set ALLOW_UNSAFE_REPO_PATH=true to bypass intentionally."
        )


def _git_lock_candidates() -> list[Path]:
    git_dir = settings.repo_path / ".git"
    if not git_dir.exists():
        return []

    candidates: list[Path] = [
        git_dir / "index.lock",
        git_dir / "HEAD.lock",
        git_dir / "packed-refs.lock",
        git_dir / "refs" / "heads" / f"{settings.branch}.lock",
    ]

    refs_heads = git_dir / "refs" / "heads"
    refs_remotes = git_dir / "refs" / "remotes"
    if refs_heads.exists():
        candidates.extend(refs_heads.rglob("*.lock"))
    if refs_remotes.exists():
        candidates.extend(refs_remotes.rglob("*.lock"))

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _has_active_git_process() -> bool:
    try:
        result = subprocess.run(["ps", "-eo", "comm"], capture_output=True, text=True, check=False)
    except Exception:
        return False
    if result.returncode != 0:
        return False
    lines = [line.strip().lower() for line in result.stdout.splitlines() if line.strip()]
    return any(name == "git" for name in lines)


def cleanup_stale_git_locks(force: bool = False) -> list[str]:
    if not settings.enable_stale_git_lock_cleanup and not force:
        return []

    if _has_active_git_process() and not force:
        logger.info("skip git lock cleanup because an active git process was detected")
        return []

    stale_after = max(0, int(settings.git_lock_stale_seconds))
    now = time.time()
    removed: list[str] = []

    for lock_path in _git_lock_candidates():
        if not lock_path.exists():
            continue
        try:
            age_seconds = now - lock_path.stat().st_mtime
        except FileNotFoundError:
            continue

        if not force and age_seconds < stale_after:
            continue

        try:
            lock_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("failed to remove stale git lock", extra={"path": str(lock_path), "error": str(exc)})
            continue

        removed.append(str(lock_path))
        logger.warning("removed stale git lock", extra={"path": str(lock_path), "age_seconds": int(age_seconds)})

    return removed


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

        lowered = message.lower()
        if "lock" in lowered and ("cannot lock ref" in lowered or "unable to create" in lowered):
            removed = cleanup_stale_git_locks(force=False)
            if removed:
                logger.warning("cleaned stale git locks after git failure", extra={"removed": removed})

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
    ensure_repo_path_safety()
    settings.ensure_directories()
    required_dirs = [
        settings.requests_dir,
        settings.responses_dir,
        settings.errors_dir,
        settings.combined_dir,
        settings.status_dir,
        "stages",
        "execution",
    ]
    for rel in required_dirs:
        (settings.repo_path / rel).mkdir(parents=True, exist_ok=True)
    if settings.auto_init_repo and not (settings.repo_path / ".git").exists():
        run_git("init", "-b", settings.branch, retryable=False)
        logger.info("initialized repo", extra={"path": str(settings.repo_path)})


def sync_repo_to_remote_head() -> None:
    cleanup_stale_git_locks(force=False)
    run_git("fetch", "origin", settings.branch)
    cleanup_stale_git_locks(force=False)
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
        "routing_metadata": request_json.get("routing_metadata"),
        "transport": request_json.get("transport"),
    }
    request_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    logger.info("wrote request artifact", extra={"job_id": job_id, "path": str(request_path)})
    return request_path


def write_stage_request_artifact(job_id: str, stage_name: str, payload: dict[str, Any]) -> Path:
    stage_dir = settings.repo_path / "stages" / job_id
    stage_dir.mkdir(parents=True, exist_ok=True)
    stage_path = stage_dir / f"{stage_name}.request.json"
    wrapped = {
        "job_id": job_id,
        "stage_name": stage_name,
        "created_at": utcnow_iso(),
        "payload": payload,
    }
    stage_path.write_text(json.dumps(wrapped, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    logger.info("wrote stage request artifact", extra={"job_id": job_id, "stage": stage_name, "path": str(stage_path)})
    return stage_path


def commit_and_push_paths(job_id: str, paths: list[Path], message: str) -> None:
    rels = [str(p.relative_to(settings.repo_path)) for p in paths]
    if not rels:
        return
    run_git("add", *rels)
    commit_result = run_git("commit", "-m", message, check=False, retryable=False)
    combined = (commit_result.stdout + commit_result.stderr).lower()
    if commit_result.returncode != 0 and "nothing to commit" not in combined:
        raise GitError(commit_result.stderr.strip() or commit_result.stdout.strip())
    run_git("push", "origin", settings.branch)
    logger.info("pushed commit", extra={"job_id": job_id, "paths": rels})


def commit_and_push_request(job_id: str, request_path: Path) -> None:
    commit_and_push_paths(job_id, [request_path], f"submit inference job {job_id}")


def commit_and_push_stage_request(job_id: str, stage_name: str, stage_path: Path) -> None:
    commit_and_push_paths(job_id, [stage_path], f"submit {stage_name} stage for {job_id}")


def _run_git_in_repo(
    repo_path: Path,
    *args: str,
    check: bool = True,
    retryable: bool = True,
) -> subprocess.CompletedProcess[str]:
    last_result: subprocess.CompletedProcess[str] | None = None
    max_attempts = settings.git_max_retries if retryable else 1

    for attempt in range(1, max_attempts + 1):
        cmd = ["git", *args]
        result = subprocess.run(
            cmd,
            cwd=repo_path,
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
            return result
        if not check and attempt == max_attempts:
            return result
        if attempt < max_attempts:
            time.sleep(settings.git_retry_delay_seconds * attempt)
            continue

        message = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise GitError(f"git {' '.join(args)} failed after {max_attempts} attempts in {repo_path}: {message}")

    assert last_result is not None
    return last_result


def sync_repo_to_remote_head_for(repo_path: Path, branch: str) -> None:
    repo = Path(repo_path)
    branch_name = str(branch or settings.branch).strip() or settings.branch
    _run_git_in_repo(repo, "fetch", "origin", branch_name)
    _run_git_in_repo(repo, "checkout", "-B", branch_name, f"origin/{branch_name}", retryable=False)
    _run_git_in_repo(repo, "reset", "--hard", f"origin/{branch_name}")
    _run_git_in_repo(repo, "clean", "-fd")


def write_request_artifact_for(job_id: str, request_json: dict[str, Any], repo_path: Path) -> Path:
    repo = Path(repo_path)
    request_path = repo / settings.requests_dir / f"{job_id}.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "job_id": job_id,
        "created_at": utcnow_iso(),
        "type": str(request_json.get("request_type", "chat")),
        "system_prompt": _extract_system_prompt(request_json),
        "user_prompt": _extract_user_prompt(request_json),
        "request": request_json,
        "routing_metadata": request_json.get("routing_metadata"),
        "transport": request_json.get("transport"),
    }
    request_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    logger.info("wrote request artifact", extra={"job_id": job_id, "path": str(request_path), "branch": "custom"})
    return request_path


def commit_and_push_request_for(job_id: str, request_path: Path, repo_path: Path, branch: str) -> None:
    repo = Path(repo_path)
    branch_name = str(branch or settings.branch).strip() or settings.branch
    rel = str(Path(request_path).relative_to(repo))
    _run_git_in_repo(repo, "add", rel)
    commit_result = _run_git_in_repo(repo, "commit", "-m", f"submit inference job {job_id}", check=False, retryable=False)
    combined = (commit_result.stdout + commit_result.stderr).lower()
    if commit_result.returncode != 0 and "nothing to commit" not in combined:
        raise GitError(commit_result.stderr.strip() or commit_result.stdout.strip())
    _run_git_in_repo(repo, "push", "origin", f"HEAD:{branch_name}")
    logger.info("pushed commit", extra={"job_id": job_id, "path": rel, "branch": branch_name, "repo_path": str(repo)})


def _try_read_stage_result_in(repo_path: Path, job_id: str, stage_name: str) -> dict[str, Any] | None:
    repo = Path(repo_path)
    candidates = [
        repo / "stages" / job_id / f"{stage_name}.result.json",
        repo / "stages" / job_id / f"{stage_name}.json",
    ]
    for path in candidates:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            logger.info("stage result artifact found", extra={"job_id": job_id, "stage": stage_name, "path": str(path)})
            return payload
    txt_path = repo / "stages" / job_id / f"{stage_name}.txt"
    if txt_path.exists():
        raw = txt_path.read_text(encoding="utf-8")
        payload = {"response": raw}
        logger.info(
            "stage text artifact found; wrapped as response",
            extra={"job_id": job_id, "stage": stage_name, "path": str(txt_path)},
        )
        return payload
    return None


def _try_read_success_in(repo_path: Path, job_id: str) -> dict[str, Any] | None:
    repo = Path(repo_path)
    response_path = repo / settings.responses_dir / f"{job_id}.json"
    status_path = repo / settings.status_dir / f"{job_id}.json"
    combined_path = repo / settings.combined_dir / f"{job_id}.json"

    if response_path.exists():
        logger.info("result artifact found", extra={"job_id": job_id, "path": str(response_path)})
        return json.loads(response_path.read_text(encoding="utf-8"))

    if combined_path.exists():
        payload = json.loads(combined_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            nested_response = payload.get("response")
            if isinstance(nested_response, dict):
                logger.info("combined response artifact found", extra={"job_id": job_id, "path": str(combined_path)})
                return nested_response
            if isinstance(nested_response, str) and nested_response.strip():
                logger.info("combined string response artifact found", extra={"job_id": job_id, "path": str(combined_path)})
                return {"message": {"role": "assistant", "content": nested_response}, "done": True}

    if status_path.exists():
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        if is_success_status_payload(payload):
            logger.info("success status artifact found", extra={"job_id": job_id, "path": str(status_path)})
            return payload

    return None


def _try_read_clarification_in(repo_path: Path, job_id: str) -> dict[str, Any] | None:
    repo = Path(repo_path)
    status_path = repo / settings.status_dir / f"{job_id}.json"
    response_path = repo / settings.responses_dir / f"{job_id}.json"

    for path in (status_path, response_path):
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = str(payload.get("state", payload.get("status", ""))).lower()
        if state in VISIBLE_NONTERMINAL_STATES:
            logger.info("clarification artifact found", extra={"job_id": job_id, "path": str(path)})
            return payload
        if payload.get("needs_clarification") is True:
            logger.info("clarification payload found", extra={"job_id": job_id, "path": str(path)})
            return payload

    return None


def _try_read_failure_in(repo_path: Path, job_id: str) -> dict[str, Any] | None:
    repo = Path(repo_path)
    candidates = [
        repo / settings.errors_dir / f"{job_id}.json",
        repo / settings.status_dir / f"{job_id}.json",
        repo / settings.responses_dir / f"{job_id}.json",
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


def wait_for_result_for(
    job_id: str,
    timeout_seconds: int,
    *,
    repo_path: Path,
    branch: str,
    repo_lock: RepoFileLock | None = None,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    repo = Path(repo_path)
    branch_name = str(branch or settings.branch).strip() or settings.branch

    while time.time() < deadline:
        if repo_lock is None:
            sync_repo_to_remote_head_for(repo, branch_name)
            failure_payload = _try_read_failure_in(repo, job_id)
            if failure_payload is not None:
                raise JobFailedError(failure_payload)
            clarification_payload = _try_read_clarification_in(repo, job_id)
            if clarification_payload is not None:
                return clarification_payload
            response_payload = _try_read_success_in(repo, job_id)
            if response_payload is not None:
                return response_payload
        else:
            with repo_lock:
                sync_repo_to_remote_head_for(repo, branch_name)
                failure_payload = _try_read_failure_in(repo, job_id)
                if failure_payload is not None:
                    raise JobFailedError(failure_payload)
                clarification_payload = _try_read_clarification_in(repo, job_id)
                if clarification_payload is not None:
                    return clarification_payload
                response_payload = _try_read_success_in(repo, job_id)
                if response_payload is not None:
                    return response_payload

        time.sleep(settings.result_poll_interval_seconds)

    raise JobTimedOutError(f"job {job_id} did not finish within {timeout_seconds} seconds")


def wait_for_result(job_id: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        sync_repo_to_remote_head()

        failure_payload = try_read_failure(job_id)
        if failure_payload is not None:
            raise JobFailedError(failure_payload)

        clarification_payload = try_read_clarification(job_id)
        if clarification_payload is not None:
            return clarification_payload

        response_payload = try_read_success(job_id)
        if response_payload is not None:
            return response_payload

        time.sleep(settings.result_poll_interval_seconds)

    raise JobTimedOutError(f"job {job_id} did not finish within {timeout_seconds} seconds")


def wait_for_stage_result(job_id: str, stage_name: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        sync_repo_to_remote_head()

        failure_payload = try_read_failure(job_id)
        if failure_payload is not None:
            raise JobFailedError(failure_payload)

        stage_payload = try_read_stage_result(job_id, stage_name)
        if stage_payload is not None:
            return stage_payload

        time.sleep(settings.result_poll_interval_seconds)

    raise JobTimedOutError(f"job {job_id} stage {stage_name} did not finish within {timeout_seconds} seconds")


def try_read_result(job_id: str) -> dict[str, Any]:
    sync_repo_to_remote_head()

    failure_payload = try_read_failure(job_id)
    if failure_payload is not None:
        raise JobFailedError(failure_payload)

    clarification_payload = try_read_clarification(job_id)
    if clarification_payload is not None:
        return clarification_payload

    response_payload = try_read_success(job_id)
    if response_payload is not None:
        return response_payload

    raise ResultNotFoundError(job_id)


def try_read_stage_result(job_id: str, stage_name: str) -> dict[str, Any] | None:
    candidates = [
        settings.repo_path / "stages" / job_id / f"{stage_name}.result.json",
        settings.repo_path / "stages" / job_id / f"{stage_name}.json",
    ]
    for path in candidates:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            logger.info("stage result artifact found", extra={"job_id": job_id, "stage": stage_name, "path": str(path)})
            return payload

    txt_path = settings.repo_path / "stages" / job_id / f"{stage_name}.txt"
    if txt_path.exists():
        raw = txt_path.read_text(encoding="utf-8")
        payload = {"response": raw}
        logger.info(
            "stage text artifact found; wrapped as response",
            extra={"job_id": job_id, "stage": stage_name, "path": str(txt_path)},
        )
        return payload
    return None


def try_read_success(job_id: str) -> dict[str, Any] | None:
    response_path = settings.repo_path / settings.responses_dir / f"{job_id}.json"
    status_path = settings.repo_path / settings.status_dir / f"{job_id}.json"
    combined_path = settings.repo_path / settings.combined_dir / f"{job_id}.json"

    if response_path.exists():
        logger.info("result artifact found", extra={"job_id": job_id, "path": str(response_path)})
        return json.loads(response_path.read_text(encoding="utf-8"))

    if combined_path.exists():
        payload = json.loads(combined_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            nested_response = payload.get("response")
            if isinstance(nested_response, dict):
                logger.info("combined response artifact found", extra={"job_id": job_id, "path": str(combined_path)})
                return nested_response
            if isinstance(nested_response, str) and nested_response.strip():
                logger.info("combined string response artifact found", extra={"job_id": job_id, "path": str(combined_path)})
                return {"message": {"role": "assistant", "content": nested_response}, "done": True}

    if status_path.exists():
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        if is_success_status_payload(payload):
            logger.info("success status artifact found", extra={"job_id": job_id, "path": str(status_path)})
            return payload

    return None


def try_read_clarification(job_id: str) -> dict[str, Any] | None:
    status_path = settings.repo_path / settings.status_dir / f"{job_id}.json"
    response_path = settings.repo_path / settings.responses_dir / f"{job_id}.json"

    for path in (status_path, response_path):
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = str(payload.get("state", payload.get("status", ""))).lower()
        if state in VISIBLE_NONTERMINAL_STATES:
            logger.info("clarification artifact found", extra={"job_id": job_id, "path": str(path)})
            return payload
        if payload.get("needs_clarification") is True:
            logger.info("clarification payload found", extra={"job_id": job_id, "path": str(path)})
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
    if state in FAILURE_STATES:
        return normalize_failure_payload(payload, outer_payload=payload)

    planner = payload.get("planner") if isinstance(payload.get("planner"), dict) else {}
    planner_intent = str(planner.get("intent_type", "")).lower()

    if (
        payload.get("done") is False
        and "message" in payload
        and not payload.get("response")
        and state not in VISIBLE_NONTERMINAL_STATES
        and payload.get("needs_clarification") is not True
        and planner_intent not in VISIBLE_NONTERMINAL_STATES
    ):
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

    normalized: dict[str, Any] = {"code": code, "message": message}

    details = {}
    for key in (
        "job_id",
        "state",
        "status",
        "failed",
        "done",
        "completed_at",
        "updated_at",
        "intent_type",
        "task_type",
        "current_stage",
    ):
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
        or state in SUCCESS_STATES
        or "message" in payload
        or "response" in payload
    ) and state not in VISIBLE_NONTERMINAL_STATES


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
