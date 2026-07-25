from __future__ import annotations

import json
import logging
import re
import shutil
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .config import Settings
from .models import AvatarResult, JobRecord, ProgressUpdate

logger = logging.getLogger(__name__)

PROMPT_ID_PATTERN = re.compile(
    r"\bPrompt\s+ID\s*[:：]\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


class GenerationCanceled(Exception):
    """Raised when a task was canceled while the provider was processing it."""


class AvatarAdapter(Protocol):
    def generate(
        self,
        job: JobRecord,
        report: Callable[[ProgressUpdate], None],
        should_cancel: Callable[[], bool],
    ) -> AvatarResult: ...


class GradioInfiniteTalkAdapter:
    """Adapter for the public InfiniteTalk Gradio API exposed by the current image."""

    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[[str], Any] | None = None,
        file_factory: Callable[[str], Any] | None = None,
        downloader: Callable[[str, Path], None] | None = None,
        json_fetcher: Callable[[str], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._settings = settings
        self._client_factory = client_factory
        self._file_factory = file_factory
        self._downloader = downloader or self._download_video
        self._json_fetcher = json_fetcher or self._fetch_json
        self._sleep = sleep
        self._clock = clock
        self._client: Any = None

    def generate(
        self,
        job: JobRecord,
        report: Callable[[ProgressUpdate], None],
        should_cancel: Callable[[], bool],
    ) -> AvatarResult:
        client = self._get_client()
        if should_cancel():
            raise GenerationCanceled()

        before = self._poll(client)
        baseline = self._gallery_references(before[0])
        report(ProgressUpdate(message="Submitting task to InfiniteTalk."))

        options = job.options
        submission = client.predict(
            mode="视频数字人",
            person_img=None,
            ref_vid={"video": self._handle_file(str(job.template_video_path))},
            audio1=self._handle_file(str(job.driving_audio_path)),
            audio2=None,
            pos=options.positive_prompt,
            neg=options.negative_prompt,
            w=options.width,
            h=options.height,
            st=options.steps,
            bl=options.blocks_to_swap,
            frame_size=options.frame_window,
            seed=options.seed,
            hd_enabled=options.hd_enabled,
            hd_res=options.hd_resolution,
            fps=options.fps,
            cam_ctrl=options.camera_control,
            pose_stabilize=options.pose_stabilize,
            api_name="/add_to_queue_wrapper",
        )
        submitted_message = self._value_at(submission, 0) or "Task accepted by InfiniteTalk."
        submitted_queue = self._value_at(submission, 1)
        report(ProgressUpdate(message=self._join_status(submitted_message, submitted_queue)))
        prompt_id = self._extract_prompt_id(self._join_status(submitted_message, submitted_queue))

        deadline = self._clock() + self._settings.task_timeout_seconds
        while self._clock() < deadline:
            if should_cancel():
                self._cancel_provider_task(client)
                raise GenerationCanceled()

            snapshot = self._poll(client)
            gallery, status, queue_status, resources, logs = snapshot
            latest_prompt_id = self._extract_prompt_id(self._join_status(status, queue_status, resources, logs))
            if latest_prompt_id:
                prompt_id = latest_prompt_id
            report(
                ProgressUpdate(
                    message=self._join_status(status, queue_status, resources),
                    logs=logs,
                )
            )

            if self._looks_canceled(status):
                raise GenerationCanceled()
            if self._looks_failed(status):
                raise RuntimeError(status)

            generated = self._find_comfyui_video(prompt_id)
            if generated:
                target = job.template_video_path.parent / "provider-result.mp4"
                self._copy_generated_video(generated, target)
                return AvatarResult(source_path=target, provider_result_url=generated)

            generated = self._find_new_video(gallery, baseline)
            if generated:
                target = job.template_video_path.parent / "provider-result.mp4"
                self._copy_generated_video(generated, target)
                return AvatarResult(source_path=target, provider_result_url=generated)

            self._sleep(self._settings.poll_interval_seconds)

        raise TimeoutError("InfiniteTalk did not produce a video before the configured task timeout")

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory:
            self._client = self._client_factory(self._settings.gradio_url)
            return self._client

        try:
            from gradio_client import Client
        except ImportError as error:
            raise RuntimeError("gradio_client is not installed. Run uv sync before starting the gateway.") from error
        self._client = Client(self._settings.gradio_url)
        return self._client

    def _handle_file(self, path: str) -> Any:
        if self._file_factory:
            return self._file_factory(path)
        try:
            from gradio_client import handle_file
        except ImportError as error:
            raise RuntimeError("gradio_client is not installed. Run uv sync before starting the gateway.") from error
        return handle_file(path)

    @staticmethod
    def _poll(client: Any) -> tuple[Any, str, str, str, str]:
        response = client.predict(api_name="/check_and_get_video")
        gallery = GradioInfiniteTalkAdapter._item_at(response, 0)
        return (
            gallery if isinstance(gallery, list) else [],
            GradioInfiniteTalkAdapter._value_at(response, 1),
            GradioInfiniteTalkAdapter._value_at(response, 2),
            GradioInfiniteTalkAdapter._value_at(response, 3),
            GradioInfiniteTalkAdapter._value_at(response, 4),
        )

    @staticmethod
    def _value_at(value: Any, index: int) -> str:
        item = GradioInfiniteTalkAdapter._item_at(value, index)
        return item if isinstance(item, str) else ""

    @staticmethod
    def _item_at(value: Any, index: int) -> Any:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) <= index:
            return None
        return value[index]

    @staticmethod
    def _join_status(*parts: str) -> str:
        return "\n".join(part.strip() for part in parts if part and part.strip())[:2000] or "Waiting for InfiniteTalk."

    @staticmethod
    def _gallery_references(gallery: list[Any]) -> set[str]:
        references: set[str] = set()
        for item in gallery:
            reference = GradioInfiniteTalkAdapter._gallery_video_reference(item)
            if reference:
                references.add(reference)
        return references

    @staticmethod
    def _find_new_video(gallery: list[Any], baseline: set[str]) -> str | None:
        for item in reversed(gallery):
            reference = GradioInfiniteTalkAdapter._gallery_video_reference(item)
            if reference and reference not in baseline:
                return reference
        return None

    @staticmethod
    def _gallery_video_reference(item: Any) -> str | None:
        if isinstance(item, str):
            return item
        if not isinstance(item, dict):
            return None
        video = item.get("video") or item.get("image") or item
        if isinstance(video, str):
            return video
        if not isinstance(video, dict):
            return None
        for key in ("url", "path"):
            value = video.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _extract_prompt_id(value: str) -> str | None:
        matches = PROMPT_ID_PATTERN.findall(value)
        return matches[-1] if matches else None

    def _find_comfyui_video(self, prompt_id: str | None) -> str | None:
        if not prompt_id or not self._settings.comfyui_url:
            return None
        try:
            history = self._json_fetcher(f"{self._settings.comfyui_url}/history/{quote(prompt_id, safe='')}")
        except Exception as error:  # noqa: BLE001 - polling must tolerate transient ComfyUI failures.
            logger.warning("ComfyUI history request for prompt %s failed: %s", prompt_id, error)
            return None

        reference = self._comfyui_output_reference(history, prompt_id)
        if not reference:
            return None
        query = urlencode(
            {
                "filename": reference["filename"],
                "subfolder": reference.get("subfolder", ""),
                "type": reference.get("type", "output"),
            }
        )
        return f"{self._settings.comfyui_url}/view?{query}"

    @staticmethod
    def _comfyui_output_reference(history: Any, prompt_id: str) -> dict[str, str] | None:
        if not isinstance(history, dict):
            return None
        prompt = history.get(prompt_id)
        if not isinstance(prompt, dict):
            return None
        outputs = prompt.get("outputs")
        if not isinstance(outputs, dict):
            return None
        for output in outputs.values():
            if not isinstance(output, dict):
                continue
            for item in reversed(output.get("gifs", [])):
                if (
                    isinstance(item, dict)
                    and item.get("type") == "output"
                    and isinstance(item.get("filename"), str)
                    and item["filename"]
                ):
                    return item
        return None

    @staticmethod
    def _looks_failed(status: str) -> bool:
        normalized = status.lower()
        return any(marker in normalized for marker in ("failed", "failure", "error", "exception", "失败", "错误"))

    @staticmethod
    def _looks_canceled(status: str) -> bool:
        normalized = status.lower()
        return any(marker in normalized for marker in ("cancelled", "canceled", "已取消", "取消"))

    @staticmethod
    def _cancel_provider_task(client: Any) -> None:
        try:
            client.predict(api_name="/interrupt_current_task")
        except Exception as error:  # noqa: BLE001 - Gradio adapters may raise transport-specific exceptions.
            logger.warning("InfiniteTalk cancellation request failed: %s", error)

    def _copy_generated_video(self, reference: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        local_path = Path(reference)
        if not reference.startswith(("http://", "https://")) and local_path.is_file():
            shutil.copyfile(local_path, target)
            return

        if reference.startswith(("http://", "https://")):
            url = reference
        elif reference.startswith("/gradio_api/"):
            url = f"{self._settings.gradio_url}{reference}"
        else:
            url = self._provider_file_url(reference)
        self._downloader(url, target)

    def _provider_file_url(self, provider_path: str) -> str:
        return f"{self._settings.gradio_url}/gradio_api/file={quote(provider_path, safe='/')}"

    @staticmethod
    def _download_video(url: str, target: Path) -> None:
        request = Request(url, headers={"User-Agent": "ContentPlane-Avatar-Gateway/0.1"})
        with urlopen(request, timeout=10 * 60) as response, target.open("wb") as destination:
            shutil.copyfileobj(response, destination)
        if target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise RuntimeError("InfiniteTalk returned an empty video file")

    @staticmethod
    def _fetch_json(url: str) -> Any:
        request = Request(url, headers={"User-Agent": "ContentPlane-Avatar-Gateway/0.1"})
        with urlopen(request, timeout=30) as response:
            return json.load(response)
