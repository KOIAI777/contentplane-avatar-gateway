from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Iterable
from pathlib import Path

from .adapter import AvatarAdapter, GenerationCanceled
from .models import JobStatus, ProgressUpdate
from .store import JobStore

logger = logging.getLogger(__name__)


class JobWorker:
    def __init__(self, worker_id: str, store: JobStore, adapter: AvatarAdapter, outputs_dir: Path):
        self.worker_id = worker_id
        self._store = store
        self._adapter = adapter
        self._outputs_dir = outputs_dir
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._outputs_dir.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"avatar-job-worker-{self.worker_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_seconds: float = 10.0) -> None:
        self.request_stop()
        self.join(timeout_seconds=timeout_seconds)

    def request_stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def join(self, timeout_seconds: float = 10.0) -> None:
        if self._thread:
            self._thread.join(timeout=timeout_seconds)

    def wake(self) -> None:
        self._wake_event.set()

    def run_once(self) -> bool:
        job = self._store.claim_next_queued(self.worker_id)
        if not job:
            return False

        try:
            result = self._adapter.generate(
                job,
                report=lambda update: self._report_progress(job.id, update),
                should_cancel=lambda: self._store.is_cancel_requested(job.id) or self._stop_event.is_set(),
            )
            current = self._store.get(job.id)
            if current and current.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
                return True
            if self._store.is_cancel_requested(job.id):
                self._store.mark_canceled(job.id)
                return True

            output_path = self._outputs_dir / f"{job.id}.mp4"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(result.source_path), str(output_path))
            self._store.complete(job.id, output_path, "Avatar video generated successfully.")
        except GenerationCanceled:
            self._store.mark_canceled(job.id)
        except Exception as error:
            logger.exception("Avatar job %s failed", job.id)
            self._store.fail(job.id, str(error) or error.__class__.__name__)
        return True

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self.run_once():
                continue
            self._wake_event.wait(timeout=2.0)
            self._wake_event.clear()

    def _report_progress(self, job_id: str, update: ProgressUpdate) -> None:
        self._store.update_progress(job_id, update.message, update.logs)


class JobWorkerPool:
    def __init__(self, workers: Iterable[JobWorker]):
        self._workers = tuple(workers)
        if not self._workers:
            raise ValueError("At least one avatar worker is required")
        worker_ids = [worker.worker_id for worker in self._workers]
        if len(worker_ids) != len(set(worker_ids)):
            raise ValueError("Avatar worker IDs must be unique")

    @property
    def worker_ids(self) -> tuple[str, ...]:
        return tuple(worker.worker_id for worker in self._workers)

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    def start(self) -> None:
        for worker in self._workers:
            worker.start()

    def stop(self, timeout_seconds: float = 10.0) -> None:
        for worker in self._workers:
            worker.request_stop()
        for worker in self._workers:
            worker.join(timeout_seconds=timeout_seconds)

    def wake(self) -> None:
        for worker in self._workers:
            worker.wake()
