from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
import subprocess
import threading
import time
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
    commit_and_push_request,
    ensure_repo_ready,
    normalize_failure_payload,
    sync_repo_to_remote_head,
    try_read_result,
    wait_for_result,
    wait_for_stage_result,
    write_request_artifact,
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

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, name="job-worker-v2", daemon=True)
        self._thread.start()
        logger.info("worker started")

    def stop(self) -> None:
        self._stop_event.set()
        self._notify_event.set()
        if self._thread:
            self._thread.join(timeout=2)
        logger.info("worker stopped")

    def notify(self) -> None:
        self._notify_event.set()

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
            if self._is_allsequential_model(request_model):
                self._process_job_allsequential(job, intent_type=intent_type, task_type=task_type)
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
                self._update_status(job_id, "failed", intent_type=intent_type, task_type=task_type)
                db.mark_failed(
                    job_id,
                    {
                        "code": "RESEARCH_NOT_IMPLEMENTED",
                        "message": "Research workflow is not implemented in worker.",
                    },
                    status="failed",
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
            request_payload = job.get("request_json") if isinstance(job.get("request_json"), dict) else {}
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

    def _resolve_allsequential_targets(self) -> list[str]:
        targets: list[str] = []
        for model_name in settings.all_sequential_models():
            normalized = str(model_name or "").strip()
            if not normalized:
                continue
            if self._is_allsequential_model(normalized):
                continue
            if normalized not in targets:
                targets.append(normalized)
        return targets

    @staticmethod
    def _sanitize_model_tail(model_name: str) -> str:
        tail = str(model_name or "").strip().split("/")[-1]
        return re.sub(r"[^a-zA-Z0-9_-]+", "-", tail)[:40] or "model"

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

    def _run_one_shot_request(self, job_id: str, request_payload: dict[str, Any]) -> dict[str, Any]:
        with REPO_LOCK:
            sync_repo_to_remote_head()
            request_path = write_request_artifact(job_id, request_payload)
            commit_and_push_request(job_id, request_path)

        result = wait_for_result(job_id, timeout_seconds=settings.job_timeout_seconds)
        return self._apply_runtime_handoff_if_configured(job_id=job_id, result=result)

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

            child_payload = json.loads(json.dumps(request_payload))
            child_payload["model"] = target_model
            child_payload["allsequential_parent_job_id"] = job_id
            child_payload["allsequential_index"] = idx
            child_payload["allsequential_total"] = len(targets)

            try:
                raw_result = self._run_one_shot_request(child_job_id, child_payload)
                normalized_result = self._normalize_response_payload(
                    raw_result,
                    router_result={"intent_type": "question", "task_type": "information"},
                )
                message = normalized_result.get("message") if isinstance(normalized_result, dict) else None
                content = ""
                if isinstance(message, dict):
                    content = str(message.get("content") or "").strip()
                if not content:
                    content = str(raw_result or "").strip()

                aggregated_results.append(
                    {
                        "index": idx,
                        "model": target_model,
                        "job_id": child_job_id,
                        "status": "completed",
                        "content": content,
                    }
                )
                success_count += 1
            except Exception as exc:
                aggregated_results.append(
                    {
                        "index": idx,
                        "model": target_model,
                        "job_id": child_job_id,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
                logger.warning(
                    "allsequential child failed",
                    extra={"parent_job_id": job_id, "child_job_id": child_job_id, "model": target_model, "error": str(exc)},
                )

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

    def _allsequential_virtual_turns_enabled(self) -> bool:
        if not settings.allsequential_virtual_turns_enabled:
            return False
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
        kickoff_content = (
            f"Running this prompt across {len(targets)} sources now. "
            "I will send each source result as a separate follow-up message."
        )
        kickoff_execution = {
            "mode": "allsequential_virtual_turns",
            "targets": targets,
            "results": [],
            "source_messages": [],
            "success_count": 0,
            "failure_count": 0,
            "delivery_errors": [],
            "stage": "virtual_turns_in_progress",
        }
        kickoff_payload = {
            "message": {"role": "assistant", "content": kickoff_content},
            "intent_type": "question",
            "task_type": "allsequential",
            "current_stage": "virtual_turns_in_progress",
            "execution": kickoff_execution,
            "stages": {"allsequential": "virtual_turns_in_progress"},
            "done": True,
        }
        db.mark_completed(
            job_id,
            kickoff_payload,
            execution_json=kickoff_execution,
            stages_json={"allsequential": "virtual_turns_in_progress"},
        )
        logger.info(
            "allsequential virtual turns started",
            extra={"job_id": job_id, "status": "completed", "total": len(targets)},
        )

        aggregated_results: list[dict[str, Any]] = []
        delivery_errors: list[dict[str, Any]] = []
        success_count = 0

        for idx, target_model in enumerate(targets, start=1):
            child_job_id = f"{job_id}_allseq_{idx:02d}_{self._sanitize_model_tail(target_model)}"
            child_payload = json.loads(json.dumps(request_payload))
            child_payload["model"] = target_model
            child_payload["allsequential_parent_job_id"] = job_id
            child_payload["allsequential_index"] = idx
            child_payload["allsequential_total"] = len(targets)

            item: dict[str, Any]
            try:
                raw_result = self._run_one_shot_request(child_job_id, child_payload)
                normalized_result = self._normalize_response_payload(
                    raw_result,
                    router_result={"intent_type": "question", "task_type": "information"},
                )
                message = normalized_result.get("message") if isinstance(normalized_result, dict) else None
                content = ""
                if isinstance(message, dict):
                    content = str(message.get("content") or "").strip()
                if not content:
                    content = str(raw_result or "").strip()

                item = {
                    "index": idx,
                    "model": target_model,
                    "job_id": child_job_id,
                    "status": "completed",
                    "content": content,
                }
                success_count += 1
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
            if item_status != "completed" and not settings.allsequential_virtual_turns_send_failures:
                continue

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

        final_source_messages = self._build_allsequential_source_messages(aggregated_results)
        final_content = self._format_allsequential_response(
            base_prompt=base_prompt,
            results=aggregated_results,
            source_messages=final_source_messages,
        )
        execution_meta = {
            "mode": "allsequential_virtual_turns",
            "targets": targets,
            "results": aggregated_results,
            "source_messages": final_source_messages,
            "success_count": success_count,
            "failure_count": len(aggregated_results) - success_count,
            "delivery_errors": delivery_errors,
            "stage": "virtual_turns_complete",
        }
        final_payload = {
            "message": {"role": "assistant", "content": kickoff_content},
            "intent_type": "question",
            "task_type": "allsequential",
            "current_stage": "completed",
            "execution": execution_meta,
            "stages": {"allsequential": "virtual_turns_complete"},
            "source_messages": final_source_messages,
            "allsequential_summary": final_content,
            "done": True,
        }
        db.mark_completed(
            job_id,
            final_payload,
            execution_json=execution_meta,
            stages_json={"allsequential": "virtual_turns_complete"},
        )
        logger.info(
            "allsequential virtual turns completed",
            extra={
                "job_id": job_id,
                "status": "completed",
                "success_count": success_count,
                "total": len(aggregated_results),
                "delivery_failures": len(delivery_errors),
            },
        )

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
            request_payload = job.get("request_json") if isinstance(job.get("request_json"), dict) else {}
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
