from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path

from .gradio_adapter import AvatarAdapter, GenerationCanceled
from .models import JobStatus, ProgressUpdate
from .store import JobStore

logger = logging.getLogger(__name__)


class JobWorker:
    def __init__(self, store: JobStore, adapter: AvatarAdapter, outputs_dir: Path):
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
        self._thread = threading.Thread(target=self._run, name="avatar-job-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout_seconds: float = 10.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout=timeout_seconds)

    def wake(self) -> None:
        self._wake_event.set()

    def run_once(self) -> bool:
        job = self._store.claim_next_queued()
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
