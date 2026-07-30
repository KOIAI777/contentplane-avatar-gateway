from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from .adapter import GenerationCanceled
from .config import Settings, WorkerEndpoint
from .models import AvatarResult, JobRecord, ProgressUpdate


class GradioHeyGemAdapter:
    """Adapter for HeyGem's public Gradio `process_single` endpoint."""

    def __init__(
        self,
        settings: Settings,
        endpoint: WorkerEndpoint,
        client_factory: Callable[[str], Any] | None = None,
        file_factory: Callable[[str], Any] | None = None,
        downloader: Callable[[str, Path], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._settings = settings
        self._endpoint = endpoint
        self._client_factory = client_factory
        self._file_factory = file_factory
        self._downloader = downloader or self._download_video
        self._sleep = sleep
        self._clock = clock
        self._client: Any = None

    @property
    def worker_id(self) -> str:
        return self._endpoint.id

    def generate(
        self,
        job: JobRecord,
        report: Callable[[ProgressUpdate], None],
        should_cancel: Callable[[], bool],
    ) -> AvatarResult:
        if should_cancel():
            raise GenerationCanceled()

        client = self._get_client()
        report(ProgressUpdate(message=f"Submitting task to HeyGem worker {self.worker_id}."))
        remote_job = client.submit(
            self._handle_file(str(job.driving_audio_path)),
            {"video": self._handle_file(str(job.template_video_path))},
            api_name="/process_single",
        )

        deadline = self._clock() + self._settings.task_timeout_seconds
        while self._clock() < deadline:
            if should_cancel():
                self._cancel(remote_job)
                raise GenerationCanceled()
            if remote_job.done():
                break
            report(ProgressUpdate(message=self._status_message(remote_job)))
            self._sleep(self._settings.poll_interval_seconds)
        else:
            self._cancel(remote_job)
            raise TimeoutError(f"HeyGem worker {self.worker_id} did not finish before the task timeout")

        if should_cancel():
            self._cancel(remote_job)
            raise GenerationCanceled()

        result = remote_job.result()
        reference = self._video_reference(result)
        if not reference:
            raise RuntimeError(f"HeyGem worker {self.worker_id} returned no video")

        target = job.driving_audio_path.parent / "provider-result.mp4"
        self._copy_generated_video(reference, target)
        report(ProgressUpdate(message=f"HeyGem worker {self.worker_id} completed the task."))
        return AvatarResult(source_path=target, provider_result_url=reference)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory:
            self._client = self._client_factory(self._endpoint.url)
            return self._client
        try:
            from gradio_client import Client
        except ImportError as error:
            raise RuntimeError("gradio_client is not installed. Run uv sync before starting the gateway.") from error
        self._client = Client(self._endpoint.url)
        return self._client

    def _handle_file(self, path: str) -> Any:
        if self._file_factory:
            return self._file_factory(path)
        try:
            from gradio_client import handle_file
        except ImportError as error:
            raise RuntimeError("gradio_client is not installed. Run uv sync before starting the gateway.") from error
        return handle_file(path)

    def _status_message(self, remote_job: Any) -> str:
        try:
            status = remote_job.status()
        except Exception:  # noqa: BLE001 - status polling must not abort an active generation.
            return f"HeyGem worker {self.worker_id} is generating the video."
        code = getattr(status, "code", None)
        code_value = getattr(code, "value", code)
        rank = getattr(status, "rank", None)
        queue_size = getattr(status, "queue_size", None)
        parts = [f"HeyGem worker {self.worker_id}"]
        if code_value:
            parts.append(str(code_value))
        if isinstance(rank, int) and rank >= 0:
            parts.append(f"queue position {rank + 1}")
        if isinstance(queue_size, int) and queue_size >= 0:
            parts.append(f"queue size {queue_size}")
        return " · ".join(parts)[:2000]

    @staticmethod
    def _cancel(remote_job: Any) -> None:
        try:
            remote_job.cancel()
        except Exception:  # noqa: BLE001 - provider cancellation is best effort.
            return

    @classmethod
    def _video_reference(cls, value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                reference = cls._video_reference(item)
                if reference:
                    return reference
            return None
        if not isinstance(value, dict):
            return None
        for key in ("video", "path", "url", "name"):
            item = value.get(key)
            reference = cls._video_reference(item)
            if reference:
                return reference
        return None

    def _copy_generated_video(self, reference: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        local_path = Path(reference)
        if not reference.startswith(("http://", "https://")) and local_path.is_file():
            shutil.copyfile(local_path, target)
        else:
            self._downloader(self._provider_url(reference), target)
        if not target.is_file() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"HeyGem worker {self.worker_id} returned an empty video file")

    def _provider_url(self, reference: str) -> str:
        if reference.startswith(("http://", "https://")):
            return reference
        if reference.startswith("/"):
            return f"{self._endpoint.url}{reference}"
        return f"{self._endpoint.url}/gradio_api/file={quote(reference, safe='/')}"

    @staticmethod
    def _download_video(url: str, target: Path) -> None:
        request = Request(url, headers={"User-Agent": "ContentPlane-Avatar-Gateway/0.2"})
        with urlopen(request, timeout=10 * 60) as response, target.open("wb") as destination:
            shutil.copyfileobj(response, destination)
