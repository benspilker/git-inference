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
STATUS_VALUES = {"queued", "running", "completed", "failed", "expired"}


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
                error_json TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at ON jobs(status, created_at, job_id)"
        )


def requeue_inflight_jobs() -> None:
    with DB_LOCK, connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'queued', started_at = NULL WHERE status = 'running'"
        )


def create_job(idempotency_key: str, request_hash: str, request_json: dict[str, Any]) -> str:
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    with DB_LOCK, connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (job_id, idempotency_key, request_hash, status, created_at, request_json)
            VALUES (?, ?, ?, 'queued', ?, ?)
            """,
            (job_id, idempotency_key, request_hash, utcnow_iso(), json.dumps(request_json, sort_keys=True)),
        )
    return job_id


def get_job_by_idempotency_key(idempotency_key: str) -> dict[str, Any] | None:
    with DB_LOCK, connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
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


def mark_running(job_id: str) -> None:
    with DB_LOCK, connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', started_at = ? WHERE job_id = ?",
            (utcnow_iso(), job_id),
        )


def mark_completed(job_id: str, response_json: dict[str, Any]) -> None:
    with DB_LOCK, connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'completed', completed_at = ?, response_json = ? WHERE job_id = ?",
            (utcnow_iso(), json.dumps(response_json, sort_keys=True), job_id),
        )


def mark_failed(job_id: str, error_json: dict[str, Any], status: str = "failed") -> None:
    if status not in {"failed", "expired"}:
        raise ValueError(f"invalid terminal status: {status}")
    with DB_LOCK, connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, completed_at = ?, error_json = ? WHERE job_id = ?",
            (status, utcnow_iso(), json.dumps(error_json, sort_keys=True), job_id),
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
    with DB_LOCK, connect() as conn:
        row = conn.execute(
            "SELECT job_id FROM jobs WHERE status = 'running' ORDER BY started_at ASC, job_id ASC LIMIT 1"
        ).fetchone()
    return str(row["job_id"]) if row else None


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key in ("request_json", "response_json", "error_json"):
        if data.get(key):
            data[key] = json.loads(data[key])
    return data
