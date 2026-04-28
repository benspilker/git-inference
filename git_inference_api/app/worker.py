from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import re
import shlex
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db
from .config import settings
from .git_ops import (
    GitError,
    JobFailedError,
    JobTimedOutError,
    REPO_LOCK,
    ResultNotFoundError,
    commit_and_push_paths,
    commit_and_push_request,
    commit_and_push_request_for,
    ensure_repo_ready,
    normalize_failure_payload,
    sync_repo_to_remote_head,
    sync_repo_to_remote_head_for,
    try_read_result,
    wait_for_result,
    wait_for_result_for,
    wait_for_stage_result,
    write_request_artifact,
    write_request_artifact_for,
)
from .task_registry import canonicalize_task_type, get_task, validate_required_fields

logger = logging.getLogger("git_inference_api.worker")


class JobWorker:
    """
    V2 stage-oriented worker.

    This worker orchestrates routing, planning, deterministic local execution,
    verification, and final phrasing. Clarification is treated as a visible
    non-terminal status rather than a failure.
    """

    def __init__(self) -> None:
        self._notify_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._virtual_turn_threads_lock = threading.Lock()
        self._virtual_turn_active_jobs: set[str] = set()
        self._workspace_setup_lock = threading.Lock()
        self._base_repo_origin_url: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, name="job-worker-v2", daemon=True)
        self._thread.start()
        self._resume_allsequential_virtual_turns()
        self._resume_allparallel_virtual_turns()
        logger.info("worker started")

    def stop(self) -> None:
        self._stop_event.set()
        self._notify_event.set()
        if self._thread:
            self._thread.join(timeout=2)
        logger.info("worker stopped")

    def notify(self) -> None:
        self._notify_event.set()

    def _resume_allsequential_virtual_turns(self) -> None:
        jobs = db.list_allsequential_virtual_turns_in_progress_jobs(limit=100)
        if not jobs:
            return
        for job in jobs:
            job_id = str(job.get("job_id") or "").strip()
            if not job_id:
                continue
            self._spawn_allsequential_virtual_turns_background(job_id)
        logger.info(
            "queued allsequential virtual-turn recovery",
            extra={"recovered_jobs": [str(job.get("job_id")) for job in jobs if job.get("job_id")]},
        )

    def _resume_allparallel_virtual_turns(self) -> None:
        jobs = db.list_allparallel_virtual_turns_in_progress_jobs(limit=100)
        if not jobs:
            return
        for job in jobs:
            job_id = str(job.get("job_id") or "").strip()
            if not job_id:
                continue
            self._spawn_allparallel_virtual_turns_background(job_id)
        logger.info(
            "queued allparallel virtual-turn recovery",
            extra={"recovered_jobs": [str(job.get("job_id")) for job in jobs if job.get("job_id")]},
        )

    def _spawn_allsequential_virtual_turns_background(self, job_id: str) -> None:
        with self._virtual_turn_threads_lock:
            if job_id in self._virtual_turn_active_jobs:
                return
            self._virtual_turn_active_jobs.add(job_id)

        bg_thread = threading.Thread(
            target=self._run_allsequential_virtual_turns_background,
            name=f"allseq-vturn-{job_id[:8]}",
            daemon=True,
            args=(job_id,),
        )
        bg_thread.start()
        logger.info(
            "allsequential virtual turns background worker spawned",
            extra={"job_id": job_id, "thread_name": bg_thread.name},
        )

    def _spawn_allparallel_virtual_turns_background(self, job_id: str) -> None:
        with self._virtual_turn_threads_lock:
            if job_id in self._virtual_turn_active_jobs:
                return
            self._virtual_turn_active_jobs.add(job_id)

        bg_thread = threading.Thread(
            target=self._run_allparallel_virtual_turns_background,
            name=f"allpar-vturn-{job_id[:8]}",
            daemon=True,
            args=(job_id,),
        )
        bg_thread.start()
        logger.info(
            "allparallel virtual turns background worker spawned",
            extra={"job_id": job_id, "thread_name": bg_thread.name},
        )

    def _run_loop(self) -> None:
        repo_ready = False
        while not self._stop_event.is_set():
            try:
                if not repo_ready:
                    ensure_repo_ready()
                    repo_ready = True

                job = db.next_queued_job()
                if not job:
                    self._notify_event.wait(timeout=settings.worker_poll_interval_seconds)
                    self._notify_event.clear()
                    continue
                self._process_job(job)
            except Exception:
                repo_ready = False
                logger.exception("worker loop iteration failed")
                self._notify_event.clear()
                time.sleep(max(0.2, float(settings.worker_poll_interval_seconds)))

    def _process_job(self, job: dict[str, Any]) -> None:
        job_id = job["job_id"]
        routing = self._extract_routing_metadata(job)
        intent_type = routing.get("intent_type")
        task_type = routing.get("task_type")
        request_model = self._extract_request_model(job)

        try:
            if self._is_allparallel_model(request_model):
                self._process_job_allparallel(job, intent_type=intent_type, task_type=task_type)
                return

            if self._is_allsequential_model(request_model):
                self._process_job_allsequential(job, intent_type=intent_type, task_type=task_type)
                return

            if self._is_synthesis_model(request_model):
                self._process_job_synthesis(job, intent_type=intent_type, task_type=task_type)
                return

            if not settings.enable_stage_orchestration:
                self._process_job_single_run(job, intent_type=intent_type, task_type=task_type)
                return

            self._submit_stage_mode_request(job)
            self._update_status(job_id, "routing", intent_type=intent_type, task_type=task_type)
            router_result = self._run_router_stage(job)
            intent_type = router_result.get("intent_type") or intent_type
            task_type = router_result.get("task_type") or task_type
            self._update_status(job_id, "routed", intent_type=intent_type, task_type=task_type)

            if intent_type == "question":
                self._update_status(job_id, "planning", intent_type=intent_type, task_type=task_type)
                result = self._run_answerer_stage(job, router_result)
                db.mark_completed(job_id, result)
                logger.info("question workflow completed", extra={"job_id": job_id, "status": "completed"})
                return

            if intent_type == "job":
                self._update_status(job_id, "planning", intent_type=intent_type, task_type=task_type)
                planner_result = self._run_planner_stage(job, router_result)
                task_type = planner_result.get("task_type") or task_type

                if planner_result.get("intent_type") == "needs_clarification":
                    self._mark_needs_clarification(job_id, planner_result, intent_type=intent_type, task_type=task_type)
                    logger.info(
                        "job requires clarification",
                        extra={"job_id": job_id, "status": "needs_clarification", "task_type": task_type},
                    )
                    return

                self._update_status(job_id, "executing", intent_type=intent_type, task_type=task_type)
                execution_result = self._execute_local_task(job, planner_result)

                execution_status = str(execution_result.get("execution_status") or "").lower()
                if execution_status == "needs_clarification":
                    clarification = {
                        "intent_type": "needs_clarification",
                        "task_type": task_type,
                        "question": self._build_execution_clarification_question(execution_result),
                        "missing_fields": (execution_result.get("details") or {}).get("missing_fields") or [],
                        "execution": execution_result,
                    }
                    self._mark_needs_clarification(job_id, clarification, intent_type=intent_type, task_type=task_type)
                    logger.info(
                        "execution requires clarification",
                        extra={"job_id": job_id, "status": "needs_clarification", "task_type": task_type},
                    )
                    return

                if execution_status == "failed":
                    details = execution_result.get("details") if isinstance(execution_result.get("details"), dict) else {}
                    db.mark_failed(
                        job_id,
                        {
                            "code": str(details.get("code") or "LOCAL_EXECUTION_FAILED"),
                            "message": str(details.get("message") or "Local execution failed."),
                            "details": execution_result,
                        },
                        status="failed",
                    )
                    self._update_status(job_id, "failed", intent_type=intent_type, task_type=task_type)
                    logger.warning("execution failed", extra={"job_id": job_id, "task_type": task_type})
                    return

                self._update_status(job_id, "verifying", intent_type=intent_type, task_type=task_type)
                verification_result = self._verify_local_task(job, execution_result)

                if not verification_result.get("verified") and execution_status == "success":
                    db.mark_failed(
                        job_id,
                        {
                            "code": "VERIFICATION_FAILED",
                            "message": "Execution completed but verification failed.",
                            "details": {
                                "execution": execution_result,
                                "verification": verification_result,
                            },
                        },
                        status="failed",
                    )
                    self._update_status(job_id, "failed", intent_type=intent_type, task_type=task_type)
                    logger.warning("verification failed", extra={"job_id": job_id, "task_type": task_type})
                    return

                final_result = self._run_final_phraser_stage(job, planner_result, execution_result, verification_result)
                db.mark_completed(
                    job_id,
                    final_result,
                    execution_json=execution_result,
                    stages_json=final_result.get("stages") if isinstance(final_result, dict) else None,
                )
                logger.info("job workflow completed", extra={"job_id": job_id, "status": "completed"})
                return

            if intent_type == "research":
                task_type = task_type or "root_topic_research"
                self._update_status(job_id, "planning", intent_type=intent_type, task_type=task_type)
                result = self._run_answerer_stage(
                    job,
                    {
                        "intent_type": intent_type,
                        "task_type": task_type,
                    },
                )
                db.mark_completed(job_id, result)
                logger.info(
                    "research workflow completed via answerer lane",
                    extra={"job_id": job_id, "status": "completed", "task_type": task_type},
                )
                return

            self._update_status(job_id, "failed", intent_type=intent_type, task_type=task_type)
            db.mark_failed(
                job_id,
                {
                    "code": "ROUTER_INVALID_INTENT",
                    "message": "Router stage returned an unsupported intent_type.",
                },
                status="failed",
            )

        except JobFailedError as exc:
            db.mark_failed(job_id, normalize_failure_payload(exc.payload), status="failed")
            logger.exception("job failed from pipeline", extra={"job_id": job_id, "status": "failed"})
        except JobTimedOutError as exc:
            db.mark_failed(job_id, {"code": "JOB_TIMEOUT", "message": str(exc)}, status="expired")
            logger.exception("job expired", extra={"job_id": job_id, "status": "expired"})
        except GitError as exc:
            self._recover_or_fail_git_error(job_id, exc)
        except Exception as exc:  # pragma: no cover
            db.mark_failed(job_id, {"code": "UNEXPECTED_ERROR", "message": str(exc)}, status="failed")
            logger.exception("job failed unexpectedly", extra={"job_id": job_id, "status": "failed"})
        finally:
            self._notify_event.set()

    def _process_job_single_run(
        self,
        job: dict[str, Any],
        intent_type: str | None = None,
        task_type: str | None = None,
    ) -> None:
        """
        Compatibility mode:
        submit a single request artifact and wait for the workflow's final result.
        """
        job_id = job["job_id"]
        self._update_status(job_id, "routing", intent_type=intent_type, task_type=task_type)

        with REPO_LOCK:
            sync_repo_to_remote_head()
            request_payload_raw = job.get("request_json") if isinstance(job.get("request_json"), dict) else {}
            request_payload = self._coerce_routing_metadata(
                request_payload_raw,
                default_intent_type=intent_type,
                default_task_type=task_type,
                requires_local_execution=True if str(intent_type or "").strip().lower() == "job" else None,
            )
            request_path = write_request_artifact(job_id, request_payload)
            commit_and_push_request(job_id, request_path)

        result = wait_for_result(job_id, timeout_seconds=settings.job_timeout_seconds)
        result = self._apply_runtime_handoff_if_configured(job_id=job_id, result=result)

        if isinstance(result, dict):
            state = str(result.get("state", result.get("status", ""))).strip().lower()
            if state == "needs_clarification" or result.get("needs_clarification") is True:
                self._mark_needs_clarification(
                    job_id,
                    result,
                    intent_type=result.get("intent_type") or intent_type,
                    task_type=result.get("task_type") or task_type,
                )
                logger.info(
                    "one-shot workflow requires clarification",
                    extra={"job_id": job_id, "status": "needs_clarification"},
                )
                return

        normalized = self._normalize_response_payload(
            result,
            router_result={"intent_type": intent_type, "task_type": task_type},
        )

        execution_json = normalized.get("execution") if isinstance(normalized.get("execution"), dict) else None
        stages_json = normalized.get("stages") if isinstance(normalized.get("stages"), dict) else None

        db.mark_completed(
            job_id,
            normalized,
            execution_json=execution_json,
            stages_json=stages_json,
        )
        logger.info(
            "one-shot workflow completed",
            extra={"job_id": job_id, "status": "completed", "intent_type": intent_type, "task_type": task_type},
        )

    def _extract_request_model(self, job: dict[str, Any]) -> str:
        request_json = job.get("request_json")
        if isinstance(request_json, dict):
            return str(request_json.get("model") or "").strip()
        return ""

    @staticmethod
    def _is_allsequential_model(model_name: str) -> bool:
        normalized = str(model_name or "").strip().lower()
        if not normalized:
            return False
        if normalized == "git-allsequential":
            return True
        return normalized.split("/")[-1] == "git-allsequential"

    @staticmethod
    def _is_allparallel_model(model_name: str) -> bool:
        normalized = str(model_name or "").strip().lower()
        if not normalized:
            return False
        if normalized == "git-parallel":
            return True
        return normalized.split("/")[-1] == "git-parallel"

    @staticmethod
    def _is_synthesis_model(model_name: str) -> bool:
        normalized = str(model_name or "").strip().lower()
        if not normalized:
            return False
        return normalized.split("/")[-1] in {"git-synth", "git-synthesis"}

    def _resolve_allsequential_targets(self) -> list[str]:
        targets: list[str] = []
        for model_name in settings.all_sequential_models():
            normalized = str(model_name or "").strip()
            if not normalized:
                continue
            if self._is_allsequential_model(normalized):
                continue
            if self._is_allparallel_model(normalized):
                continue
            if normalized not in targets:
                targets.append(normalized)
        return targets

    def _resolve_allparallel_targets(self) -> list[str]:
        targets: list[str] = []
        for model_name in settings.all_parallel_models():
            normalized = str(model_name or "").strip()
            if not normalized:
                continue
            if self._is_allsequential_model(normalized):
                continue
            if self._is_allparallel_model(normalized):
                continue
            if normalized not in targets:
                targets.append(normalized)
        return targets

    @staticmethod
    def _coerce_optional_bool(value: Any) -> bool | None:
        if value is None:
            return None
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
        return bool(value)

    def _coerce_routing_metadata(
        self,
        request_payload: dict[str, Any] | None,
        *,
        default_intent_type: str | None = None,
        default_task_type: str | None = None,
        requires_local_execution: bool | None = None,
        default_route_state: str = "received",
    ) -> dict[str, Any]:
        payload = json.loads(json.dumps(request_payload if isinstance(request_payload, dict) else {}))
        routing_raw = payload.get("routing_metadata")
        routing = dict(routing_raw) if isinstance(routing_raw, dict) else {}

        intent_type = str(routing.get("intent_type") or default_intent_type or "").strip()
        task_type = str(routing.get("task_type") or default_task_type or "").strip()
        route_state = str(routing.get("route_state") or default_route_state or "received").strip() or "received"

        if requires_local_execution is None:
            requires_local_execution = self._coerce_optional_bool(routing.get("requires_local_execution"))
        if requires_local_execution is None and intent_type:
            requires_local_execution = intent_type.lower() == "job"
        if requires_local_execution is None:
            requires_local_execution = False

        routing["schema_version"] = str(routing.get("schema_version") or "1.0")
        routing["route_state"] = route_state
        routing["requires_local_execution"] = bool(requires_local_execution)
        if intent_type:
            routing["intent_type"] = intent_type
        else:
            routing.pop("intent_type", None)
        if task_type:
            routing["task_type"] = task_type
        else:
            routing.pop("task_type", None)

        payload["routing_metadata"] = routing
        return payload

    def _build_allsequential_child_payload(
        self,
        parent_payload: dict[str, Any],
        *,
        target_model: str,
        parent_job_id: str,
        index: int,
        total: int,
    ) -> dict[str, Any]:
        parent_routing = parent_payload.get("routing_metadata") if isinstance(parent_payload, dict) else None
        parent_intent = str(parent_routing.get("intent_type") or "").strip() if isinstance(parent_routing, dict) else ""
        parent_task = str(parent_routing.get("task_type") or "").strip() if isinstance(parent_routing, dict) else ""

        child_payload = self._coerce_routing_metadata(
            parent_payload,
            default_intent_type=parent_intent or "question",
            default_task_type=parent_task or settings.default_question_task_type,
            requires_local_execution=False,
            default_route_state="received",
        )
        child_routing = child_payload.get("routing_metadata")
        if isinstance(child_routing, dict):
            child_routing["route_state"] = "received"
        child_payload["model"] = target_model
        child_payload["allsequential_parent_job_id"] = parent_job_id
        child_payload["allsequential_index"] = index
        child_payload["allsequential_total"] = total
        return child_payload

    def _build_allparallel_child_payload(
        self,
        parent_payload: dict[str, Any],
        *,
        target_model: str,
        parent_job_id: str,
        index: int,
        total: int,
    ) -> dict[str, Any]:
        parent_routing = parent_payload.get("routing_metadata") if isinstance(parent_payload, dict) else None
        parent_intent = str(parent_routing.get("intent_type") or "").strip() if isinstance(parent_routing, dict) else ""
        parent_task = str(parent_routing.get("task_type") or "").strip() if isinstance(parent_routing, dict) else ""

        child_payload = self._coerce_routing_metadata(
            parent_payload,
            default_intent_type=parent_intent or "question",
            default_task_type=parent_task or settings.default_question_task_type,
            requires_local_execution=False,
            default_route_state="received",
        )
        child_routing = child_payload.get("routing_metadata")
        if isinstance(child_routing, dict):
            child_routing["route_state"] = "received"
        child_payload["model"] = target_model
        child_payload["allparallel_parent_job_id"] = parent_job_id
        child_payload["allparallel_index"] = index
        child_payload["allparallel_total"] = total
        return child_payload

    @staticmethod
    def _sanitize_model_tail(model_name: str) -> str:
        tail = str(model_name or "").strip().split("/")[-1]
        return re.sub(r"[^a-zA-Z0-9_-]+", "-", tail)[:40] or "model"

    @staticmethod
    def _sanitize_branch_token(branch_name: str) -> str:
        token = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(branch_name or "").strip())
        token = token.strip(".-")
        return token[:80] or "branch"

    def _resolve_branch_for_model(self, model_name: str) -> str:
        tail = str(model_name or "").strip().split("/")[-1]
        if not tail:
            return settings.branch
        normalized = tail.lower()
        if normalized in {"git-allsequential", "git-parallel", "git-synth", "git-synthesis"}:
            return settings.branch
        return tail

    def _resolve_origin_url(self) -> str:
        if self._base_repo_origin_url:
            return self._base_repo_origin_url
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=settings.repo_path,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or "unknown git error"
            raise GitError(f"Unable to resolve origin URL from {settings.repo_path}: {detail}")
        url = str(result.stdout or "").strip()
        if not url:
            raise GitError(f"Origin URL is empty in {settings.repo_path}")
        self._base_repo_origin_url = url
        return url

    def _resolve_repo_context_for_model(self, model_name: str) -> tuple[Path, str, Any]:
        branch_name = self._resolve_branch_for_model(model_name)
        if branch_name == settings.branch:
            return settings.repo_path, settings.branch, REPO_LOCK

        branch_token = self._sanitize_branch_token(branch_name)
        repo_path = settings.repo_path.parent / f"{settings.repo_path.name}__{branch_token}"
        lock_path = settings.repo_lock_path.parent / f"{settings.repo_lock_path.name}.{branch_token}"
        repo_lock = REPO_LOCK if repo_path == settings.repo_path else type(REPO_LOCK)(lock_path)

        with self._workspace_setup_lock:
            if not (repo_path / ".git").is_dir():
                origin_url = self._resolve_origin_url()
                clone = subprocess.run(
                    ["git", "clone", "--branch", branch_name, "--single-branch", origin_url, str(repo_path)],
                    text=True,
                    capture_output=True,
                )
                if clone.returncode != 0:
                    detail = (clone.stderr or clone.stdout).strip() or "unknown git clone error"
                    logger.warning(
                        "falling back to default branch workspace",
                        extra={"model": model_name, "requested_branch": branch_name, "error": detail},
                    )
                    return settings.repo_path, settings.branch, REPO_LOCK

            subprocess.run(
                ["git", "config", "user.name", settings.git_author_name],
                cwd=repo_path,
                text=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", settings.git_author_email],
                cwd=repo_path,
                text=True,
                capture_output=True,
            )
            for rel in (
                settings.requests_dir,
                settings.responses_dir,
                settings.errors_dir,
                settings.combined_dir,
                settings.status_dir,
                settings.stages_dir,
                settings.execution_dir,
            ):
                (repo_path / rel).mkdir(parents=True, exist_ok=True)

        return repo_path, branch_name, repo_lock

    @staticmethod
    def _compact_for_telegram_chunking(text: str) -> str:
        """
        Keep each source response in one large paragraph block so Telegram newline
        chunk mode prefers splitting between sources, not within a source answer.
        """
        normalized = str(text or "").replace("\r\n", "\n")
        normalized = re.sub(r"\n\s*\n+", "\n", normalized)
        return normalized.strip()

    @staticmethod
    def _split_for_transport(text: str, max_chars: int = 3400) -> list[str]:
        """
        Pre-split long text so downstream channel chunkers receive stable blocks.
        This helps preserve source headers when a single source answer is lengthy.
        """
        raw = str(text or "").strip()
        if not raw:
            return [""]
        if len(raw) <= max_chars:
            return [raw]

        chunks: list[str] = []
        remaining = raw
        while remaining:
            if len(remaining) <= max_chars:
                chunks.append(remaining.strip())
                break

            window = remaining[:max_chars]
            split_idx = window.rfind("\n")
            if split_idx < int(max_chars * 0.6):
                split_idx = window.rfind(" ")
            if split_idx < int(max_chars * 0.6):
                split_idx = max_chars

            head = remaining[:split_idx].strip()
            if head:
                chunks.append(head)
            remaining = remaining[split_idx:].lstrip()

        return chunks or [raw]

    def _run_one_shot_request(
        self,
        job_id: str,
        request_payload: dict[str, Any],
        *,
        target_model: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        model_name = str(target_model or request_payload.get("model") or "").strip()
        repo_path, repo_branch, repo_lock = self._resolve_repo_context_for_model(model_name)

        with repo_lock:
            sync_repo_to_remote_head_for(repo_path, repo_branch)
            request_path = write_request_artifact_for(job_id, request_payload, repo_path)
            commit_and_push_request_for(job_id, request_path, repo_path, repo_branch)

        result = wait_for_result_for(
            job_id,
            timeout_seconds=int(timeout_seconds or settings.job_timeout_seconds),
            repo_path=repo_path,
            branch=repo_branch,
            repo_lock=repo_lock,
        )
        return self._apply_runtime_handoff_if_configured(job_id=job_id, result=result)

    @staticmethod
    def _extract_job_id_from_text(text: str) -> str:
        match = re.search(r"\b(job_[A-Za-z0-9_]+)\b", str(text or ""))
        if not match:
            return ""
        return str(match.group(1) or "").strip()

    @staticmethod
    def _extract_options_dict(request_payload: dict[str, Any]) -> dict[str, Any]:
        options = request_payload.get("options")
        return options if isinstance(options, dict) else {}

    def _fanout_auto_synthesis_enabled_for_request(self, request_payload: dict[str, Any]) -> bool:
        if not settings.fanout_auto_synthesis_enabled:
            return False
        if not self._allsequential_followup_delivery_enabled():
            return False

        options = self._extract_options_dict(request_payload)
        for key in ("auto_synthesis", "auto_synth", "fanout_auto_synthesis", "synthesize_after_fanout"):
            value = self._coerce_optional_bool(options.get(key))
            if value is not None:
                return bool(value)

        for key in ("auto_synthesis", "auto_synth", "fanout_auto_synthesis", "synthesize_after_fanout"):
            value = self._coerce_optional_bool(request_payload.get(key))
            if value is not None:
                return bool(value)

        return True

    @staticmethod
    def _build_synthesis_entries_from_fanout_results(
        *,
        source_job_id: str,
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        ordered = sorted(results, key=lambda item: int(item.get("index") or 0))
        entries: list[dict[str, Any]] = []
        for item in ordered:
            status = str(item.get("status") or "unknown").strip().lower() or "unknown"
            if status == "completed":
                content = str(item.get("content") or "").strip() or "(empty response)"
            else:
                err = str(item.get("error") or "unknown error").strip() or "unknown error"
                content = f"Error: {err}"
            entries.append(
                JobWorker._normalize_synthesis_source_entry(
                    {
                        "source": str(item.get("model") or "unknown").strip() or "unknown",
                        "status": status,
                        "index": int(item.get("index") or 0),
                        "content": content,
                        "source_job_id": source_job_id,
                    }
                )
            )
        return entries

    @staticmethod
    def _extract_job_ids_from_value(value: Any) -> list[str]:
        values: list[str] = []
        if isinstance(value, list):
            for item in value:
                text = str(item or "").strip()
                if text and text not in values:
                    values.append(text)
            return values
        text = str(value or "").strip()
        if not text:
            return values
        for token in re.split(r"[,\s]+", text):
            candidate = str(token or "").strip()
            if candidate and candidate not in values:
                values.append(candidate)
        return values

    def _extract_synthesis_source_job_ids(self, request_payload: dict[str, Any], base_prompt: str) -> list[str]:
        options = self._extract_options_dict(request_payload)
        ordered_ids: list[str] = []

        for key in ("source_job_ids", "synth_source_job_ids", "combined_job_ids"):
            for value in self._extract_job_ids_from_value(options.get(key)):
                if value not in ordered_ids:
                    ordered_ids.append(value)

        for key in ("source_job_id", "synth_source_job_id", "combined_job_id"):
            value = str(options.get(key) or "").strip()
            if value and value not in ordered_ids:
                ordered_ids.append(value)

        # Allow inline mentions like: "use job_abc and job_def"
        for match in re.finditer(r"\b(job_[A-Za-z0-9_]+)\b", str(base_prompt or "")):
            candidate = str(match.group(1) or "").strip()
            if candidate and candidate not in ordered_ids:
                ordered_ids.append(candidate)

        return ordered_ids

    @staticmethod
    def _is_fanout_combined_payload(payload: dict[str, Any]) -> bool:
        execution = payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
        mode = str(execution.get("mode") or "").strip().lower()
        if mode not in {"allparallel", "allparallel_virtual_turns", "allsequential", "allsequential_virtual_turns"}:
            return False
        source_messages = execution.get("source_messages")
        return isinstance(source_messages, list) and len(source_messages) > 0

    def _resolve_synthesis_source_combined_set(self, explicit_job_ids: list[str] | None = None) -> list[tuple[dict[str, Any], str]]:
        combined_dir = settings.repo_path / settings.combined_dir
        combined_dir.mkdir(parents=True, exist_ok=True)

        with REPO_LOCK:
            sync_repo_to_remote_head()

        resolved: list[tuple[dict[str, Any], str]] = []
        explicit = [str(job_id or "").strip() for job_id in (explicit_job_ids or []) if str(job_id or "").strip()]

        if explicit:
            for explicit_job_id in explicit:
                target = combined_dir / f"{explicit_job_id}.json"
                if not target.exists():
                    raise RuntimeError(f"Combined artifact not found for source job: {explicit_job_id}")
                payload = json.loads(target.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or not self._is_fanout_combined_payload(payload):
                    raise RuntimeError(f"Combined artifact for {explicit_job_id} is not a valid fanout payload.")
                resolved.append((payload, explicit_job_id))
            return resolved

        files = sorted(combined_dir.glob("job_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if not self._is_fanout_combined_payload(payload):
                continue
            source_job_id = str(payload.get("job_id") or path.stem).strip()
            if source_job_id:
                resolved.append((payload, source_job_id))
                break

        if resolved:
            return resolved

        raise RuntimeError("No valid fanout combined artifact was found on main branch.")

    @staticmethod
    def _has_last_chat_context_trigger(base_prompt: str) -> bool:
        text = str(base_prompt or "").strip()
        if not text:
            return False
        return bool(re.match(r"^(?:based on|using|from)\s+the\s+last\s+chat\b", text, flags=re.IGNORECASE))

    def _last_chat_context_requested(self, request_payload: dict[str, Any], base_prompt: str) -> bool:
        options = self._extract_options_dict(request_payload)
        for key in ("use_last_chat_context", "last_chat_context", "based_on_last_chat"):
            value = self._coerce_optional_bool(options.get(key))
            if value is not None:
                return bool(value)
        for key in ("use_last_chat_context", "last_chat_context", "based_on_last_chat"):
            value = self._coerce_optional_bool(request_payload.get(key))
            if value is not None:
                return bool(value)
        return self._has_last_chat_context_trigger(base_prompt)

    def _resolve_latest_auto_synthesis_content(self, *, skip_job_id: str = "") -> tuple[str, str] | None:
        combined_dir = settings.repo_path / settings.combined_dir
        if not combined_dir.exists():
            return None

        with REPO_LOCK:
            sync_repo_to_remote_head()

        files = sorted(combined_dir.glob("job_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            candidate_job_id = str(path.stem).strip()
            if skip_job_id and candidate_job_id == str(skip_job_id).strip():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict) or not self._is_fanout_combined_payload(payload):
                continue
            execution = payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
            auto = execution.get("auto_synthesis") if isinstance(execution.get("auto_synthesis"), dict) else {}
            status = str(auto.get("status") or "").strip().lower()
            if status != "completed":
                continue
            content = str(auto.get("content") or "").strip()
            if not content:
                continue
            if self._detect_unusable_synthesis_content(content):
                continue
            source_job_id = str(payload.get("job_id") or candidate_job_id).strip() or candidate_job_id
            return source_job_id, content

        return None

    @staticmethod
    def _build_last_chat_followup_prompt(base_prompt: str, *, source_job_id: str, synthesis_content: str) -> str:
        prompt = str(base_prompt or "").strip()
        context = str(synthesis_content or "").strip()
        if len(context) > 5000:
            context = context[:5000].rstrip() + "..."
        return (
            f"Previous synthesized context from {source_job_id}:\n"
            f"{context}\n\n"
            f"Follow-up request:\n{prompt}"
        ).strip()

    @staticmethod
    def _replace_last_user_message(messages: list[Any], new_content: str) -> list[Any]:
        replaced = list(messages)
        last_user_index = -1
        for idx, message in enumerate(replaced):
            if isinstance(message, dict) and str(message.get("role") or "").strip().lower() == "user":
                last_user_index = idx
        if last_user_index >= 0 and isinstance(replaced[last_user_index], dict):
            updated = dict(replaced[last_user_index])
            updated["content"] = new_content
            replaced[last_user_index] = updated
            return replaced
        replaced.append({"role": "user", "content": new_content})
        return replaced

    def _maybe_apply_last_chat_context_for_parallel(
        self,
        *,
        job_id: str,
        request_payload: dict[str, Any],
        base_prompt: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if not self._last_chat_context_requested(request_payload, base_prompt):
            return request_payload, None

        resolved = self._resolve_latest_auto_synthesis_content(skip_job_id=job_id)
        if not resolved:
            return request_payload, {
                "requested": True,
                "applied": False,
                "reason": "no_completed_auto_synthesis_found",
            }

        source_job_id, synthesis_content = resolved
        effective_prompt = self._build_last_chat_followup_prompt(
            base_prompt,
            source_job_id=source_job_id,
            synthesis_content=synthesis_content,
        )

        enriched_payload = json.loads(json.dumps(request_payload))
        enriched_payload["user_prompt"] = effective_prompt
        if "prompt" in enriched_payload:
            enriched_payload["prompt"] = effective_prompt
        messages = enriched_payload.get("messages")
        if isinstance(messages, list):
            enriched_payload["messages"] = self._replace_last_user_message(messages, effective_prompt)

        context_meta = {
            "requested": True,
            "applied": True,
            "source_job_id": source_job_id,
            "trigger": "keyword",
        }
        enriched_payload["followup_context"] = context_meta
        return enriched_payload, context_meta

    @staticmethod
    def _coerce_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return default

    def _merge_source_messages(self, source_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for item in source_messages:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "unknown").strip() or "unknown"
            entry = grouped.setdefault(
                source,
                {
                    "source": source,
                    "status": str(item.get("status") or "unknown").strip() or "unknown",
                    "index": self._coerce_int(item.get("index"), 999),
                    "parts": [],
                },
            )
            part_index = self._coerce_int(item.get("part_index"), 1)
            part_content = str(item.get("content") or "").strip()
            entry["parts"].append((part_index, part_content))
            status = str(item.get("status") or "").strip()
            if status:
                entry["status"] = status
            entry["index"] = min(entry.get("index", 999), self._coerce_int(item.get("index"), 999))

        merged: list[dict[str, Any]] = []
        for _, data in sorted(grouped.items(), key=lambda kv: (kv[1].get("index", 999), kv[0])):
            parts = sorted(data.get("parts") or [], key=lambda p: p[0])
            content = "\n".join(part for _, part in parts if str(part).strip()).strip()
            merged.append(
                {
                    "source": data.get("source"),
                    "status": data.get("status"),
                    "index": data.get("index"),
                    "content": content,
                }
            )
        return merged

    @staticmethod
    def _source_weight(source_name: str, *, prefer_best_sources: bool) -> float:
        if not prefer_best_sources:
            return 1.0
        weights = {
            "git-qwen": 1.35,
            "git-grok": 1.35,
            "git-chatgpt": 1.00,
            "git-perplexity": 0.95,
            "git-inceptionlabs": 1.10,
        }
        source = str(source_name or "").strip().lower()
        return float(weights.get(source, 1.0))

    def _collect_synthesis_source_entries(self, payloads_with_ids: list[tuple[dict[str, Any], str]]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for payload, source_job_id in payloads_with_ids:
            execution = payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
            source_messages = execution.get("source_messages") if isinstance(execution.get("source_messages"), list) else []
            merged = self._merge_source_messages(source_messages)
            for item in merged:
                row = dict(item)
                row["source_job_id"] = source_job_id
                entries.append(self._normalize_synthesis_source_entry(row))
        return entries

    @staticmethod
    def _detect_unusable_synthesis_source_content(content: str) -> str | None:
        text = str(content or "").strip()
        if not text:
            return "empty"

        lower = text.lower()
        if lower.startswith("live_web_unavailable"):
            return "live_web_unavailable"

        # Guard against policy/prompt echo content that is not an answer.
        markers = (
            "you are juniper",
            "execution constraints:",
            "current request:",
            "if and only if live web lookup is truly unavailable",
            "reply exactly: live_web_unavailable",
        )
        if any(marker in lower for marker in markers):
            return "policy_echo"
        return None

    @staticmethod
    def _normalize_synthesis_source_entry(entry: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(entry)
        normalized["source"] = str(normalized.get("source") or "unknown").strip() or "unknown"
        normalized["status"] = str(normalized.get("status") or "unknown").strip().lower() or "unknown"
        normalized["content"] = str(normalized.get("content") or "").strip()

        if normalized["status"] == "completed":
            issue = JobWorker._detect_unusable_synthesis_source_content(str(normalized.get("content") or ""))
            if issue:
                normalized["status"] = "failed"
                normalized["error"] = f"unusable source content ({issue})"
                normalized["content"] = f"Error: {normalized['error']}"
                return normalized

        if not str(normalized.get("content") or "").strip():
            normalized["content"] = "Error: unknown source failure"
            if normalized["status"] == "completed":
                normalized["status"] = "failed"
                normalized["error"] = "empty source content"

        return normalized

    @staticmethod
    def _pick_first(patterns: list[str], text: str) -> float | None:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            try:
                return float(match.group(1))
            except Exception:
                continue
        return None

    @staticmethod
    def _pick_first_range_avg(patterns: list[str], text: str) -> float | None:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            try:
                left = float(match.group(1))
                right = float(match.group(2))
                return (left + right) / 2.0
            except Exception:
                continue
        return None

    @staticmethod
    def _extract_current_temperature_f(text: str) -> float | None:
        value = JobWorker._pick_first_range_avg(
            [
                r"current[^.\n]{0,90}?(-?\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(-?\d+(?:\.\d+)?)\s*°?\s*f\b",
                r"temperature[^.\n]{0,90}?(-?\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(-?\d+(?:\.\d+)?)\s*°?\s*f\b",
            ],
            text,
        )
        if value is None:
            value = JobWorker._pick_first_range_avg(
                [
                    r"\b(-?\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(-?\d+(?:\.\d+)?)\s*°?\s*f\b",
                ],
                text,
            )
        if value is None:
            value = JobWorker._pick_first(
                [
                    r"current[^.\n]{0,90}?(-?\d+(?:\.\d+)?)\s*°?\s*f\b",
                    r"temperature[^.\n]{0,90}?(-?\d+(?:\.\d+)?)\s*°?\s*f\b",
                    r"\b(-?\d+(?:\.\d+)?)\s*°?\s*f\b",
                ],
                text,
            )
        if value is not None and -40.0 <= value <= 130.0:
            return value

        c_value = JobWorker._pick_first_range_avg(
            [
                r"current[^.\n]{0,90}?(-?\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(-?\d+(?:\.\d+)?)\s*°?\s*c\b",
                r"temperature[^.\n]{0,90}?(-?\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(-?\d+(?:\.\d+)?)\s*°?\s*c\b",
            ],
            text,
        )
        if c_value is None:
            c_value = JobWorker._pick_first(
                [
                    r"current[^.\n]{0,90}?(-?\d+(?:\.\d+)?)\s*°?\s*c\b",
                    r"temperature[^.\n]{0,90}?(-?\d+(?:\.\d+)?)\s*°?\s*c\b",
                    r"\b(-?\d+(?:\.\d+)?)\s*°?\s*c\b",
                ],
                text,
            )
        if c_value is not None and -40.0 <= c_value <= 55.0:
            return (c_value * 9.0 / 5.0) + 32.0
        return None

    @staticmethod
    def _extract_current_wind_mph(text: str) -> float | None:
        value = JobWorker._pick_first_range_avg(
            [
                r"wind[^.\n]{0,90}?(-?\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(-?\d+(?:\.\d+)?)\s*mph\b",
                r"\b(-?\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(-?\d+(?:\.\d+)?)\s*mph\b",
            ],
            text,
        )
        if value is None:
            value = JobWorker._pick_first(
                [
                    r"wind[^.\n]{0,90}?(-?\d+(?:\.\d+)?)\s*mph\b",
                    r"\b(-?\d+(?:\.\d+)?)\s*mph\b",
                ],
                text,
            )
        if value is None:
            return None
        if value < 0.0 or value > 200.0:
            return None
        return value

    @staticmethod
    def _extract_max_precip_chance_pct(text: str) -> float | None:
        values: list[float] = []
        for match in re.finditer(r"(\d{1,3}(?:\.\d+)?)\s*%", text):
            start = max(0, match.start() - 20)
            end = min(len(text), match.end() + 20)
            window = text[start:end].lower()
            if not any(token in window for token in ("rain", "storm", "precip", "chance", "shower")):
                continue
            try:
                value = float(match.group(1))
            except Exception:
                continue
            if 0.0 <= value <= 100.0:
                values.append(value)
        if not values:
            return None
        return max(values)

    @staticmethod
    def _classify_condition(text: str) -> str:
        lower = str(text or "").lower()
        if any(token in lower for token in ("thunderstorm", "storms", "storm")):
            return "stormy"
        if any(token in lower for token in ("showers", "rain", "drizzle")):
            return "rainy"
        if any(token in lower for token in ("overcast", "mostly cloudy", "cloudy")):
            return "cloudy"
        if any(token in lower for token in ("partly cloudy", "partly sunny", "mainly clear")):
            return "partly cloudy"
        if any(token in lower for token in ("sunny", "clear sky", "clear")):
            return "sunny"
        return "unknown"

    @staticmethod
    def _classify_rain_risk(text: str, precip_pct: float | None) -> str:
        lower = str(text or "").lower()
        if precip_pct is not None:
            if precip_pct > 60.0:
                return "high"
            if precip_pct > 30.0:
                return "moderate"
            return "low"
        if any(token in lower for token in ("severe", "thunderstorm", "heavy rain", "elevated", "high risk")):
            return "high"
        if any(token in lower for token in ("showers", "rain possible", "moderate")):
            return "moderate"
        if any(token in lower for token in ("low rain", "low risk", "low chance", "no rain", "dry")):
            return "low"
        return "unknown"

    def _build_synthesis_aggregate(
        self,
        source_entries: list[dict[str, Any]],
        *,
        prefer_best_sources: bool,
    ) -> dict[str, Any]:
        completed_entries = [entry for entry in source_entries if str(entry.get("status") or "").lower() == "completed"]
        failed_entries = [entry for entry in source_entries if str(entry.get("status") or "").lower() != "completed"]

        temp_values: list[tuple[float, float]] = []
        wind_values: list[tuple[float, float]] = []
        condition_scores: dict[str, float] = {}
        rain_scores: dict[str, float] = {}
        per_source: list[dict[str, Any]] = []
        max_precip_pct: float | None = None

        for entry in completed_entries:
            source = str(entry.get("source") or "unknown")
            content = str(entry.get("content") or "")
            weight = self._source_weight(source, prefer_best_sources=prefer_best_sources)

            temp_f = self._extract_current_temperature_f(content)
            wind_mph = self._extract_current_wind_mph(content)
            precip_pct = self._extract_max_precip_chance_pct(content)
            condition = self._classify_condition(content)
            rain_risk = self._classify_rain_risk(content, precip_pct)

            if temp_f is not None:
                temp_values.append((temp_f, weight))
            if wind_mph is not None:
                wind_values.append((wind_mph, weight))
            condition_scores[condition] = condition_scores.get(condition, 0.0) + weight
            rain_scores[rain_risk] = rain_scores.get(rain_risk, 0.0) + weight
            if precip_pct is not None:
                max_precip_pct = precip_pct if max_precip_pct is None else max(max_precip_pct, precip_pct)

            per_source.append(
                {
                    "source": source,
                    "source_job_id": str(entry.get("source_job_id") or ""),
                    "weight": round(weight, 3),
                    "temperature_f": round(temp_f, 1) if temp_f is not None else None,
                    "temperature_c": round((temp_f - 32.0) * 5.0 / 9.0, 1) if temp_f is not None else None,
                    "wind_mph": round(wind_mph, 1) if wind_mph is not None else None,
                    "precip_chance_pct": round(precip_pct, 1) if precip_pct is not None else None,
                    "condition": condition,
                    "rain_risk": rain_risk,
                }
            )

        def weighted_average(values: list[tuple[float, float]]) -> float | None:
            if not values:
                return None
            total_weight = sum(weight for _, weight in values)
            if total_weight <= 0:
                return None
            return sum(value * weight for value, weight in values) / total_weight

        avg_temp_f = weighted_average(temp_values)
        avg_wind_mph = weighted_average(wind_values)
        top_condition = max(condition_scores.items(), key=lambda item: item[1])[0] if condition_scores else "unknown"
        top_rain_risk = max(rain_scores.items(), key=lambda item: item[1])[0] if rain_scores else "unknown"

        return {
            "completed_sources": len(completed_entries),
            "failed_sources": [
                {
                    "source": str(item.get("source") or "unknown"),
                    "source_job_id": str(item.get("source_job_id") or ""),
                }
                for item in failed_entries
            ],
            "temperature": {
                "weighted_avg_f": round(avg_temp_f, 1) if avg_temp_f is not None else None,
                "weighted_avg_c": round((avg_temp_f - 32.0) * 5.0 / 9.0, 1) if avg_temp_f is not None else None,
                "min_f": round(min((value for value, _ in temp_values)), 1) if temp_values else None,
                "max_f": round(max((value for value, _ in temp_values)), 1) if temp_values else None,
            },
            "wind": {
                "weighted_avg_mph": round(avg_wind_mph, 1) if avg_wind_mph is not None else None,
                "min_mph": round(min((value for value, _ in wind_values)), 1) if wind_values else None,
                "max_mph": round(max((value for value, _ in wind_values)), 1) if wind_values else None,
            },
            "rain": {
                "consensus_risk": top_rain_risk,
                "max_precip_chance_pct": round(max_precip_pct, 1) if max_precip_pct is not None else None,
            },
            "conditions": {
                "consensus": top_condition,
                "weighted_scores": {k: round(v, 3) for k, v in condition_scores.items()},
            },
            "prefer_best_sources": bool(prefer_best_sources),
            "per_source": per_source,
        }

    def _build_synthesis_prompt(
        self,
        *,
        base_prompt: str,
        source_job_ids: list[str],
        synthesis_mode: str,
        aggregate: dict[str, Any],
        source_entries: list[dict[str, Any]],
        chunk_label: str = "",
    ) -> tuple[str, str]:
        system_prompt = (
            "You synthesize one final response from multi-source model outputs. "
            "Use only the provided aggregate metrics and source excerpts. "
            "Do not invent external facts. "
            "If data conflicts, state the range and uncertainty briefly. "
            "This is strictly a question-answer synthesis task. "
            "Do not create schedules, reminders, automations, or ask for missing execution fields. "
            "Write one concise final answer text."
        )

        excerpts = []
        for entry in source_entries:
            excerpts.append(
                {
                    "source": entry.get("source"),
                    "source_job_id": entry.get("source_job_id"),
                    "status": entry.get("status"),
                    "excerpt": str(entry.get("content") or "")[:1200],
                }
            )

        instruction = str(base_prompt or "").strip() or "Summarize the fanout data into one concise response."
        user_payload = {
            "instruction": instruction,
            "synthesis_mode": synthesis_mode,
            "source_job_ids": source_job_ids,
            "aggregate": aggregate,
            "source_excerpts": excerpts,
        }
        if chunk_label:
            user_payload["chunk"] = chunk_label
        user_prompt = (
            "Tell me one final response using the instruction and structured synthesis input below.\n"
            "This is a question-only summarization request. Do not schedule tasks or request additional fields.\n"
            "Prefer aggregate numbers for averages where available. Mention uncertainty when sources disagree.\n\n"
            + json.dumps(user_payload, indent=2, ensure_ascii=False)
        )
        return system_prompt, user_prompt

    @staticmethod
    def _count_words(text: str) -> int:
        return len(re.findall(r"\S+", str(text or "")))

    def _chunk_synthesis_entries(
        self,
        source_entries: list[dict[str, Any]],
        *,
        max_words_per_chunk: int = 2200,
    ) -> list[list[dict[str, Any]]]:
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_words = 0
        for entry in source_entries:
            entry_words = self._count_words(entry.get("content") or "")
            if current and (current_words + entry_words) > max_words_per_chunk:
                chunks.append(current)
                current = []
                current_words = 0
            current.append(entry)
            current_words += entry_words
        if current:
            chunks.append(current)
        return chunks or [[]]

    def _run_synthesis_child_chatgpt(
        self,
        *,
        job_id: str,
        parent_job_id: str,
        system_prompt: str,
        user_prompt: str,
        combined_in_message: bool,
        timeout_seconds: int,
    ) -> str:
        child_payload = self._coerce_routing_metadata(
            {
                "request_type": "chat",
                "model": "git-chatgpt",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "user_prompt": user_prompt,
                "combined_in_message": bool(combined_in_message),
            },
            default_intent_type="question",
            default_task_type="synthesized_summary",
            requires_local_execution=False,
            default_route_state="received",
        )
        raw_result = self._run_one_shot_request(
            job_id,
            child_payload,
            target_model="git-chatgpt",
            timeout_seconds=timeout_seconds,
        )
        if isinstance(raw_result, dict):
            child_state = str(raw_result.get("state") or raw_result.get("status") or "").strip().lower()
            if child_state in {"needs_clarification", "failed", "expired"} or raw_result.get("needs_clarification") is True:
                raise RuntimeError(f"git-chatgpt synthesis child returned non-answer state: {child_state or 'unknown'}")
            if child_state and raw_result.get("done") is False:
                raise RuntimeError(f"git-chatgpt synthesis child is still in-progress state: {child_state}")
        normalized_result = self._normalize_response_payload(
            raw_result,
            router_result={"intent_type": "question", "task_type": "synthesized_summary"},
        )
        content = self._extract_assistant_text(normalized_result) or self._extract_assistant_text(raw_result)
        if not content:
            raise RuntimeError("git-chatgpt synthesis child returned empty content")
        content_issue = self._detect_unusable_synthesis_content(content)
        if content_issue:
            raise RuntimeError(f"git-chatgpt synthesis child returned unusable content: {content_issue}")
        return content

    @staticmethod
    def _detect_unusable_synthesis_content(content: str) -> str | None:
        text = str(content or "").strip()
        if not text:
            return "empty"
        lower = text.lower()

        if "i still need these fields to continue" in lower:
            return "clarification_fields"
        if lower.startswith("live_web_unavailable"):
            return "live_web_unavailable"

        markers = (
            "relevant memory:",
            "\"source\": \"git-",
            "\"source_excerpts\"",
            "\"chunk_summaries\"",
            "\"instruction\":",
        )
        for marker in markers:
            if marker in lower:
                return f"debug_or_payload_leak:{marker}"

        if lower.count("{") >= 6 and "\"source\"" in lower:
            return "structured_payload_blob"
        return None

    @staticmethod
    def _build_local_synthesis_fallback(
        *,
        instruction: str,
        aggregate: dict[str, Any],
        child_error: str,
        synthesis_mode: str,
        source_entries: list[dict[str, Any]],
    ) -> str:
        temp = aggregate.get("temperature") if isinstance(aggregate.get("temperature"), dict) else {}
        wind = aggregate.get("wind") if isinstance(aggregate.get("wind"), dict) else {}
        rain = aggregate.get("rain") if isinstance(aggregate.get("rain"), dict) else {}
        cond = aggregate.get("conditions") if isinstance(aggregate.get("conditions"), dict) else {}

        lines: list[str] = []
        task = str(instruction or "").strip()
        if task:
            lines.append(f"Synthesis summary: {task}")

        if synthesis_mode == "weather":
            avg_f = temp.get("weighted_avg_f")
            avg_c = temp.get("weighted_avg_c")
            min_f = temp.get("min_f")
            max_f = temp.get("max_f")
            if avg_f is not None and avg_c is not None:
                temp_line = f"Weighted average temperature: {avg_f}F ({avg_c}C)"
                if min_f is not None and max_f is not None:
                    temp_line += f", source range {min_f}F to {max_f}F."
                else:
                    temp_line += "."
                lines.append(temp_line)

            wind_avg = wind.get("weighted_avg_mph")
            if wind_avg is not None:
                wind_line = f"Weighted average wind: {wind_avg} mph"
                if wind.get("min_mph") is not None and wind.get("max_mph") is not None:
                    wind_line += f", source range {wind.get('min_mph')} to {wind.get('max_mph')} mph."
                else:
                    wind_line += "."
                lines.append(wind_line)

            condition = str(cond.get("consensus") or "unknown")
            rain_risk = str(rain.get("consensus_risk") or "unknown")
            precip_max = rain.get("max_precip_chance_pct")
            lines.append(f"Condition consensus: {condition}. Rain risk consensus: {rain_risk}.")
            if precip_max is not None:
                lines.append(f"Highest precipitation chance mentioned by sources: {precip_max}%.")
        else:
            completed = [entry for entry in source_entries if str(entry.get("status") or "").lower() == "completed"]
            if completed:
                lines.append("Cross-source synthesis (deterministic fallback):")
                for entry in completed[:5]:
                    source = str(entry.get("source") or "unknown")
                    excerpt = re.sub(r"\s+", " ", str(entry.get("content") or "")).strip()
                    excerpt = excerpt[:220] + ("..." if len(excerpt) > 220 else "")
                    lines.append(f"{source}: {excerpt}")

        completed_sources = aggregate.get("completed_sources")
        failed_sources = aggregate.get("failed_sources")
        if isinstance(failed_sources, list):
            failed_labels = []
            for item in failed_sources:
                if isinstance(item, dict):
                    failed_labels.append(str(item.get("source") or "unknown"))
                else:
                    failed_labels.append(str(item))
        else:
            failed_labels = []
        lines.append(f"Sources completed: {completed_sources}. Sources failed: {', '.join(failed_labels) or 'none'}.")
        lines.append(f"Note: fallback synthesis used because git-chatgpt synthesis request failed ({child_error}).")
        return " ".join(lines).strip()

    def _run_synthesis_from_entries(
        self,
        *,
        synthesis_job_id: str,
        base_prompt: str,
        source_entries: list[dict[str, Any]],
        source_job_ids: list[str],
        source_modes: list[str],
        requested_source_job_ids: list[str] | None,
        combined_in_message: bool,
        send_followups: bool,
    ) -> tuple[str, dict[str, Any]]:
        if not source_entries:
            raise RuntimeError("Synthesis source data is empty after merging source messages.")

        synthesis_mode = "weather" if self._is_weather_prompt(base_prompt) else "general"
        prefer_best_sources = synthesis_mode == "weather"
        aggregate = self._build_synthesis_aggregate(
            source_entries,
            prefer_best_sources=prefer_best_sources,
        )
        source_chunks = self._chunk_synthesis_entries(source_entries, max_words_per_chunk=2200)

        fallback_reason = ""
        child_job_ids: list[str] = []
        map_reduce_used = len(source_chunks) > 1
        synth_timeout_cap = max(60, int(settings.synthesis_child_timeout_seconds))
        synth_timeout_seconds = max(60, min(int(settings.job_timeout_seconds), synth_timeout_cap))
        try:
            if not map_reduce_used:
                system_prompt, user_prompt = self._build_synthesis_prompt(
                    base_prompt=base_prompt,
                    source_job_ids=source_job_ids,
                    synthesis_mode=synthesis_mode,
                    aggregate=aggregate,
                    source_entries=source_chunks[0],
                )
                child_job_id = f"{synthesis_job_id}_synth_01_git-chatgpt"
                child_job_ids.append(child_job_id)
                content = self._run_synthesis_child_chatgpt(
                    job_id=child_job_id,
                    parent_job_id=synthesis_job_id,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    combined_in_message=combined_in_message,
                    timeout_seconds=synth_timeout_seconds,
                )
            else:
                chunk_summaries: list[dict[str, Any]] = []
                total_chunks = len(source_chunks)
                for idx, chunk in enumerate(source_chunks, start=1):
                    system_prompt, user_prompt = self._build_synthesis_prompt(
                        base_prompt=base_prompt,
                        source_job_ids=source_job_ids,
                        synthesis_mode=synthesis_mode,
                        aggregate=aggregate,
                        source_entries=chunk,
                        chunk_label=f"{idx}/{total_chunks}",
                    )
                    child_job_id = f"{synthesis_job_id}_synth_chunk_{idx:02d}_git-chatgpt"
                    child_job_ids.append(child_job_id)
                    chunk_text = self._run_synthesis_child_chatgpt(
                        job_id=child_job_id,
                        parent_job_id=synthesis_job_id,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        combined_in_message=combined_in_message,
                        timeout_seconds=synth_timeout_seconds,
                    )
                    chunk_summaries.append({"chunk_index": idx, "summary": chunk_text})

                final_system_prompt = (
                    "You synthesize one final response from chunk summaries. "
                    "Use only provided summaries and aggregate metrics. "
                    "Do not add new facts. "
                    "This is strictly a question-answer synthesis task. "
                    "Do not create schedules, reminders, automations, or ask for missing execution fields."
                )
                final_user_payload = {
                    "instruction": str(base_prompt or "").strip()
                    or "Synthesize one final response from chunk summaries.",
                    "synthesis_mode": synthesis_mode,
                    "source_job_ids": source_job_ids,
                    "aggregate": aggregate,
                    "chunk_summaries": chunk_summaries,
                }
                final_user_prompt = (
                    "Tell me one coherent final response using these chunk summaries.\n"
                    "This is a question-only summarization request. Do not schedule tasks or request additional fields.\n\n"
                    + json.dumps(final_user_payload, indent=2, ensure_ascii=False)
                )
                final_child_job_id = f"{synthesis_job_id}_synth_final_git-chatgpt"
                child_job_ids.append(final_child_job_id)
                content = self._run_synthesis_child_chatgpt(
                    job_id=final_child_job_id,
                    parent_job_id=synthesis_job_id,
                    system_prompt=final_system_prompt,
                    user_prompt=final_user_prompt,
                    combined_in_message=combined_in_message,
                    timeout_seconds=synth_timeout_seconds,
                )
        except Exception as child_exc:
            fallback_reason = str(child_exc)
            content = self._build_local_synthesis_fallback(
                instruction=base_prompt,
                aggregate=aggregate,
                child_error=fallback_reason,
                synthesis_mode=synthesis_mode,
                source_entries=source_entries,
            )

        followup_enabled = bool(send_followups and self._allsequential_followup_delivery_enabled())
        delivery_errors: list[dict[str, Any]] = []
        if followup_enabled:
            for followup in self._build_synthesis_followup_messages(content):
                ok, err = self._send_openclaw_channel_message(str(followup.get("text") or ""))
                if not ok:
                    delivery_errors.append(
                        {
                            "part_index": followup.get("part_index"),
                            "part_total": followup.get("part_total"),
                            "error": err or "unknown delivery error",
                        }
                    )
            if delivery_errors:
                logger.warning(
                    "synthesis follow-up delivery failed",
                    extra={"job_id": synthesis_job_id, "delivery_failures": len(delivery_errors)},
                )

        execution_meta = {
            "mode": "synthesis",
            "source_job_id": source_job_ids[0] if source_job_ids else None,
            "source_job_ids": source_job_ids,
            "child_job_ids": child_job_ids,
            "target_model": "git-chatgpt",
            "aggregate": aggregate,
            "source_count": len(source_entries),
            "requested_source_job_ids": requested_source_job_ids or None,
            "source_mode": source_modes[0] if source_modes else None,
            "source_modes": source_modes,
            "synthesis_mode": synthesis_mode,
            "map_reduce_used": map_reduce_used,
            "chunk_count": len(source_chunks),
            "fallback_used": bool(fallback_reason),
            "fallback_reason": fallback_reason or None,
            "followup_delivery_enabled": followup_enabled,
            "delivery_errors": delivery_errors,
        }
        return content, execution_meta

    def _maybe_run_fanout_auto_synthesis(
        self,
        *,
        parent_job_id: str,
        fanout_mode: str,
        request_payload: dict[str, Any],
        base_prompt: str,
        fanout_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not self._fanout_auto_synthesis_enabled_for_request(request_payload):
            return {"enabled": False, "status": "skipped", "reason": "disabled"}

        source_entries = self._build_synthesis_entries_from_fanout_results(
            source_job_id=parent_job_id,
            results=fanout_results,
        )
        completed_count = sum(1 for item in source_entries if str(item.get("status") or "").lower() == "completed")
        if completed_count <= 0:
            return {"enabled": True, "status": "skipped", "reason": "no_completed_sources"}

        synth_instruction = str(base_prompt or "").strip() or "Synthesize one final response from fanout sources."
        combined_in_message = bool(request_payload.get("combined_in_message", False))
        try:
            content, synth_execution = self._run_synthesis_from_entries(
                synthesis_job_id=f"{parent_job_id}_autosynth",
                base_prompt=synth_instruction,
                source_entries=source_entries,
                source_job_ids=[parent_job_id],
                source_modes=[fanout_mode],
                requested_source_job_ids=[parent_job_id],
                combined_in_message=combined_in_message,
                send_followups=True,
            )
            return {
                "enabled": True,
                "status": "completed",
                "content": content,
                "execution": synth_execution,
            }
        except Exception as exc:
            logger.warning(
                "fanout auto synthesis failed",
                extra={"job_id": parent_job_id, "fanout_mode": fanout_mode, "error": str(exc)},
            )
            return {
                "enabled": True,
                "status": "failed",
                "error": str(exc),
            }

    def _process_job_synthesis(
        self,
        job: dict[str, Any],
        intent_type: str | None = None,
        task_type: str | None = None,
    ) -> None:
        job_id = job["job_id"]
        request_payload = job.get("request_json") if isinstance(job.get("request_json"), dict) else {}
        requested_intent = str(intent_type or "").strip().lower()
        requested_task = str(task_type or "").strip().lower()

        if requested_intent == "job" or requested_task in {"scheduled_weather_report", "reminder"}:
            self._update_status(job_id, "failed", intent_type="job", task_type="synthesized_summary")
            db.mark_failed(
                job_id,
                {
                    "code": "SYNTH_UNSUPPORTED_TASK",
                    "message": "git-synth is for question-style prompts and is not enabled for job execution.",
                },
                status="failed",
            )
            return

        self._update_status(job_id, "routing", intent_type="question", task_type="synthesized_summary")
        base_prompt = str(request_payload.get("user_prompt") or request_payload.get("prompt") or "").strip()
        source_job_id_hints = self._extract_synthesis_source_job_ids(request_payload, base_prompt)

        try:
            payloads_with_ids = self._resolve_synthesis_source_combined_set(source_job_id_hints)
            source_job_ids = [source_job_id for _, source_job_id in payloads_with_ids]
            source_modes: list[str] = []
            for payload, _ in payloads_with_ids:
                execution = payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
                mode = str(execution.get("mode") or "").strip()
                if mode:
                    source_modes.append(mode)

            merged_sources = self._collect_synthesis_source_entries(payloads_with_ids)
            if not merged_sources:
                raise RuntimeError("Synthesis source data is empty after merging source messages.")

            self._update_status(job_id, "executing", intent_type="question", task_type="synthesized_summary")
            combined_in_message = bool(request_payload.get("combined_in_message", False))
            content, execution_meta = self._run_synthesis_from_entries(
                synthesis_job_id=job_id,
                base_prompt=base_prompt,
                source_entries=merged_sources,
                source_job_ids=source_job_ids,
                source_modes=source_modes,
                requested_source_job_ids=source_job_id_hints,
                combined_in_message=combined_in_message,
                send_followups=True,
            )
            final_payload = {
                "message": {"role": "assistant", "content": content},
                "intent_type": "question",
                "task_type": "synthesized_summary",
                "current_stage": "completed",
                "execution": execution_meta,
                "stages": {"synthesis": "complete"},
                "done": True,
            }
            db.mark_completed(job_id, final_payload, execution_json=execution_meta, stages_json={"synthesis": "complete"})
            logger.info(
                "synthesis workflow completed",
                extra={"job_id": job_id, "status": "completed", "source_job_ids": source_job_ids},
            )
        except Exception as exc:
            self._update_status(job_id, "failed", intent_type="question", task_type="synthesized_summary")
            db.mark_failed(
                job_id,
                {"code": "SYNTH_EXECUTION_FAILED", "message": str(exc)},
                status="failed",
            )
            logger.warning("synthesis workflow failed", extra={"job_id": job_id, "error": str(exc)})

    def _execute_fanout_child(
        self,
        *,
        parent_job_id: str,
        child_job_id: str,
        target_model: str,
        index: int,
        child_payload: dict[str, Any],
        fanout_kind: str,
    ) -> dict[str, Any]:
        child_routing = child_payload.get("routing_metadata") if isinstance(child_payload.get("routing_metadata"), dict) else {}
        try:
            raw_result = self._run_one_shot_request(
                child_job_id,
                child_payload,
                target_model=target_model,
            )
            normalized_result = self._normalize_response_payload(
                raw_result,
                router_result={
                    "intent_type": child_routing.get("intent_type"),
                    "task_type": child_routing.get("task_type"),
                },
            )
            content = self._extract_assistant_text(normalized_result)
            if not content:
                content = self._extract_assistant_text(raw_result)
            if not content:
                content = "(completed without response text)"
            content = self._maybe_ground_inception_weather_response(
                target_model=target_model,
                request_payload=child_payload,
                content=content,
            )
            return {
                "index": index,
                "model": target_model,
                "job_id": child_job_id,
                "status": "completed",
                "content": content,
            }
        except Exception as exc:
            logger.warning(
                f"{fanout_kind} child failed",
                extra={"parent_job_id": parent_job_id, "child_job_id": child_job_id, "model": target_model, "error": str(exc)},
            )
            return {
                "index": index,
                "model": target_model,
                "job_id": child_job_id,
                "status": "failed",
                "error": str(exc),
            }

    @staticmethod
    def _is_inceptionlabs_model(model_name: str) -> bool:
        tail = str(model_name or "").strip().split("/")[-1].lower()
        return tail in {"git-inceptionlabs", "inceptionlabs"}

    @staticmethod
    def _extract_payload_prompt(payload: dict[str, Any]) -> str:
        prompt = str(payload.get("user_prompt") or payload.get("prompt") or "").strip()
        return prompt

    @staticmethod
    def _is_weather_prompt(prompt: str) -> bool:
        lower = str(prompt or "").lower()
        if not lower:
            return False
        weather_markers = (
            "weather",
            "forecast",
            "temperature",
            "feels like",
            "humidity",
            "wind",
            "rain",
            "snow",
            "sunny",
            "cloudy",
        )
        return any(marker in lower for marker in weather_markers)

    @staticmethod
    def _extract_weather_location(prompt: str) -> str:
        text = str(prompt or "").strip()
        if not text:
            return ""
        patterns = [
            r"weather(?:\s+like)?(?:\s+today|\s+now)?\s+in\s+([A-Za-z0-9 .,'-]+)",
            r"\bin\s+([A-Za-z0-9 .,'-]+)(?:\?|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = match.group(1).strip(" .,\n\t?")
            if candidate:
                return candidate
        return ""

    @staticmethod
    def _c_to_f(value: float | None) -> float | None:
        if value is None:
            return None
        return (value * 9 / 5) + 32

    @staticmethod
    def _iter_geocode_candidates(location: str) -> list[str]:
        raw = str(location or "").strip(" \t\r\n,.;")
        if not raw:
            return []
        candidates: list[str] = [raw]

        no_country = re.sub(r"(?i),?\s*(usa|u\.s\.a\.|us|united states)\.?\s*$", "", raw).strip(" ,.;")
        if no_country and no_country not in candidates:
            candidates.append(no_country)

        if "," in no_country:
            first_segment = no_country.split(",", 1)[0].strip(" ,.;")
            if first_segment and first_segment not in candidates:
                candidates.append(first_segment)

        if "," in raw:
            first_segment_raw = raw.split(",", 1)[0].strip(" ,.;")
            if first_segment_raw and first_segment_raw not in candidates:
                candidates.append(first_segment_raw)

        return candidates

    def _fetch_openmeteo_weather_summary(self, location: str) -> str:
        best = None
        for candidate in self._iter_geocode_candidates(location):
            geo_url = (
                "https://geocoding-api.open-meteo.com/v1/search?"
                + urllib.parse.urlencode({"name": candidate, "count": 1, "language": "en", "format": "json"})
            )
            with urllib.request.urlopen(geo_url, timeout=20) as resp:
                geo_payload = json.loads(resp.read().decode("utf-8"))
            results = geo_payload.get("results") or []
            if results:
                best = results[0]
                break

        if not isinstance(best, dict):
            raise RuntimeError(f"Could not geocode location: {location}")

        lat = best["latitude"]
        lon = best["longitude"]
        display_name_parts = [best.get("name"), best.get("admin1"), best.get("country")]
        display_name = ", ".join([part for part in display_name_parts if isinstance(part, str) and part.strip()])

        weather_url = (
            "https://api.open-meteo.com/v1/forecast?"
            + urllib.parse.urlencode(
                {
                    "latitude": lat,
                    "longitude": lon,
                    "timezone": "auto",
                    "current": "temperature_2m,weather_code,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "forecast_days": 1,
                }
            )
        )
        with urllib.request.urlopen(weather_url, timeout=20) as resp:
            weather_payload = json.loads(resp.read().decode("utf-8"))

        current = weather_payload.get("current", {})
        daily = weather_payload.get("daily", {})
        codes = {
            0: "Clear sky",
            1: "Mainly clear",
            2: "Partly cloudy",
            3: "Overcast",
            45: "Fog",
            48: "Depositing rime fog",
            51: "Light drizzle",
            53: "Moderate drizzle",
            55: "Dense drizzle",
            61: "Slight rain",
            63: "Moderate rain",
            65: "Heavy rain",
            71: "Slight snow",
            73: "Moderate snow",
            75: "Heavy snow",
            80: "Rain showers",
            81: "Rain showers",
            82: "Heavy rain showers",
            95: "Thunderstorm",
            96: "Thunderstorm with hail",
            99: "Thunderstorm with hail",
        }
        code = current.get("weather_code")
        description = codes.get(code, f"Weather code {code}")
        temp_c = current.get("temperature_2m")
        wind_kmh = current.get("wind_speed_10m")
        tmax = (daily.get("temperature_2m_max") or [None])[0]
        tmin = (daily.get("temperature_2m_min") or [None])[0]

        parts = [f"Weather for {display_name}:"]
        if temp_c is not None:
            parts.append(f"Current {temp_c:.1f}C ({self._c_to_f(temp_c):.1f}F), {description}.")
        else:
            parts.append(f"Current conditions: {description}.")
        if tmax is not None and tmin is not None:
            parts.append(f"Today high/low: {tmax:.1f}C/{tmin:.1f}C ({self._c_to_f(tmax):.1f}F/{self._c_to_f(tmin):.1f}F).")
        if wind_kmh is not None:
            parts.append(f"Wind: {wind_kmh:.1f} km/h.")
        parts.append("Source: Open-Meteo live API on " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
        return " ".join(parts).strip()

    def _maybe_ground_inception_weather_response(
        self,
        *,
        target_model: str,
        request_payload: dict[str, Any],
        content: str,
    ) -> str:
        if not self._is_inceptionlabs_model(target_model):
            return content

        prompt = self._extract_payload_prompt(request_payload)
        if not self._is_weather_prompt(prompt):
            return content

        location = self._extract_weather_location(prompt)
        if not location:
            logger.info(
                "inception weather grounding skipped: no location in prompt",
                extra={"model": target_model},
            )
            return content

        try:
            grounded = self._fetch_openmeteo_weather_summary(location)
            if grounded:
                return grounded
        except Exception as exc:
            logger.warning(
                "inception weather grounding failed; using model response",
                extra={"model": target_model, "location": location, "error": str(exc)},
            )

        return content

    @staticmethod
    def _extract_assistant_text(raw: Any) -> str:
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw.strip()
        if isinstance(raw, (int, float, bool)):
            return str(raw)
        if isinstance(raw, list):
            parts = [JobWorker._extract_assistant_text(item) for item in raw]
            joined = "\n".join(part for part in parts if part)
            return joined.strip()
        if not isinstance(raw, dict):
            return ""

        message = raw.get("message")
        if isinstance(message, dict):
            content = JobWorker._extract_assistant_text(message.get("content"))
            if content:
                return content
        elif isinstance(message, str):
            content = message.strip()
            if content:
                return content

        for key in ("response", "content", "text"):
            if key in raw:
                content = JobWorker._extract_assistant_text(raw.get(key))
                if content:
                    return content

        nested = raw.get("data")
        if isinstance(nested, dict):
            content = JobWorker._extract_assistant_text(nested)
            if content:
                return content

        return ""

    def _process_job_allsequential(
        self,
        job: dict[str, Any],
        intent_type: str | None = None,
        task_type: str | None = None,
    ) -> None:
        """
        Fan out one API request to multiple configured models, run sequentially,
        and return an ordered combined answer.
        """
        job_id = job["job_id"]
        request_payload = job.get("request_json") if isinstance(job.get("request_json"), dict) else {}
        requested_intent = str(intent_type or "").strip().lower()
        requested_task = str(task_type or "").strip().lower()

        if requested_intent == "job" or requested_task in {"scheduled_weather_report", "reminder"}:
            self._update_status(job_id, "failed", intent_type="job", task_type=task_type or "allsequential")
            db.mark_failed(
                job_id,
                {
                    "code": "ALLSEQUENTIAL_UNSUPPORTED_TASK",
                    "message": "git-allsequential is for question-style prompts and is not enabled for job execution.",
                },
                status="failed",
            )
            return

        targets = self._resolve_allsequential_targets()
        if not targets:
            self._update_status(job_id, "failed", intent_type="question", task_type="allsequential")
            db.mark_failed(
                job_id,
                {
                    "code": "ALLSEQUENTIAL_NO_TARGETS",
                    "message": "No target models configured for git-allsequential.",
                },
                status="failed",
            )
            return

        self._update_status(job_id, "routing", intent_type="question", task_type="allsequential")
        base_prompt = str(
            request_payload.get("user_prompt")
            or request_payload.get("prompt")
            or ""
        ).strip()

        if self._allsequential_virtual_turns_enabled():
            self._process_job_allsequential_virtual_turns(
                job_id=job_id,
                request_payload=request_payload,
                targets=targets,
                base_prompt=base_prompt,
            )
            return

        aggregated_results: list[dict[str, Any]] = []
        success_count = 0

        for idx, target_model in enumerate(targets, start=1):
            child_job_id = f"{job_id}_allseq_{idx:02d}_{self._sanitize_model_tail(target_model)}"
            self._update_status(
                job_id,
                "executing",
                intent_type="question",
                task_type=f"allsequential:{target_model}",
            )

            child_payload = self._build_allsequential_child_payload(
                request_payload,
                target_model=target_model,
                parent_job_id=job_id,
                index=idx,
                total=len(targets),
            )
            item = self._execute_fanout_child(
                parent_job_id=job_id,
                child_job_id=child_job_id,
                target_model=target_model,
                index=idx,
                child_payload=child_payload,
                fanout_kind="allsequential",
            )
            aggregated_results.append(item)
            if str(item.get("status") or "").strip().lower() == "completed":
                success_count += 1

        if success_count <= 0:
            self._update_status(job_id, "failed", intent_type="question", task_type="allsequential")
            db.mark_failed(
                job_id,
                {
                    "code": "ALLSEQUENTIAL_ALL_FAILED",
                    "message": "All models failed in git-allsequential.",
                    "details": {"results": aggregated_results},
                },
                status="failed",
            )
            return

        source_messages = self._build_allsequential_source_messages(aggregated_results)
        combined_content = self._format_allsequential_response(
            base_prompt=base_prompt,
            results=aggregated_results,
            source_messages=source_messages,
        )
        execution_meta = {
            "mode": "allsequential",
            "targets": targets,
            "results": aggregated_results,
            "source_messages": source_messages,
            "success_count": success_count,
            "failure_count": len(aggregated_results) - success_count,
        }
        final_payload = {
            "message": {"role": "assistant", "content": combined_content},
            "intent_type": "question",
            "task_type": "allsequential",
            "current_stage": "completed",
            "execution": execution_meta,
            "stages": {"allsequential": "complete"},
            "source_messages": source_messages,
            "done": True,
        }
        db.mark_completed(job_id, final_payload, execution_json=execution_meta, stages_json={"allsequential": "complete"})
        logger.info(
            "allsequential workflow completed",
            extra={"job_id": job_id, "status": "completed", "success_count": success_count, "total": len(aggregated_results)},
        )

    def _process_job_allparallel(
        self,
        job: dict[str, Any],
        intent_type: str | None = None,
        task_type: str | None = None,
    ) -> None:
        """
        Fan out one API request to multiple configured models and run them in parallel.
        """
        job_id = job["job_id"]
        request_payload = job.get("request_json") if isinstance(job.get("request_json"), dict) else {}
        requested_intent = str(intent_type or "").strip().lower()
        requested_task = str(task_type or "").strip().lower()

        if requested_intent == "job" or requested_task in {"scheduled_weather_report", "reminder"}:
            self._update_status(job_id, "failed", intent_type="job", task_type=task_type or "allparallel")
            db.mark_failed(
                job_id,
                {
                    "code": "ALLPARALLEL_UNSUPPORTED_TASK",
                    "message": "git-parallel is for question-style prompts and is not enabled for job execution.",
                },
                status="failed",
            )
            return

        targets = self._resolve_allparallel_targets()
        if not targets:
            self._update_status(job_id, "failed", intent_type="question", task_type="allparallel")
            db.mark_failed(
                job_id,
                {
                    "code": "ALLPARALLEL_NO_TARGETS",
                    "message": "No target models configured for git-parallel.",
                },
                status="failed",
            )
            return

        self._update_status(job_id, "routing", intent_type="question", task_type="allparallel")
        base_prompt = str(
            request_payload.get("user_prompt")
            or request_payload.get("prompt")
            or ""
        ).strip()
        request_payload, followup_context = self._maybe_apply_last_chat_context_for_parallel(
            job_id=job_id,
            request_payload=request_payload,
            base_prompt=base_prompt,
        )
        if isinstance(followup_context, dict):
            logger.info(
                "allparallel follow-up context resolution",
                extra={
                    "job_id": job_id,
                    "requested": bool(followup_context.get("requested")),
                    "applied": bool(followup_context.get("applied")),
                    "source_job_id": str(followup_context.get("source_job_id") or ""),
                    "reason": str(followup_context.get("reason") or ""),
                },
            )

        if self._allparallel_virtual_turns_enabled():
            self._process_job_allparallel_virtual_turns(
                job_id=job_id,
                request_payload=request_payload,
                targets=targets,
                base_prompt=base_prompt,
            )
            return

        self._update_status(job_id, "executing", intent_type="question", task_type="allparallel")
        aggregated_results: list[dict[str, Any]] = []
        max_workers = max(1, min(len(targets), 8))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures: list[concurrent.futures.Future[dict[str, Any]]] = []
            for idx, target_model in enumerate(targets, start=1):
                child_job_id = f"{job_id}_allpar_{idx:02d}_{self._sanitize_model_tail(target_model)}"
                child_payload = self._build_allparallel_child_payload(
                    request_payload,
                    target_model=target_model,
                    parent_job_id=job_id,
                    index=idx,
                    total=len(targets),
                )
                futures.append(
                    executor.submit(
                        self._execute_fanout_child,
                        parent_job_id=job_id,
                        child_job_id=child_job_id,
                        target_model=target_model,
                        index=idx,
                        child_payload=child_payload,
                        fanout_kind="allparallel",
                    )
                )
            for future in concurrent.futures.as_completed(futures):
                aggregated_results.append(future.result())

        ordered_results = sorted(
            aggregated_results,
            key=lambda item: int(item.get("index") or 0),
        )
        success_count = sum(1 for item in ordered_results if str(item.get("status") or "").strip().lower() == "completed")
        if success_count <= 0:
            self._update_status(job_id, "failed", intent_type="question", task_type="allparallel")
            db.mark_failed(
                job_id,
                {
                    "code": "ALLPARALLEL_ALL_FAILED",
                    "message": "All models failed in git-parallel.",
                    "details": {"results": ordered_results},
                },
                status="failed",
            )
            return

        source_messages = self._build_allsequential_source_messages(ordered_results)
        combined_content = self._format_allsequential_response(
            base_prompt=base_prompt,
            results=ordered_results,
            source_messages=source_messages,
        )
        execution_meta = {
            "mode": "allparallel",
            "targets": targets,
            "results": ordered_results,
            "source_messages": source_messages,
            "success_count": success_count,
            "failure_count": len(ordered_results) - success_count,
        }
        if isinstance(followup_context, dict):
            execution_meta["followup_context"] = followup_context
        final_payload = {
            "message": {"role": "assistant", "content": combined_content},
            "intent_type": "question",
            "task_type": "allparallel",
            "current_stage": "completed",
            "execution": execution_meta,
            "stages": {"allparallel": "complete"},
            "source_messages": source_messages,
            "done": True,
        }
        self._publish_allparallel_audit_artifact(
            job_id=job_id,
            request_payload=request_payload,
            execution_meta=execution_meta,
            combined_content=combined_content,
        )
        db.mark_completed(job_id, final_payload, execution_json=execution_meta, stages_json={"allparallel": "complete"})
        logger.info(
            "allparallel workflow completed",
            extra={"job_id": job_id, "status": "completed", "success_count": success_count, "total": len(ordered_results)},
        )

    def _allparallel_virtual_turns_enabled(self) -> bool:
        return bool(settings.allparallel_virtual_turns_enabled)

    def _process_job_allparallel_virtual_turns(
        self,
        job_id: str,
        request_payload: dict[str, Any],
        targets: list[str],
        base_prompt: str,
    ) -> None:
        send_followups = self._allsequential_followup_delivery_enabled()
        kickoff_content = self._build_virtual_turns_kickoff_content(
            len(targets),
            send_followups,
            include_auto_synthesis_note=self._fanout_auto_synthesis_enabled_for_request(request_payload),
        )
        kickoff_execution = self._build_allparallel_virtual_turns_execution(
            request_payload=request_payload,
            targets=targets,
            aggregated_results=[],
            delivery_errors=[],
            stage="virtual_turns_in_progress",
            kickoff_content=kickoff_content,
            base_prompt=base_prompt,
        )
        self._persist_allparallel_virtual_turns_state(job_id=job_id, execution_meta=kickoff_execution)
        logger.info(
            "allparallel virtual turns started",
            extra={"job_id": job_id, "status": "completed", "total": len(targets)},
        )
        self._spawn_allparallel_virtual_turns_background(job_id)

    def _build_allparallel_virtual_turns_execution(
        self,
        *,
        request_payload: dict[str, Any],
        targets: list[str],
        aggregated_results: list[dict[str, Any]],
        delivery_errors: list[dict[str, Any]],
        stage: str,
        kickoff_content: str,
        base_prompt: str,
        error: str | None = None,
    ) -> dict[str, Any]:
        ordered_results = sorted(
            aggregated_results,
            key=lambda item: int(item.get("index") or 0),
        )
        source_messages = self._build_allsequential_source_messages(ordered_results)
        success_count = sum(1 for item in ordered_results if str(item.get("status") or "").strip().lower() == "completed")
        completed_indexes = [
            int(item.get("index") or 0)
            for item in ordered_results
            if int(item.get("index") or 0) > 0
        ]
        execution_meta: dict[str, Any] = {
            "mode": "allparallel_virtual_turns",
            "request_payload": request_payload,
            "base_prompt": base_prompt,
            "kickoff_content": kickoff_content,
            "targets": targets,
            "results": ordered_results,
            "source_messages": source_messages,
            "success_count": success_count,
            "failure_count": len(ordered_results) - success_count,
            "delivery_errors": delivery_errors,
            "completed_indexes": completed_indexes,
            "stage": stage,
        }
        followup_context = request_payload.get("followup_context") if isinstance(request_payload, dict) else None
        if isinstance(followup_context, dict):
            execution_meta["followup_context"] = followup_context
        if error:
            execution_meta["error"] = str(error)
        if stage == "virtual_turns_complete":
            execution_meta["allparallel_summary"] = self._format_allsequential_response(
                base_prompt=base_prompt,
                results=ordered_results,
                source_messages=source_messages,
            )
        return execution_meta

    def _persist_allparallel_virtual_turns_state(self, *, job_id: str, execution_meta: dict[str, Any]) -> None:
        stage = str(execution_meta.get("stage") or "virtual_turns_in_progress").strip().lower()
        kickoff_content = str(execution_meta.get("kickoff_content") or "").strip()
        if not kickoff_content:
            target_count = len(execution_meta.get("targets") or [])
            kickoff_content = self._build_virtual_turns_kickoff_content(
                target_count,
                self._allsequential_followup_delivery_enabled(),
            )
        payload: dict[str, Any] = {
            "message": {"role": "assistant", "content": kickoff_content},
            "intent_type": "question",
            "task_type": "allparallel",
            "current_stage": stage,
            "execution": execution_meta,
            "stages": {"allparallel": stage},
            "source_messages": execution_meta.get("source_messages") or [],
            "done": True,
        }
        if stage == "virtual_turns_complete":
            payload["allparallel_summary"] = execution_meta.get("allparallel_summary")
            payload["current_stage"] = "completed"
        elif stage == "virtual_turns_error":
            payload["current_stage"] = "completed"
        db.mark_completed(
            job_id,
            payload,
            execution_json=execution_meta,
            stages_json={"allparallel": stage},
        )

    def _run_allparallel_virtual_turns_background(
        self,
        job_id: str,
    ) -> None:
        request_payload: dict[str, Any] = {}
        targets: list[str] = []
        base_prompt = ""
        kickoff_content = ""
        aggregated_results: list[dict[str, Any]] = []
        delivery_errors: list[dict[str, Any]] = []
        try:
            ensure_repo_ready()
            job = db.get_job(job_id) or {}
            request_payload = job.get("request_json") if isinstance(job.get("request_json"), dict) else {}
            response_json = job.get("response_json") if isinstance(job.get("response_json"), dict) else {}
            execution = response_json.get("execution") if isinstance(response_json.get("execution"), dict) else None
            if not isinstance(execution, dict):
                execution = job.get("execution_json") if isinstance(job.get("execution_json"), dict) else None
            if not isinstance(execution, dict):
                logger.warning("missing virtual-turn state, skipping allparallel background run", extra={"job_id": job_id})
                return

            mode = str(execution.get("mode") or "").strip().lower()
            stage = str(execution.get("stage") or "").strip().lower()
            if mode != "allparallel_virtual_turns" or stage != "virtual_turns_in_progress":
                return

            targets = [str(item).strip() for item in execution.get("targets") or [] if str(item).strip()]
            if not targets:
                logger.warning("allparallel virtual-turn state has no targets", extra={"job_id": job_id})
                return
            send_followups = self._allsequential_followup_delivery_enabled()
            if not send_followups:
                logger.info(
                    "allparallel virtual follow-up delivery disabled; progress is available via api job state",
                    extra={"job_id": job_id},
                )

            request_from_state = execution.get("request_payload")
            if isinstance(request_from_state, dict):
                request_payload = dict(request_from_state)
            base_prompt = str(
                execution.get("base_prompt")
                or request_payload.get("user_prompt")
                or request_payload.get("prompt")
                or ""
            ).strip()
            kickoff_content = str(execution.get("kickoff_content") or "").strip()
            if not kickoff_content:
                kickoff_content = self._build_virtual_turns_kickoff_content(
                    len(targets),
                    send_followups,
                    include_auto_synthesis_note=self._fanout_auto_synthesis_enabled_for_request(request_payload),
                )

            aggregated_results = self._coerce_list_of_dicts(execution.get("results"))
            delivery_errors = self._coerce_list_of_dicts(execution.get("delivery_errors"))
            completed_indexes = {
                int(item.get("index") or 0)
                for item in aggregated_results
                if int(item.get("index") or 0) > 0
            }
            pending: list[tuple[int, str]] = [
                (idx, target_model)
                for idx, target_model in enumerate(targets, start=1)
                if idx not in completed_indexes
            ]
            if self._stop_event.is_set():
                return
            if pending:
                max_workers = max(1, min(len(pending), 8))
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures: dict[concurrent.futures.Future[dict[str, Any]], tuple[int, str]] = {}
                    for idx, target_model in pending:
                        child_job_id = f"{job_id}_allpar_{idx:02d}_{self._sanitize_model_tail(target_model)}"
                        child_payload = self._build_allparallel_child_payload(
                            request_payload,
                            target_model=target_model,
                            parent_job_id=job_id,
                            index=idx,
                            total=len(targets),
                        )
                        future = executor.submit(
                            self._execute_fanout_child,
                            parent_job_id=job_id,
                            child_job_id=child_job_id,
                            target_model=target_model,
                            index=idx,
                            child_payload=child_payload,
                            fanout_kind="allparallel",
                        )
                        futures[future] = (idx, target_model)

                    for future in concurrent.futures.as_completed(futures):
                        item = future.result()
                        idx = int(item.get("index") or 0)
                        aggregated_results = [existing for existing in aggregated_results if int(existing.get("index") or 0) != idx]
                        aggregated_results.append(item)

                        item_status = str(item.get("status") or "").strip().lower()
                        should_send_item = item_status == "completed" or settings.allparallel_virtual_turns_send_failures
                        if send_followups and should_send_item:
                            source_messages = self._build_allsequential_source_messages(
                                [item],
                                total_override=len(targets),
                            )
                            for source_msg in source_messages:
                                ok, err = self._send_openclaw_channel_message(str(source_msg.get("text") or ""))
                                if not ok:
                                    delivery_errors.append(
                                        {
                                            "index": item.get("index"),
                                            "model": item.get("model"),
                                            "part_index": source_msg.get("part_index"),
                                            "part_total": source_msg.get("part_total"),
                                            "error": err or "unknown delivery error",
                                        }
                                    )
                                    logger.warning(
                                        "allparallel virtual delivery failed",
                                        extra={
                                            "job_id": job_id,
                                            "model": item.get("model"),
                                            "index": item.get("index"),
                                            "error": err or "unknown delivery error",
                                        },
                                    )

                        checkpoint_execution = self._build_allparallel_virtual_turns_execution(
                            request_payload=request_payload,
                            targets=targets,
                            aggregated_results=aggregated_results,
                            delivery_errors=delivery_errors,
                            stage="virtual_turns_in_progress",
                            kickoff_content=kickoff_content,
                            base_prompt=base_prompt,
                        )
                        self._persist_allparallel_virtual_turns_state(job_id=job_id, execution_meta=checkpoint_execution)
        except Exception as exc:
            logger.exception(
                "allparallel virtual background run failed",
                extra={"job_id": job_id, "error": str(exc)},
            )
            error_execution = self._build_allparallel_virtual_turns_execution(
                request_payload=request_payload if isinstance(request_payload, dict) else {},
                targets=targets if isinstance(targets, list) else [],
                aggregated_results=aggregated_results if isinstance(aggregated_results, list) else [],
                delivery_errors=delivery_errors if isinstance(delivery_errors, list) else [],
                stage="virtual_turns_error",
                kickoff_content=kickoff_content if isinstance(kickoff_content, str) else "",
                base_prompt=base_prompt if isinstance(base_prompt, str) else "",
                error=str(exc),
            )
            self._persist_allparallel_virtual_turns_state(job_id=job_id, execution_meta=error_execution)
        else:
            if not self._stop_event.is_set():
                final_execution = self._build_allparallel_virtual_turns_execution(
                    request_payload=request_payload,
                    targets=targets,
                    aggregated_results=aggregated_results,
                    delivery_errors=delivery_errors,
                    stage="virtual_turns_complete",
                    kickoff_content=kickoff_content,
                    base_prompt=base_prompt,
                )
                final_execution["auto_synthesis"] = self._maybe_run_fanout_auto_synthesis(
                    parent_job_id=job_id,
                    fanout_mode="allparallel_virtual_turns",
                    request_payload=request_payload,
                    base_prompt=base_prompt,
                    fanout_results=aggregated_results,
                )
                self._publish_allparallel_audit_artifact(
                    job_id=job_id,
                    request_payload=request_payload,
                    execution_meta=final_execution,
                    combined_content=str(final_execution.get("allparallel_summary") or ""),
                )
                self._persist_allparallel_virtual_turns_state(job_id=job_id, execution_meta=final_execution)
                logger.info(
                    "allparallel virtual turns completed",
                    extra={
                        "job_id": job_id,
                        "status": "completed",
                        "success_count": final_execution.get("success_count"),
                        "total": len(aggregated_results),
                        "delivery_failures": len(delivery_errors),
                    },
                )
        finally:
            with self._virtual_turn_threads_lock:
                self._virtual_turn_active_jobs.discard(job_id)

    def _allsequential_virtual_turns_enabled(self) -> bool:
        return bool(settings.allsequential_virtual_turns_enabled)

    @staticmethod
    def _build_virtual_turns_kickoff_content(
        target_count: int,
        send_followups: bool,
        include_auto_synthesis_note: bool = False,
    ) -> str:
        if send_followups:
            content = (
                f"Running this prompt across {target_count} sources now. "
                "I will send each source result as a separate follow-up message."
            )
            if include_auto_synthesis_note:
                content += " After all sources finish, I will also send one synthesized follow-up."
            return content
        return (
            f"Running this prompt across {target_count} sources now. "
            "Follow-up delivery is not configured, so check /api/jobs/<job_id> for per-source progress and results."
        )

    def _allsequential_followup_delivery_enabled(self) -> bool:
        if not settings.openclaw_cron_ssh_target.strip():
            return False
        if not settings.openclaw_cron_to.strip():
            return False
        return True

    def _process_job_allsequential_virtual_turns(
        self,
        job_id: str,
        request_payload: dict[str, Any],
        targets: list[str],
        base_prompt: str,
    ) -> None:
        send_followups = self._allsequential_followup_delivery_enabled()
        kickoff_content = self._build_virtual_turns_kickoff_content(
            len(targets),
            send_followups,
            include_auto_synthesis_note=self._fanout_auto_synthesis_enabled_for_request(request_payload),
        )
        kickoff_execution = self._build_allsequential_virtual_turns_execution(
            request_payload=request_payload,
            targets=targets,
            aggregated_results=[],
            delivery_errors=[],
            stage="virtual_turns_in_progress",
            kickoff_content=kickoff_content,
            base_prompt=base_prompt,
        )
        self._persist_allsequential_virtual_turns_state(job_id=job_id, execution_meta=kickoff_execution)
        logger.info(
            "allsequential virtual turns started",
            extra={"job_id": job_id, "status": "completed", "total": len(targets)},
        )
        self._spawn_allsequential_virtual_turns_background(job_id)

    @staticmethod
    def _coerce_list_of_dicts(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        out: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                out.append(dict(item))
        return out

    def _build_allsequential_virtual_turns_execution(
        self,
        *,
        request_payload: dict[str, Any],
        targets: list[str],
        aggregated_results: list[dict[str, Any]],
        delivery_errors: list[dict[str, Any]],
        stage: str,
        kickoff_content: str,
        base_prompt: str,
        error: str | None = None,
    ) -> dict[str, Any]:
        source_messages = self._build_allsequential_source_messages(aggregated_results)
        success_count = sum(1 for item in aggregated_results if str(item.get("status") or "").strip().lower() == "completed")
        next_index = min(len(targets) + 1, len(aggregated_results) + 1)
        execution_meta: dict[str, Any] = {
            "mode": "allsequential_virtual_turns",
            "request_payload": request_payload,
            "base_prompt": base_prompt,
            "kickoff_content": kickoff_content,
            "targets": targets,
            "results": aggregated_results,
            "source_messages": source_messages,
            "success_count": success_count,
            "failure_count": len(aggregated_results) - success_count,
            "delivery_errors": delivery_errors,
            "next_index": next_index,
            "stage": stage,
        }
        if error:
            execution_meta["error"] = str(error)
        if stage == "virtual_turns_complete":
            execution_meta["allsequential_summary"] = self._format_allsequential_response(
                base_prompt=base_prompt,
                results=aggregated_results,
                source_messages=source_messages,
            )
        return execution_meta

    def _persist_allsequential_virtual_turns_state(self, *, job_id: str, execution_meta: dict[str, Any]) -> None:
        stage = str(execution_meta.get("stage") or "virtual_turns_in_progress").strip().lower()
        kickoff_content = str(execution_meta.get("kickoff_content") or "").strip()
        if not kickoff_content:
            target_count = len(execution_meta.get("targets") or [])
            kickoff_content = self._build_virtual_turns_kickoff_content(
                target_count,
                self._allsequential_followup_delivery_enabled(),
            )
        payload: dict[str, Any] = {
            "message": {"role": "assistant", "content": kickoff_content},
            "intent_type": "question",
            "task_type": "allsequential",
            "current_stage": stage,
            "execution": execution_meta,
            "stages": {"allsequential": stage},
            "source_messages": execution_meta.get("source_messages") or [],
            "done": True,
        }
        if stage == "virtual_turns_complete":
            payload["allsequential_summary"] = execution_meta.get("allsequential_summary")
            payload["current_stage"] = "completed"
        elif stage == "virtual_turns_error":
            payload["current_stage"] = "completed"
        db.mark_completed(
            job_id,
            payload,
            execution_json=execution_meta,
            stages_json={"allsequential": stage},
        )

    def _run_allsequential_virtual_turns_background(
        self,
        job_id: str,
    ) -> None:
        request_payload: dict[str, Any] = {}
        targets: list[str] = []
        base_prompt = ""
        kickoff_content = ""
        aggregated_results: list[dict[str, Any]] = []
        delivery_errors: list[dict[str, Any]] = []
        try:
            ensure_repo_ready()
            job = db.get_job(job_id) or {}
            request_payload = job.get("request_json") if isinstance(job.get("request_json"), dict) else {}
            response_json = job.get("response_json") if isinstance(job.get("response_json"), dict) else {}
            execution = response_json.get("execution") if isinstance(response_json.get("execution"), dict) else None
            if not isinstance(execution, dict):
                execution = job.get("execution_json") if isinstance(job.get("execution_json"), dict) else None
            if not isinstance(execution, dict):
                logger.warning("missing virtual-turn state, skipping background run", extra={"job_id": job_id})
                return

            mode = str(execution.get("mode") or "").strip().lower()
            stage = str(execution.get("stage") or "").strip().lower()
            if mode != "allsequential_virtual_turns" or stage != "virtual_turns_in_progress":
                return

            targets = [str(item).strip() for item in execution.get("targets") or [] if str(item).strip()]
            if not targets:
                logger.warning("virtual-turn state has no targets", extra={"job_id": job_id})
                return
            send_followups = self._allsequential_followup_delivery_enabled()
            if not send_followups:
                logger.info(
                    "allsequential virtual follow-up delivery disabled; progress is available via api job state",
                    extra={"job_id": job_id},
                )

            request_from_state = execution.get("request_payload")
            if isinstance(request_from_state, dict):
                request_payload = dict(request_from_state)
            base_prompt = str(
                execution.get("base_prompt")
                or request_payload.get("user_prompt")
                or request_payload.get("prompt")
                or ""
            ).strip()
            kickoff_content = str(execution.get("kickoff_content") or "").strip()
            if not kickoff_content:
                kickoff_content = self._build_virtual_turns_kickoff_content(
                    len(targets),
                    send_followups,
                    include_auto_synthesis_note=self._fanout_auto_synthesis_enabled_for_request(request_payload),
                )

            aggregated_results = self._coerce_list_of_dicts(execution.get("results"))
            delivery_errors = self._coerce_list_of_dicts(execution.get("delivery_errors"))
            next_index = int(execution.get("next_index") or (len(aggregated_results) + 1))
            next_index = max(1, min(next_index, len(targets) + 1))

            for idx in range(next_index, len(targets) + 1):
                if self._stop_event.is_set():
                    break
                target_model = targets[idx - 1]
                child_job_id = f"{job_id}_allseq_{idx:02d}_{self._sanitize_model_tail(target_model)}"
                child_payload = self._build_allsequential_child_payload(
                    request_payload,
                    target_model=target_model,
                    parent_job_id=job_id,
                    index=idx,
                    total=len(targets),
                )
                child_routing = child_payload.get("routing_metadata") if isinstance(child_payload.get("routing_metadata"), dict) else {}

                item: dict[str, Any]
                try:
                    raw_result = self._run_one_shot_request(child_job_id, child_payload)
                    normalized_result = self._normalize_response_payload(
                        raw_result,
                        router_result={
                            "intent_type": child_routing.get("intent_type"),
                            "task_type": child_routing.get("task_type"),
                        },
                    )
                    content = self._extract_assistant_text(normalized_result)
                    if not content:
                        content = self._extract_assistant_text(raw_result)
                    if not content:
                        content = "(completed without response text)"

                    item = {
                        "index": idx,
                        "model": target_model,
                        "job_id": child_job_id,
                        "status": "completed",
                        "content": content,
                    }
                except Exception as exc:
                    item = {
                        "index": idx,
                        "model": target_model,
                        "job_id": child_job_id,
                        "status": "failed",
                        "error": str(exc),
                    }
                    logger.warning(
                        "allsequential child failed",
                        extra={
                            "parent_job_id": job_id,
                            "child_job_id": child_job_id,
                            "model": target_model,
                            "error": str(exc),
                        },
                    )

                aggregated_results.append(item)

                item_status = str(item.get("status") or "").strip().lower()
                should_send_item = item_status == "completed" or settings.allsequential_virtual_turns_send_failures
                if send_followups and should_send_item:
                    source_messages = self._build_allsequential_source_messages(
                        [item],
                        total_override=len(targets),
                    )
                    for source_msg in source_messages:
                        ok, err = self._send_openclaw_channel_message(str(source_msg.get("text") or ""))
                        if not ok:
                            delivery_errors.append(
                                {
                                    "index": idx,
                                    "model": target_model,
                                    "part_index": source_msg.get("part_index"),
                                    "part_total": source_msg.get("part_total"),
                                    "error": err or "unknown delivery error",
                                }
                            )
                            logger.warning(
                                "allsequential virtual delivery failed",
                                extra={
                                    "job_id": job_id,
                                    "model": target_model,
                                    "index": idx,
                                    "error": err or "unknown delivery error",
                                },
                            )

                checkpoint_execution = self._build_allsequential_virtual_turns_execution(
                    request_payload=request_payload,
                    targets=targets,
                    aggregated_results=aggregated_results,
                    delivery_errors=delivery_errors,
                    stage="virtual_turns_in_progress",
                    kickoff_content=kickoff_content,
                    base_prompt=base_prompt,
                )
                self._persist_allsequential_virtual_turns_state(job_id=job_id, execution_meta=checkpoint_execution)
        except Exception as exc:
            logger.exception(
                "allsequential virtual background run failed",
                extra={"job_id": job_id, "error": str(exc)},
            )
            error_execution = self._build_allsequential_virtual_turns_execution(
                request_payload=request_payload if isinstance(request_payload, dict) else {},
                targets=targets if isinstance(targets, list) else [],
                aggregated_results=aggregated_results if isinstance(aggregated_results, list) else [],
                delivery_errors=delivery_errors if isinstance(delivery_errors, list) else [],
                stage="virtual_turns_error",
                kickoff_content=kickoff_content if isinstance(kickoff_content, str) else "",
                base_prompt=base_prompt if isinstance(base_prompt, str) else "",
                error=str(exc),
            )
            self._persist_allsequential_virtual_turns_state(job_id=job_id, execution_meta=error_execution)
        else:
            if not self._stop_event.is_set():
                final_execution = self._build_allsequential_virtual_turns_execution(
                    request_payload=request_payload,
                    targets=targets,
                    aggregated_results=aggregated_results,
                    delivery_errors=delivery_errors,
                    stage="virtual_turns_complete",
                    kickoff_content=kickoff_content,
                    base_prompt=base_prompt,
                )
                final_execution["auto_synthesis"] = self._maybe_run_fanout_auto_synthesis(
                    parent_job_id=job_id,
                    fanout_mode="allsequential_virtual_turns",
                    request_payload=request_payload,
                    base_prompt=base_prompt,
                    fanout_results=aggregated_results,
                )
                self._persist_allsequential_virtual_turns_state(job_id=job_id, execution_meta=final_execution)
                logger.info(
                    "allsequential virtual turns completed",
                    extra={
                        "job_id": job_id,
                        "status": "completed",
                        "success_count": final_execution.get("success_count"),
                        "total": len(aggregated_results),
                        "delivery_failures": len(delivery_errors),
                    },
                )
        finally:
            with self._virtual_turn_threads_lock:
                self._virtual_turn_active_jobs.discard(job_id)

    def _build_allsequential_source_messages(
        self,
        results: list[dict[str, Any]],
        total_override: int | None = None,
    ) -> list[dict[str, Any]]:
        total = int(total_override) if total_override else len(results)
        if total <= 0:
            total = len(results)
        source_messages: list[dict[str, Any]] = []
        for item in results:
            idx = item.get("index")
            model_name = str(item.get("model") or "unknown").strip() or "unknown"
            status = str(item.get("status") or "unknown").strip() or "unknown"
            if status == "completed":
                content = self._compact_for_telegram_chunking(str(item.get("content") or "")) or "(empty response)"
            else:
                err = str(item.get("error") or "unknown error").strip() or "unknown error"
                content = f"Error: {err}"
            parts = self._split_for_transport(content)
            part_total = len(parts)
            for part_idx, part_content in enumerate(parts, start=1):
                if part_total > 1:
                    header = f"[{idx}/{total}] Source: {model_name} | Status: {status} | Part {part_idx}/{part_total}"
                else:
                    header = f"[{idx}/{total}] Source: {model_name} | Status: {status}"
                source_messages.append(
                    {
                        "index": idx,
                        "source": model_name,
                        "status": status,
                        "part_index": part_idx,
                        "part_total": part_total,
                        "content": part_content,
                        "text": f"{header}\n{part_content}",
                    }
                )
        return source_messages

    def _format_allsequential_response(
        self,
        base_prompt: str,
        results: list[dict[str, Any]],
        source_messages: list[dict[str, Any]] | None = None,
    ) -> str:
        lines: list[str] = []
        for entry in source_messages or self._build_allsequential_source_messages(results):
            lines.append(str(entry.get("text") or "").strip())
            lines.append("")
        return "\n".join(lines).strip()

    def _build_synthesis_followup_messages(self, content: str) -> list[dict[str, Any]]:
        normalized = self._compact_for_telegram_chunking(str(content or "")) or "(empty response)"
        parts = self._split_for_transport(normalized)
        part_total = len(parts)
        messages: list[dict[str, Any]] = []
        for part_index, part_content in enumerate(parts, start=1):
            if part_total > 1:
                header = f"[synth] Source: git-synth | Status: completed | Part {part_index}/{part_total}"
            else:
                header = "[synth] Source: git-synth | Status: completed"
            messages.append(
                {
                    "part_index": part_index,
                    "part_total": part_total,
                    "content": part_content,
                    "text": f"{header}\n{part_content}",
                }
            )
        return messages

    def _publish_allparallel_audit_artifact(
        self,
        *,
        job_id: str,
        request_payload: dict[str, Any],
        execution_meta: dict[str, Any],
        combined_content: str,
    ) -> None:
        if str(execution_meta.get("mode") or "").strip().lower() not in {"allparallel", "allparallel_virtual_turns"}:
            return

        summary_text = str(combined_content or "").strip()
        if not summary_text:
            return

        stage = str(execution_meta.get("stage") or "complete").strip().lower()
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        combined_payload = {
            "job_id": job_id,
            "status": {
                "job_id": job_id,
                "state": "completed",
                "intent_type": "question",
                "task_type": "allparallel",
                "current_stage": stage,
                "updated_at": now_iso,
                "created_at": now_iso,
            },
            "response": {
                "job_id": job_id,
                "message": {"role": "assistant", "content": summary_text},
                "done": True,
                "completed_at": now_iso,
            },
            "execution": execution_meta,
            "request": request_payload,
            "evaluation": None,
            "generated_at": now_iso,
        }

        combined_path = settings.repo_path / settings.combined_dir / f"{job_id}.json"
        combined_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with REPO_LOCK:
                sync_repo_to_remote_head()
                combined_path.write_text(
                    json.dumps(combined_payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                commit_and_push_paths(job_id, [combined_path], f"audit: aggregate git-parallel results for {job_id} [skip ci]")
            logger.info("allparallel audit artifact pushed", extra={"job_id": job_id, "path": str(combined_path)})
        except Exception as exc:
            logger.warning(
                "allparallel audit artifact push failed",
                extra={"job_id": job_id, "path": str(combined_path), "error": str(exc)},
            )

    def _send_openclaw_channel_message(self, text: str) -> tuple[bool, str]:
        body = str(text or "").strip()
        if not body:
            return False, "empty message payload"
        if not settings.openclaw_cron_ssh_target.strip():
            return False, "OPENCLAW_CRON_SSH_TARGET is not configured"
        if not settings.openclaw_cron_to.strip():
            return False, "OPENCLAW_CRON_TO is not configured"

        remote_arg_variants = [
            [
                settings.openclaw_cron_cli_path,
                "message",
                "send",
                "--channel",
                settings.openclaw_cron_channel,
                "--target",
                settings.openclaw_cron_to.strip(),
                "--message",
                body,
                "--json",
            ],
            [
                settings.openclaw_cron_cli_path,
                "message",
                "send",
                "--channel",
                settings.openclaw_cron_channel,
                "--to",
                settings.openclaw_cron_to.strip(),
                "--message",
                body,
                "--json",
            ],
        ]

        transport_attempts: list[tuple[str, list[str]]] = []
        win_ssh = Path(settings.openclaw_cron_windows_ssh_path)
        if win_ssh.exists():
            transport_attempts.append(("windows_ssh", [str(win_ssh), settings.openclaw_cron_ssh_target]))
        transport_attempts.append(("ssh", ["ssh", settings.openclaw_cron_ssh_target]))

        errors: list[str] = []
        for transport, ssh_cmd in transport_attempts:
            for remote_args in remote_arg_variants:
                remote_command = " ".join(shlex.quote(arg) for arg in remote_args)
                cmd = [*ssh_cmd, remote_command]
                try:
                    proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=settings.openclaw_cron_timeout_seconds,
                        check=False,
                    )
                except Exception as exc:
                    errors.append(f"{transport}: {exc}")
                    continue
                if proc.returncode == 0:
                    return True, ""
                err_text = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
                errors.append(f"{transport}: {err_text}")

        return False, " | ".join(errors) if errors else "OpenClaw message send failed"

    def _apply_runtime_handoff_if_configured(self, job_id: str, result: dict[str, Any]) -> dict[str, Any]:
        if not settings.enable_runtime_handoff_executor:
            return result
        if not settings.openclaw_cron_ssh_target.strip():
            return result
        if not isinstance(result, dict):
            return result

        execution = result.get("execution") if isinstance(result.get("execution"), dict) else None
        if execution is None:
            execution_path = settings.repo_path / settings.execution_dir / f"{job_id}.json"
            if execution_path.exists():
                try:
                    loaded = json.loads(execution_path.read_text(encoding="utf-8"))
                    execution = loaded if isinstance(loaded, dict) else None
                except Exception:
                    execution = None
        if execution is None:
            return result

        task_type = canonicalize_task_type(str(execution.get("task_type") or ""))
        execution_status = str(execution.get("execution_status") or "").lower()
        if task_type != "scheduled_weather_report":
            return result
        if execution_status not in {"handoff_required", "needs_clarification"}:
            return result

        bridged_execution, user_message = self._bridge_openclaw_cron_from_execution(job_id=job_id, execution=execution)
        updated = dict(result)
        updated["execution"] = bridged_execution
        if user_message:
            updated["message"] = {"role": "assistant", "content": user_message}
        if str(bridged_execution.get("execution_status") or "").lower() == "success":
            updated["done"] = True
            updated["needs_clarification"] = False
            updated["completed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return updated

    def _bridge_openclaw_cron_from_execution(self, job_id: str, execution: dict[str, Any]) -> tuple[dict[str, Any], str]:
        details = execution.get("details") if isinstance(execution.get("details"), dict) else {}
        params = details.get("parameters") if isinstance(details.get("parameters"), dict) else {}
        params = self._normalize_weather_schedule_parameters(params)

        location = self._first_nonempty(params.get("location"), "Indianapolis")
        timezone_value = self._first_nonempty(params.get("timezone"), settings.default_user_timezone)
        cron_expr = self._derive_cron_expression(params)
        enabled = bool(params.get("enabled", False))
        cron_name = self._first_nonempty(params.get("cron_name"), params.get("job_name"), f"weather-{job_id[:8]}")
        if not cron_expr:
            failed = {
                **execution,
                "execution_status": "failed",
                "verified": False,
                "details": {
                    "code": "CRON_BRIDGE_INVALID_SCHEDULE",
                    "message": "Could not derive cron expression from execution parameters.",
                    "parameters": params,
                },
            }
            return failed, "I could not create the OpenClaw cron job because the schedule parameters were incomplete."

        weather_message = (
            f"Get the current weather for {location} and send a concise Telegram update "
            "with temperature, conditions, and notable rain/wind risk."
        )
        remote_args = [
            settings.openclaw_cron_cli_path,
            "cron",
            "add",
            "--name",
            cron_name,
            "--cron",
            cron_expr,
            "--tz",
            timezone_value,
            "--agent",
            settings.openclaw_cron_agent,
            "--message",
            weather_message,
            "--channel",
            settings.openclaw_cron_channel,
            "--json",
        ]
        if not enabled:
            remote_args.append("--disabled")
        if settings.openclaw_cron_to.strip():
            remote_args.extend(["--to", settings.openclaw_cron_to.strip()])
        remote_command = " ".join(shlex.quote(arg) for arg in remote_args)

        attempts: list[tuple[str, list[str]]] = []
        win_ssh = Path(settings.openclaw_cron_windows_ssh_path)
        if win_ssh.exists():
            attempts.append(("windows_ssh", [str(win_ssh), settings.openclaw_cron_ssh_target, remote_command]))
        attempts.append(("ssh", ["ssh", settings.openclaw_cron_ssh_target, remote_command]))

        proc: subprocess.CompletedProcess[str] | None = None
        transport_used = ""
        errors: list[str] = []
        for transport, cmd in attempts:
            try:
                candidate = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=settings.openclaw_cron_timeout_seconds,
                    check=False,
                )
            except Exception as exc:
                errors.append(f"{transport}: {exc}")
                continue
            if candidate.returncode == 0:
                proc = candidate
                transport_used = transport
                break
            err_text = (candidate.stderr or candidate.stdout or "").strip() or f"exit {candidate.returncode}"
            errors.append(f"{transport}: {err_text}")

        if proc is None:
            failed = {
                **execution,
                "execution_status": "failed",
                "verified": False,
                "details": {
                    "code": "CRON_BRIDGE_EXECUTION_FAILED",
                    "message": " | ".join(errors) if errors else "All OpenClaw cron bridge attempts failed.",
                    "parameters": params,
                },
            }
            return failed, "I could not create the OpenClaw cron job because the bridge command failed."

        cron_id = ""
        payload = self._parse_json_object(proc.stdout)
        if isinstance(payload, dict):
            cron_id = str(payload.get("id") or "").strip()

        success = {
            **execution,
            "execution_status": "success",
            "verified": True,
            "details": {
                **(details if isinstance(details, dict) else {}),
                "bridge": "openclaw_cron",
                "ssh_transport": transport_used or "unknown",
                "cron_id": cron_id or None,
                "cron_name": cron_name,
                "cron_expr": cron_expr,
                "timezone": timezone_value,
                "enabled": enabled,
                "location": location,
                "raw_output": (proc.stdout or "").strip(),
            },
        }
        state_word = "enabled" if enabled else "disabled"
        msg = f"Created {state_word} OpenClaw cron job `{cron_name}` for {location} at {cron_expr} ({timezone_value})."
        if cron_id:
            msg = f"{msg} Job ID: {cron_id}."
        return success, msg

    @staticmethod
    def _derive_cron_expression(params: dict[str, Any]) -> str:
        raw = str(params.get("cron_schedule") or params.get("cron") or params.get("cron_expression") or "").strip()
        if raw:
            parts = raw.split()
            if len(parts) in {5, 6}:
                return raw
        time_value = str(params.get("time") or params.get("send_time") or params.get("time_of_day") or "").strip()
        m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", time_value)
        if not m:
            return ""
        hour = int(m.group(1))
        minute = int(m.group(2))
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return ""
        frequency = str(params.get("frequency") or "daily").strip().lower()
        if frequency in {"daily", "everyday", "every_day"}:
            return f"{minute} {hour} * * *"
        return ""

    @staticmethod
    def _parse_json_object(text: str) -> dict[str, Any] | None:
        raw = (text or "").strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(raw[start : end + 1])
                return parsed if isinstance(parsed, dict) else None
            except Exception:
                return None
        return None

    def _submit_stage_mode_request(self, job: dict[str, Any]) -> None:
        """
        Stage orchestration consumes stage artifacts emitted by the normal request pipeline run.
        Submit the request once up front so router/planner/answerer/final stage artifacts can appear.
        """
        job_id = job["job_id"]
        with REPO_LOCK:
            sync_repo_to_remote_head()
            request_payload_raw = job.get("request_json") if isinstance(job.get("request_json"), dict) else {}
            routing = self._extract_routing_metadata(job)
            intent_type = str(routing.get("intent_type") or "").strip() or None
            task_type = str(routing.get("task_type") or "").strip() or None
            request_payload = self._coerce_routing_metadata(
                request_payload_raw,
                default_intent_type=intent_type,
                default_task_type=task_type,
                requires_local_execution=True if intent_type == "job" else None,
            )
            request_path = write_request_artifact(job_id, request_payload)
            commit_and_push_request(job_id, request_path)

    def _extract_routing_metadata(self, job: dict[str, Any]) -> dict[str, Any]:
        request_json = job.get("request_json") or {}
        routing = request_json.get("routing_metadata")
        return routing if isinstance(routing, dict) else {}

    def _update_status(self, job_id: str, status: str, intent_type: str | None = None, task_type: str | None = None) -> None:
        if hasattr(db, "update_job_status"):
            db.update_job_status(job_id, status=status, intent_type=intent_type, task_type=task_type)
        elif status == "routing" and hasattr(db, "mark_running"):
            db.mark_running(job_id)
        logger.info(
            "job stage update",
            extra={"job_id": job_id, "status": status, "intent_type": intent_type, "task_type": task_type},
        )

    def _mark_needs_clarification(
        self,
        job_id: str,
        planner_result: dict[str, Any],
        intent_type: str | None = None,
        task_type: str | None = None,
    ) -> None:
        payload = {
            "message": {
                "role": "assistant",
                "content": planner_result.get("question") or "I need a bit more information before I can do that.",
            },
            "intent_type": intent_type,
            "task_type": task_type,
            "current_stage": "needs_clarification",
            "planner": planner_result,
        }
        if hasattr(db, "mark_needs_clarification"):
            db.mark_needs_clarification(job_id, payload)
        elif hasattr(db, "mark_completed"):
            db.mark_completed(job_id, payload)

    def _build_execution_clarification_question(self, execution_result: dict[str, Any]) -> str:
        details = execution_result.get("details") if isinstance(execution_result.get("details"), dict) else {}
        missing = details.get("missing_fields")
        if isinstance(missing, list) and missing:
            missing_text = ", ".join(str(x) for x in missing if str(x).strip())
            if missing_text:
                task_type = execution_result.get("task_type") or "this task"
                return f"To continue {task_type}, I still need: {missing_text}."
        message = details.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return "I need a bit more information before I can do that."

    def _run_router_stage(self, job: dict[str, Any]) -> dict[str, Any]:
        return self._run_stage_via_pipeline(job["job_id"], stage_name="router")

    def _run_answerer_stage(self, job: dict[str, Any], router_result: dict[str, Any]) -> dict[str, Any]:
        result = self._run_stage_via_pipeline(job["job_id"], stage_name="answerer")
        return self._normalize_response_payload(result, router_result=router_result)

    def _run_planner_stage(self, job: dict[str, Any], router_result: dict[str, Any]) -> dict[str, Any]:
        return self._run_stage_via_pipeline(job["job_id"], stage_name="planner")

    def _execute_local_task(self, job: dict[str, Any], planner_result: dict[str, Any]) -> dict[str, Any]:
        original_task_type = str(planner_result.get("task_type") or "").strip()
        task_type = canonicalize_task_type(original_task_type)
        parameters = planner_result.get("parameters") if isinstance(planner_result.get("parameters"), dict) else {}
        success_condition = str(planner_result.get("success_condition") or "").strip()
        if task_type == "scheduled_weather_report":
            parameters = self._normalize_weather_schedule_parameters(parameters)

        base = {
            "task_type": task_type or None,
            "original_task_type": original_task_type or None,
            "verified": False,
            "executed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }

        if not task_type:
            return {
                **base,
                "execution_status": "failed",
                "details": {
                    "code": "MISSING_TASK_TYPE",
                    "message": "Planner output did not include task_type.",
                    "parameters": parameters,
                },
            }

        if task_type.startswith("system_"):
            return {
                **base,
                "execution_status": "success",
                "verified": True,
                "details": {
                    "executor": "system_noop",
                    "message": "System meta task requires no external execution.",
                    "task_type": task_type,
                    "original_task_type": original_task_type or None,
                    "response_text": str(parameters.get("response_text") or parameters.get("message") or ""),
                    "success_condition": success_condition,
                },
            }

        task = get_task(task_type)
        if task is None:
            return {
                **base,
                "execution_status": "failed",
                "details": {
                    "code": "UNSUPPORTED_TASK_TYPE",
                    "message": f"Unsupported task_type: {original_task_type or task_type}",
                    "parameters": parameters,
                },
            }

        missing_fields = validate_required_fields(task_type, parameters)
        if missing_fields:
            return {
                **base,
                "execution_status": "needs_clarification",
                "details": {
                    "code": "MISSING_REQUIRED_FIELDS",
                    "message": "Task parameters are missing required fields.",
                    "missing_fields": missing_fields,
                    "parameters": parameters,
                },
            }

        if not settings.enable_local_execution:
            return {
                **base,
                "execution_status": "handoff_required",
                "details": {
                    "code": "LOCAL_EXECUTION_DISABLED",
                    "message": "Local execution is disabled in configuration.",
                    "executor": task.executor_name,
                    "parameters": parameters,
                    "success_condition": success_condition,
                },
            }

        if task_type == "file_write":
            raw_path = str(parameters.get("path") or "").strip()
            content = str(parameters.get("content") or "")
            if not raw_path:
                return {
                    **base,
                    "execution_status": "needs_clarification",
                    "details": {
                        "code": "MISSING_REQUIRED_FIELDS",
                        "message": "Task parameters are missing required fields.",
                        "missing_fields": ["path"],
                        "parameters": parameters,
                    },
                }

            target = Path(raw_path)
            if not target.is_absolute():
                target = (settings.repo_path / target).resolve()
            else:
                target = target.resolve()

            try:
                target.relative_to(settings.repo_path.resolve())
            except Exception:
                return {
                    **base,
                    "execution_status": "failed",
                    "details": {
                        "code": "PATH_OUTSIDE_WORKSPACE",
                        "message": "file_write path must stay inside repository workspace.",
                        "path": str(target),
                    },
                }

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            return {
                **base,
                "execution_status": "success",
                "verified": True,
                "details": {
                    "executor": task.executor_name,
                    "path": str(target),
                    "bytes_written": len(content.encode("utf-8")),
                    "sha256": digest,
                    "success_condition": success_condition,
                },
            }

        if task_type in {"scheduled_weather_report", "reminder"}:
            handoff_dir = settings.repo_path / settings.execution_dir / "handoff"
            handoff_dir.mkdir(parents=True, exist_ok=True)
            job_id = str(job.get("job_id") or "job_unknown")
            handoff_path = handoff_dir / f"{job_id}.json"
            handoff_payload = {
                "job_id": job_id,
                "task_type": task_type,
                "parameters": parameters,
                "success_condition": success_condition,
                "created_at": base["executed_at"],
                "status": "pending_runtime_executor",
            }
            handoff_path.write_text(json.dumps(handoff_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return {
                **base,
                "execution_status": "handoff_required",
                "details": {
                    "executor": task.executor_name,
                    "message": "Task requires runtime-specific executor not available in API worker.",
                    "handoff_path": str(handoff_path),
                    "parameters": parameters,
                    "success_condition": success_condition,
                },
            }

        return {
            **base,
            "execution_status": "failed",
            "details": {
                "code": "NO_EXECUTOR_IMPLEMENTED",
                "message": f"No executor implemented for task_type: {task_type}",
                "parameters": parameters,
            },
        }

    def _normalize_weather_schedule_parameters(self, parameters: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = dict(parameters or {})

        schedule_obj = normalized.get("schedule")
        schedule_dict = schedule_obj if isinstance(schedule_obj, dict) else {}

        cron_expr = str(
            normalized.get("cron_schedule")
            or normalized.get("cron")
            or normalized.get("cron_expression")
            or schedule_dict.get("cron_expression")
            or ""
        ).strip()

        schedule_text = str(
            normalized.get("schedule")
            if isinstance(normalized.get("schedule"), str)
            else normalized.get("target_time_reference")
            or ""
        ).strip()

        timezone_value = self._first_nonempty(
            normalized.get("timezone"),
            normalized.get("time_zone"),
            normalized.get("recipient_timezone"),
            normalized.get("time_of_delivery_timezone"),
            normalized.get("timezone_policy"),
            schedule_dict.get("timezone"),
        )
        if timezone_value:
            normalized["timezone"] = timezone_value

        location_value = self._first_nonempty(
            normalized.get("location"),
            normalized.get("city"),
            normalized.get("recipient_location"),
            schedule_dict.get("location"),
        )
        if not location_value:
            command_text = str(normalized.get("command") or "").strip()
            cmd_match = re.search(r"weather(?:_update)?\s+([A-Za-z][A-Za-z\s,\-\.]+)$", command_text, flags=re.IGNORECASE)
            if cmd_match:
                location_value = cmd_match.group(1).strip()
        if location_value:
            normalized["location"] = location_value

        frequency_value = self._first_nonempty(
            normalized.get("frequency"),
            normalized.get("date"),
            normalized.get("cadence"),
            normalized.get("interval"),
            schedule_dict.get("frequency"),
            schedule_dict.get("cadence"),
        )
        if not frequency_value and cron_expr:
            frequency_value = "daily"
        if frequency_value:
            normalized["frequency"] = frequency_value

        time_value = self._first_nonempty(
            normalized.get("time"),
            normalized.get("send_time"),
            normalized.get("time_of_day"),
            normalized.get("time_of_delivery"),
            schedule_dict.get("time"),
        )
        if not time_value:
            m = re.search(r"\b(\d{1,2}:\d{2})\b", schedule_text)
            if m:
                time_value = m.group(1)
        if not time_value and cron_expr:
            parts = cron_expr.split()
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                time_value = f"{int(parts[1]):02d}:{int(parts[0]):02d}"
        if time_value:
            normalized["time"] = time_value

        return normalized

    @staticmethod
    def _first_nonempty(*values: Any) -> str:
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    def _verify_local_task(self, job: dict[str, Any], execution_result: dict[str, Any]) -> dict[str, Any]:
        execution_status = str(execution_result.get("execution_status") or "").lower()
        details = execution_result.get("details") if isinstance(execution_result.get("details"), dict) else {}

        if execution_status == "success" and str(execution_result.get("task_type") or "") == "file_write":
            path = details.get("path")
            expected_hash = str(details.get("sha256") or "")
            if isinstance(path, str) and path.strip() and expected_hash:
                target = Path(path)
                if target.exists():
                    actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
                    return {
                        "verified": actual_hash == expected_hash,
                        "method": "file_hash",
                        "details": {
                            "path": str(target),
                            "expected_sha256": expected_hash,
                            "actual_sha256": actual_hash,
                        },
                    }

        if execution_status == "handoff_required":
            handoff_path = details.get("handoff_path")
            if isinstance(handoff_path, str) and handoff_path.strip():
                exists = Path(handoff_path).exists()
                return {
                    "verified": exists,
                    "method": "handoff_artifact",
                    "details": {
                        "handoff_path": handoff_path,
                        "exists": exists,
                    },
                }

        return {
            "verified": bool(execution_result.get("verified", False)),
            "method": "execution_flag",
        }

    def _run_final_phraser_stage(
        self,
        job: dict[str, Any],
        planner_result: dict[str, Any],
        execution_result: dict[str, Any],
        verification_result: dict[str, Any],
    ) -> dict[str, Any]:
        result = self._run_stage_via_pipeline(job["job_id"], stage_name="final_phraser")
        normalized = self._normalize_response_payload(result, router_result=None)
        normalized["execution"] = execution_result
        normalized["verification"] = verification_result
        normalized["intent_type"] = "job"
        normalized["task_type"] = planner_result.get("task_type")
        normalized["current_stage"] = "completed"
        normalized["stages"] = {
            "router": "complete",
            "planner": "complete",
            "execution": "complete",
            "verification": "complete",
            "final_phraser": "complete",
        }
        return normalized

    def _stage_timeout_seconds(self, stage_name: str) -> int:
        mapping = {
            "router": settings.router_stage_timeout_seconds,
            "planner": settings.planner_stage_timeout_seconds,
            "answerer": settings.answerer_stage_timeout_seconds,
            "final_phraser": settings.final_phraser_stage_timeout_seconds,
            "execution": settings.execution_stage_timeout_seconds,
            "verification": settings.verification_stage_timeout_seconds,
        }
        return int(mapping.get(stage_name, settings.job_timeout_seconds))

    def _run_stage_via_pipeline(self, job_id: str, stage_name: str) -> dict[str, Any]:
        timeout_seconds = self._stage_timeout_seconds(stage_name)

        if hasattr(wait_for_stage_result, "__call__"):
            return wait_for_stage_result(job_id, stage_name=stage_name, timeout_seconds=timeout_seconds)

        return wait_for_result(job_id, timeout_seconds=timeout_seconds)

    def _normalize_response_payload(self, raw: dict[str, Any], router_result: dict[str, Any] | None) -> dict[str, Any]:
        if isinstance(raw, dict) and "message" in raw:
            return raw
        content = ""
        if isinstance(raw, dict):
            if "response" in raw:
                content = str(raw.get("response") or "")
            elif "content" in raw:
                content = str(raw.get("content") or "")
            else:
                content = json.dumps(raw, ensure_ascii=False)
        else:
            content = str(raw)
        return {
            "message": {"role": "assistant", "content": content},
            "intent_type": (router_result or {}).get("intent_type"),
            "task_type": (router_result or {}).get("task_type"),
            "current_stage": "completed",
        }

    def _recover_or_fail_git_error(self, job_id: str, exc: Exception) -> None:
        try:
            with REPO_LOCK:
                result = try_read_result(job_id)
            db.mark_completed(job_id, result)
            logger.info("job recovered after git error", extra={"job_id": job_id, "status": "completed"})
        except JobFailedError as failed_exc:
            db.mark_failed(job_id, normalize_failure_payload(failed_exc.payload), status="failed")
            logger.exception(
                "job failed from parsed pipeline artifact after git error",
                extra={"job_id": job_id, "status": "failed"},
            )
        except Exception:
            db.mark_failed(job_id, {"code": "GIT_ERROR", "message": str(exc)}, status="failed")
            logger.exception("job failed from git error", extra={"job_id": job_id, "status": "failed"})


worker = JobWorker()
