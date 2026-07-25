from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import GenerationOptions, JobRecord, JobStatus


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, database_path: Path):
        self._database_path = database_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS avatar_jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    template_video_path TEXT NOT NULL,
                    driving_audio_path TEXT NOT NULL,
                    output_path TEXT,
                    client_ref TEXT,
                    submitted_by TEXT,
                    options_json TEXT NOT NULL,
                    message TEXT NOT NULL,
                    logs TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS avatar_jobs_status_created_idx ON avatar_jobs(status, created_at)"
            )

    def recover_after_restart(self) -> int:
        now = utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE avatar_jobs
                SET status = ?, message = ?, updated_at = ?, finished_at = ?
                WHERE status = ?
                """,
                (
                    JobStatus.INTERRUPTED.value,
                    "Gateway restarted while this task was running. Submit it again to avoid duplicate generation.",
                    now,
                    now,
                    JobStatus.RUNNING.value,
                ),
            )
            return cursor.rowcount

    def create(self, job: JobRecord) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO avatar_jobs (
                    id, status, template_video_path, driving_audio_path, output_path,
                    client_ref, submitted_by, options_json, message, logs, error,
                    cancel_requested, created_at, updated_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.status.value,
                    str(job.template_video_path),
                    str(job.driving_audio_path),
                    str(job.output_path) if job.output_path else None,
                    job.client_ref,
                    job.submitted_by,
                    job.options.model_dump_json(),
                    job.message,
                    job.logs,
                    job.error,
                    int(job.cancel_requested),
                    job.created_at,
                    job.updated_at,
                    job.started_at,
                    job.finished_at,
                ),
            )

    def get(self, job_id: str) -> JobRecord | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM avatar_jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self, limit: int = 100) -> list[JobRecord]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM avatar_jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_job(row) for row in rows]

    def queue_position(self, job_id: str) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT created_at, status FROM avatar_jobs WHERE id = ?", (job_id,)).fetchone()
            if not row or row["status"] != JobStatus.QUEUED.value:
                return 0
            count = connection.execute(
                "SELECT COUNT(*) FROM avatar_jobs WHERE status = ? AND created_at <= ?",
                (JobStatus.QUEUED.value, row["created_at"]),
            ).fetchone()[0]
        return int(count)

    def claim_next_queued(self) -> JobRecord | None:
        now = utc_now()
        with self._lock:
            connection = self._open_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                active = connection.execute(
                    "SELECT 1 FROM avatar_jobs WHERE status = ? LIMIT 1",
                    (JobStatus.RUNNING.value,),
                ).fetchone()
                if active:
                    connection.commit()
                    return None
                row = connection.execute(
                    "SELECT * FROM avatar_jobs WHERE status = ? ORDER BY created_at ASC LIMIT 1",
                    (JobStatus.QUEUED.value,),
                ).fetchone()
                if not row:
                    connection.commit()
                    return None
                connection.execute(
                    """
                    UPDATE avatar_jobs
                    SET status = ?, message = ?, updated_at = ?, started_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        JobStatus.RUNNING.value,
                        "Submitting task to the avatar worker.",
                        now,
                        now,
                        row["id"],
                        JobStatus.QUEUED.value,
                    ),
                )
                connection.commit()
                claimed = connection.execute("SELECT * FROM avatar_jobs WHERE id = ?", (row["id"],)).fetchone()
                return self._row_to_job(claimed) if claimed else None
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def update_progress(self, job_id: str, message: str, logs: str = "") -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE avatar_jobs SET message = ?, logs = ?, updated_at = ? WHERE id = ? AND status = ?",
                (message[:2000], logs[-12000:], utc_now(), job_id, JobStatus.RUNNING.value),
            )

    def complete(self, job_id: str, output_path: Path, message: str) -> None:
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE avatar_jobs
                SET status = ?, output_path = ?, message = ?, error = NULL,
                    cancel_requested = 0, updated_at = ?, finished_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.SUCCEEDED.value,
                    str(output_path),
                    message,
                    now,
                    now,
                    job_id,
                    JobStatus.RUNNING.value,
                ),
            )

    def fail(self, job_id: str, error: str) -> None:
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE avatar_jobs
                SET status = ?, error = ?, message = ?, updated_at = ?, finished_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.FAILED.value,
                    error[:4000],
                    "Avatar generation failed.",
                    now,
                    now,
                    job_id,
                    JobStatus.RUNNING.value,
                ),
            )

    def mark_canceled(self, job_id: str, message: str = "Task canceled.") -> None:
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE avatar_jobs
                SET status = ?, message = ?, updated_at = ?, finished_at = ?, cancel_requested = 1
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    JobStatus.CANCELED.value,
                    message,
                    now,
                    now,
                    job_id,
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                ),
            )

    def request_cancel(self, job_id: str) -> JobRecord | None:
        job = self.get(job_id)
        if not job:
            return None
        if job.status == JobStatus.QUEUED:
            self.mark_canceled(job_id, "Queued task canceled before execution.")
        elif job.status == JobStatus.RUNNING:
            with self._connection() as connection:
                connection.execute(
                    """
                    UPDATE avatar_jobs
                    SET cancel_requested = 1, message = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    ("Cancellation requested. Waiting for the avatar worker.", utc_now(), job_id),
                )
        return self.get(job_id)

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute("SELECT cancel_requested FROM avatar_jobs WHERE id = ?", (job_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def _connection(self) -> _LockedConnection:
        with self._lock:
            connection = self._open_connection()
        return _LockedConnection(connection, self._lock)

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._database_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            status=JobStatus(row["status"]),
            template_video_path=Path(row["template_video_path"]),
            driving_audio_path=Path(row["driving_audio_path"]),
            output_path=Path(row["output_path"]) if row["output_path"] else None,
            client_ref=row["client_ref"],
            submitted_by=row["submitted_by"],
            options=GenerationOptions.model_validate(json.loads(row["options_json"])),
            message=row["message"],
            logs=row["logs"],
            error=row["error"],
            cancel_requested=bool(row["cancel_requested"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )


class _LockedConnection:
    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock):
        self._connection = connection
        self._lock = lock

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        return self._connection

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self._connection.close()
            self._lock.release()
