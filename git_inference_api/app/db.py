from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from .config import settings

DB_LOCK = threading.Lock()

STATUS_VALUES = {
    "queued",
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

ACTIVE_STATUS_VALUES = {
    "routing",
    "planning",
    "executing",
    "verifying",
}

REQUEUEABLE_STATUS_VALUES = {
    "routing",
    "planning",
    "executing",
    "verifying",
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@contextmanager
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    settings.ensure_directories()
    with DB_LOCK, connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                request_json TEXT NOT NULL,
                response_json TEXT,
                error_json TEXT,
                intent_type TEXT,
                task_type TEXT,
                current_stage TEXT,
                execution_json TEXT,
                stages_json TEXT,
                requires_local_execution INTEGER,
                success_condition TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at ON jobs(status, created_at, job_id)"
        )
        _ensure_column(conn, "jobs", "intent_type", "TEXT")
        _ensure_column(conn, "jobs", "task_type", "TEXT")
        _ensure_column(conn, "jobs", "current_stage", "TEXT")
        _ensure_column(conn, "jobs", "execution_json", "TEXT")
        _ensure_column(conn, "jobs", "stages_json", "TEXT")
        _ensure_column(conn, "jobs", "requires_local_execution", "INTEGER")
        _ensure_column(conn, "jobs", "success_condition", "TEXT")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, sql_type: str) -> None:
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    col_names = {str(row["name"]) for row in cols}
    if column not in col_names:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def recover_inflight_jobs(max_age_seconds: int | None = None) -> dict[str, Any]:
    threshold = settings.stale_inflight_max_age_seconds if max_age_seconds is None else int(max_age_seconds)
    threshold = max(0, threshold)
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    placeholders = ",".join("?" for _ in REQUEUEABLE_STATUS_VALUES)
    values = list(REQUEUEABLE_STATUS_VALUES)

    expired_jobs: list[str] = []
    requeued_jobs: list[str] = []

    with DB_LOCK, connect() as conn:
        rows = conn.execute(
            f"""
            SELECT job_id, status, started_at, created_at
            FROM jobs
            WHERE status IN ({placeholders})
            """,
            values,
        ).fetchall()

        for row in rows:
            job_id = str(row["job_id"])
            started_at = _parse_iso_timestamp(row["started_at"])
            created_at = _parse_iso_timestamp(row["created_at"])
            reference_time = started_at or created_at
            age_seconds = (now_dt - reference_time).total_seconds() if reference_time else None

            if age_seconds is not None and age_seconds >= threshold:
                error_json = json.dumps(
                    {
                        "code": "STALE_INFLIGHT_JOB",
                        "message": f"Recovered stale in-flight job on startup (age_seconds={int(age_seconds)}).",
                    },
                    sort_keys=True,
                )
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'expired',
                        current_stage = 'expired',
                        completed_at = ?,
                        error_json = ?
                    WHERE job_id = ?
                    """,
                    (now_iso, error_json, job_id),
                )
                expired_jobs.append(job_id)
                continue

            conn.execute(
                """
                UPDATE jobs
                SET status = 'queued',
                    current_stage = 'queued',
                    started_at = NULL
                WHERE job_id = ?
                """,
                (job_id,),
            )
            requeued_jobs.append(job_id)

    return {
        "expired": len(expired_jobs),
        "requeued": len(requeued_jobs),
        "threshold_seconds": threshold,
        "expired_job_ids": expired_jobs,
        "requeued_job_ids": requeued_jobs,
    }


def requeue_inflight_jobs() -> None:
    # Compatibility wrapper for existing callers.
    recover_inflight_jobs(max_age_seconds=10**9)


def purge_inflight_jobs(
    include_queued: bool = True,
    terminal_status: str = "failed",
    reason: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if terminal_status not in {"failed", "expired"}:
        raise ValueError("terminal_status must be 'failed' or 'expired'")

    target_statuses = list(ACTIVE_STATUS_VALUES)
    if include_queued:
        target_statuses.append("queued")

    placeholders = ",".join("?" for _ in target_statuses)
    now_iso = utcnow_iso()
    details_reason = reason or "manual purge"

    with DB_LOCK, connect() as conn:
        rows = conn.execute(
            f"SELECT job_id, status FROM jobs WHERE status IN ({placeholders}) ORDER BY created_at ASC",
            target_statuses,
        ).fetchall()
        job_ids = [str(row["job_id"]) for row in rows]

        if dry_run or not job_ids:
            return {
                "dry_run": dry_run,
                "purged": len(job_ids),
                "target_statuses": target_statuses,
                "terminal_status": terminal_status,
                "job_ids": job_ids,
            }

        for row in rows:
            job_id = str(row["job_id"])
            prior_status = str(row["status"])
            error_json = json.dumps(
                {
                    "code": "QUEUE_PURGED",
                    "message": f"Manually purged job from {prior_status} to {terminal_status}.",
                    "reason": details_reason,
                },
                sort_keys=True,
            )
            conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    current_stage = ?,
                    completed_at = ?,
                    error_json = ?
                WHERE job_id = ?
                """,
                (terminal_status, terminal_status, now_iso, error_json, job_id),
            )

    return {
        "dry_run": dry_run,
        "purged": len(job_ids),
        "target_statuses": target_statuses,
        "terminal_status": terminal_status,
        "job_ids": job_ids,
    }


def create_job(idempotency_key: str, request_hash: str, request_json: dict[str, Any]) -> str:
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    routing = request_json.get("routing_metadata") if isinstance(request_json, dict) else None
    intent_type = routing.get("intent_type") if isinstance(routing, dict) else None
    task_type = routing.get("task_type") if isinstance(routing, dict) else None
    requires_local_execution = routing.get("requires_local_execution") if isinstance(routing, dict) else None
    success_condition = routing.get("success_condition") if isinstance(routing, dict) else None

    with DB_LOCK, connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, idempotency_key, request_hash, status, created_at, request_json,
                intent_type, task_type, current_stage, requires_local_execution, success_condition
            )
            VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                job_id,
                idempotency_key,
                request_hash,
                utcnow_iso(),
                json.dumps(request_json, sort_keys=True),
                intent_type,
                task_type,
                int(bool(requires_local_execution)) if requires_local_execution is not None else None,
                success_condition,
            ),
        )
    return job_id


def get_job_by_idempotency_key(idempotency_key: str) -> dict[str, Any] | None:
    with DB_LOCK, connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
    return row_to_dict(row)


def get_job(job_id: str) -> dict[str, Any] | None:
    with DB_LOCK, connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return row_to_dict(row)


def next_queued_job() -> dict[str, Any] | None:
    with DB_LOCK, connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC, job_id ASC LIMIT 1"
        ).fetchone()
    return row_to_dict(row)


def update_job_status(
    job_id: str,
    status: str,
    intent_type: str | None = None,
    task_type: str | None = None,
    current_stage: str | None = None,
) -> None:
    if status not in STATUS_VALUES:
        raise ValueError(f"invalid status: {status}")
    started_at = utcnow_iso() if status in ACTIVE_STATUS_VALUES else None
    with DB_LOCK, connect() as conn:
        if started_at is not None:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    started_at = COALESCE(started_at, ?),
                    intent_type = COALESCE(?, intent_type),
                    task_type = COALESCE(?, task_type),
                    current_stage = COALESCE(?, ?)
                WHERE job_id = ?
                """,
                (status, started_at, intent_type, task_type, current_stage, status, job_id),
            )
        else:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    intent_type = COALESCE(?, intent_type),
                    task_type = COALESCE(?, task_type),
                    current_stage = COALESCE(?, ?)
                WHERE job_id = ?
                """,
                (status, intent_type, task_type, current_stage, status, job_id),
            )


def set_routing_metadata(
    job_id: str,
    intent_type: str | None = None,
    task_type: str | None = None,
    current_stage: str | None = None,
    requires_local_execution: bool | None = None,
    success_condition: str | None = None,
) -> None:
    with DB_LOCK, connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET intent_type = COALESCE(?, intent_type),
                task_type = COALESCE(?, task_type),
                current_stage = COALESCE(?, current_stage),
                requires_local_execution = COALESCE(?, requires_local_execution),
                success_condition = COALESCE(?, success_condition)
            WHERE job_id = ?
            """,
            (
                intent_type,
                task_type,
                current_stage,
                int(bool(requires_local_execution)) if requires_local_execution is not None else None,
                success_condition,
                job_id,
            ),
        )


def mark_running(job_id: str) -> None:
    # Compatibility wrapper for v1 callers.
    update_job_status(job_id, status="routing", current_stage="routing")


def save_execution_result(job_id: str, execution_json: dict[str, Any]) -> None:
    with DB_LOCK, connect() as conn:
        conn.execute(
            "UPDATE jobs SET execution_json = ? WHERE job_id = ?",
            (json.dumps(execution_json, sort_keys=True), job_id),
        )


def save_stage_metadata(job_id: str, stages_json: dict[str, Any]) -> None:
    with DB_LOCK, connect() as conn:
        conn.execute(
            "UPDATE jobs SET stages_json = ? WHERE job_id = ?",
            (json.dumps(stages_json, sort_keys=True), job_id),
        )


def mark_needs_clarification(job_id: str, response_json: dict[str, Any]) -> None:
    with DB_LOCK, connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'needs_clarification',
                response_json = ?,
                current_stage = 'needs_clarification'
            WHERE job_id = ?
            """,
            (json.dumps(response_json, sort_keys=True), job_id),
        )


def mark_completed(
    job_id: str,
    response_json: dict[str, Any],
    execution_json: dict[str, Any] | None = None,
    stages_json: dict[str, Any] | None = None,
) -> None:
    with DB_LOCK, connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'completed',
                completed_at = ?,
                response_json = ?,
                execution_json = COALESCE(?, execution_json),
                stages_json = COALESCE(?, stages_json),
                current_stage = 'completed'
            WHERE job_id = ?
            """,
            (
                utcnow_iso(),
                json.dumps(response_json, sort_keys=True),
                json.dumps(execution_json, sort_keys=True) if execution_json is not None else None,
                json.dumps(stages_json, sort_keys=True) if stages_json is not None else None,
                job_id,
            ),
        )


def mark_failed(job_id: str, error_json: dict[str, Any], status: str = "failed") -> None:
    if status not in {"failed", "expired"}:
        raise ValueError(f"invalid terminal status: {status}")
    with DB_LOCK, connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?,
                completed_at = ?,
                error_json = ?,
                current_stage = ?
            WHERE job_id = ?
            """,
            (status, utcnow_iso(), json.dumps(error_json, sort_keys=True), status, job_id),
        )


def count_queue_position(job_id: str) -> int | None:
    job = get_job(job_id)
    if not job or job["status"] != "queued":
        return None
    with DB_LOCK, connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM jobs
            WHERE status = 'queued'
              AND (created_at < ? OR (created_at = ? AND job_id <= ?))
            """,
            (job["created_at"], job["created_at"], job_id),
        ).fetchone()
    return int(row["n"]) if row else None


def get_active_job_id() -> str | None:
    placeholders = ",".join("?" for _ in ACTIVE_STATUS_VALUES)
    with DB_LOCK, connect() as conn:
        row = conn.execute(
            f"SELECT job_id FROM jobs WHERE status IN ({placeholders}) ORDER BY started_at ASC, job_id ASC LIMIT 1",
            list(ACTIVE_STATUS_VALUES),
        ).fetchone()
    return str(row["job_id"]) if row else None


def list_allsequential_virtual_turns_in_progress_jobs(limit: int = 50) -> list[dict[str, Any]]:
    safe_limit = max(1, int(limit))
    with DB_LOCK, connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE status = 'completed'
              AND response_json IS NOT NULL
            ORDER BY completed_at DESC, job_id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()

    matches: list[dict[str, Any]] = []
    for row in rows:
        data = row_to_dict(row)
        if not isinstance(data, dict):
            continue
        response_json = data.get("response_json")
        execution_json = data.get("execution_json")
        if not isinstance(response_json, dict) or not isinstance(execution_json, dict):
            continue
        if str(execution_json.get("mode") or "").strip().lower() != "allsequential_virtual_turns":
            continue
        stage = str(execution_json.get("stage") or "").strip().lower()
        if stage != "virtual_turns_in_progress":
            continue
        matches.append(data)
    return matches


def list_allparallel_virtual_turns_in_progress_jobs(limit: int = 50) -> list[dict[str, Any]]:
    safe_limit = max(1, int(limit))
    with DB_LOCK, connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE status = 'completed'
              AND response_json IS NOT NULL
            ORDER BY completed_at DESC, job_id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()

    matches: list[dict[str, Any]] = []
    for row in rows:
        data = row_to_dict(row)
        if not isinstance(data, dict):
            continue
        response_json = data.get("response_json")
        execution_json = data.get("execution_json")
        if not isinstance(response_json, dict) or not isinstance(execution_json, dict):
            continue
        if str(execution_json.get("mode") or "").strip().lower() != "allparallel_virtual_turns":
            continue
        stage = str(execution_json.get("stage") or "").strip().lower()
        if stage != "virtual_turns_in_progress":
            continue
        matches.append(data)
    return matches


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key in ("request_json", "response_json", "error_json", "execution_json", "stages_json"):
        if data.get(key):
            data[key] = json.loads(data[key])
    if "requires_local_execution" in data and data["requires_local_execution"] is not None:
        data["requires_local_execution"] = bool(data["requires_local_execution"])
    return data
