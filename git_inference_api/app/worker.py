from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
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
        ensure_repo_ready()
        while not self._stop_event.is_set():
            job = db.next_queued_job()
            if not job:
                self._notify_event.wait(timeout=settings.worker_poll_interval_seconds)
                self._notify_event.clear()
                continue
            self._process_job(job)

    def _process_job(self, job: dict[str, Any]) -> None:
        job_id = job["job_id"]
        routing = self._extract_routing_metadata(job)
        intent_type = routing.get("intent_type")
        task_type = routing.get("task_type")

        try:
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

    def _apply_runtime_handoff_if_configured(self, job_id: str, result: dict[str, Any]) -> dict[str, Any]:
        if settings.enable_runtime_handoff_executor:
            logger.warning(
                "runtime handoff executor is deprecated and ignored; cron/reminder tasks must execute inside OpenClaw runtime"
            )
        return result

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
