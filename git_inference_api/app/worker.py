from __future__ import annotations

import logging
import threading
from typing import Any

from . import db
from .config import settings
from .git_ops import (
    GitError,
    JobFailedError,
    JobTimedOutError,
    REPO_LOCK,
    commit_and_push_request,
    ensure_repo_ready,
    normalize_failure_payload,
    sync_repo_to_remote_head,
    try_read_result,
    wait_for_result,
    write_request_artifact,
)

logger = logging.getLogger("git_inference_api.worker")


class JobWorker:
    def __init__(self) -> None:
        self._notify_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, name="job-worker", daemon=True)
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
        db.mark_running(job_id)
        logger.info("job marked running", extra={"job_id": job_id, "status": "running"})

        try:
            with REPO_LOCK:
                sync_repo_to_remote_head()
                request_path = write_request_artifact(job_id, job["request_json"])
                commit_and_push_request(job_id, request_path)

            result = wait_for_result(job_id, timeout_seconds=settings.job_timeout_seconds)
            db.mark_completed(job_id, result)
            logger.info("job completed", extra={"job_id": job_id, "status": "completed"})
        except JobFailedError as exc:
            db.mark_failed(job_id, normalize_failure_payload(exc.payload), status="failed")
            logger.exception("job failed from pipeline", extra={"job_id": job_id, "status": "failed"})
        except JobTimedOutError as exc:
            db.mark_failed(
                job_id,
                {
                    "code": "JOB_TIMEOUT",
                    "message": str(exc),
                },
                status="expired",
            )
            logger.exception("job expired", extra={"job_id": job_id, "status": "expired"})
        except GitError as exc:
            try:
                with REPO_LOCK:
                    result = try_read_result(job_id)
                db.mark_completed(job_id, result)
                logger.info("job recovered after git error", extra={"job_id": job_id, "status": "completed"})
                return
            except JobFailedError as failed_exc:
                db.mark_failed(job_id, normalize_failure_payload(failed_exc.payload), status="failed")
                logger.exception(
                    "job failed from parsed pipeline artifact after git error",
                    extra={"job_id": job_id, "status": "failed"},
                )
            except Exception:
                db.mark_failed(
                    job_id,
                    {
                        "code": "GIT_ERROR",
                        "message": str(exc),
                    },
                    status="failed",
                )
                logger.exception("job failed from git error", extra={"job_id": job_id, "status": "failed"})
        except Exception as exc:  # pragma: no cover
            db.mark_failed(
                job_id,
                {
                    "code": "UNEXPECTED_ERROR",
                    "message": str(exc),
                },
                status="failed",
            )
            logger.exception("job failed unexpectedly", extra={"job_id": job_id, "status": "failed"})
        finally:
            self._notify_event.set()


worker = JobWorker()
