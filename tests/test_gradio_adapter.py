from pathlib import Path
from typing import Any

from app.config import Settings
from app.gradio_adapter import GradioInfiniteTalkAdapter
from app.models import GenerationOptions, JobRecord, JobStatus
from app.store import utc_now


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.poll_count = 0

    def predict(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        api_name = kwargs["api_name"]
        if api_name == "/add_to_queue_wrapper":
            return ("accepted", "queue: 1")
        if api_name == "/check_and_get_video":
            self.poll_count += 1
            if self.poll_count == 1:
                return ([{"video": {"url": "https://worker/old.mp4"}}], "idle", "queue: 0", "gpu", "")
            return (
                [
                    {"video": {"url": "https://worker/old.mp4"}},
                    {"video": {"url": "https://worker/new.mp4"}},
                ],
                "completed",
                "queue: 0",
                "gpu",
                "done",
            )
        raise AssertionError(f"unexpected endpoint: {api_name}")


class ConsumedGalleryClient:
    prompt_id = "0378d235-c28b-4aac-9ac2-540242959f62"

    def __init__(self) -> None:
        self.poll_count = 0

    def predict(self, **kwargs: Any) -> Any:
        api_name = kwargs["api_name"]
        if api_name == "/add_to_queue_wrapper":
            return ("accepted", "queue: 1")
        if api_name == "/check_and_get_video":
            self.poll_count += 1
            if self.poll_count == 1:
                return ([], "idle", "queue: 0", "gpu", "")
            return (
                {"__type__": "update"},
                "生成中",
                "queue: 0",
                "gpu",
                f"任务已提交! Prompt ID: {self.prompt_id}",
            )
        raise AssertionError(f"unexpected endpoint: {api_name}")


def test_adapter_submits_video_mode_and_downloads_new_gallery_item(tmp_path: Path) -> None:
    template = tmp_path / "template.mp4"
    audio = tmp_path / "audio.mp3"
    template.write_bytes(b"video")
    audio.write_bytes(b"audio")
    client = FakeClient()
    downloaded: list[str] = []
    settings = Settings(tmp_path, "token", "https://worker", poll_interval_seconds=0, task_timeout_seconds=10)
    adapter = GradioInfiniteTalkAdapter(
        settings,
        client_factory=lambda _: client,
        file_factory=lambda path: f"file:{path}",
        downloader=lambda url, target: (downloaded.append(url), target.write_bytes(b"result")),
        sleep=lambda _: None,
    )
    now = utc_now()
    job = JobRecord(
        id="job",
        status=JobStatus.RUNNING,
        template_video_path=template,
        driving_audio_path=audio,
        output_path=None,
        client_ref=None,
        submitted_by=None,
        options=GenerationOptions(),
        message="running",
        logs="",
        error=None,
        cancel_requested=False,
        created_at=now,
        updated_at=now,
        started_at=now,
        finished_at=None,
    )

    result = adapter.generate(job, report=lambda _: None, should_cancel=lambda: False)

    submit = next(call for call in client.calls if call["api_name"] == "/add_to_queue_wrapper")
    assert submit["mode"] == "视频数字人"
    assert submit["person_img"] is None
    assert submit["audio2"] is None
    assert submit["st"] == 4
    assert submit["bl"] == 40
    assert submit["frame_size"] == 61
    assert submit["hd_enabled"] is False
    assert submit["hd_res"] == 1080
    assert submit["cam_ctrl"] is True
    assert submit["pose_stabilize"] is True
    assert downloaded == ["https://worker/new.mp4"]
    assert result.source_path.read_bytes() == b"result"


def test_adapter_uses_comfyui_history_when_gradio_gallery_was_consumed(tmp_path: Path) -> None:
    template = tmp_path / "template.mp4"
    audio = tmp_path / "audio.mp3"
    template.write_bytes(b"video")
    audio.write_bytes(b"audio")
    client = ConsumedGalleryClient()
    fetched: list[str] = []
    downloaded: list[str] = []
    settings = Settings(
        tmp_path,
        "token",
        "https://worker",
        comfyui_url="http://127.0.0.1:8188",
        poll_interval_seconds=0,
        task_timeout_seconds=10,
    )

    def fetch_json(url: str) -> Any:
        fetched.append(url)
        return {
            client.prompt_id: {
                "outputs": {
                    "121": {
                        "gifs": [
                            {
                                "filename": "InfiniteTalk_00005-audio.mp4",
                                "subfolder": "",
                                "type": "output",
                            }
                        ]
                    }
                }
            }
        }

    adapter = GradioInfiniteTalkAdapter(
        settings,
        client_factory=lambda _: client,
        file_factory=lambda path: f"file:{path}",
        downloader=lambda url, target: (downloaded.append(url), target.write_bytes(b"result")),
        json_fetcher=fetch_json,
        sleep=lambda _: None,
    )
    now = utc_now()
    job = JobRecord(
        id="job",
        status=JobStatus.RUNNING,
        template_video_path=template,
        driving_audio_path=audio,
        output_path=None,
        client_ref=None,
        submitted_by=None,
        options=GenerationOptions(),
        message="running",
        logs="",
        error=None,
        cancel_requested=False,
        created_at=now,
        updated_at=now,
        started_at=now,
        finished_at=None,
    )

    result = adapter.generate(job, report=lambda _: None, should_cancel=lambda: False)

    assert fetched == [f"http://127.0.0.1:8188/history/{client.prompt_id}"]
    assert downloaded == ["http://127.0.0.1:8188/view?filename=InfiniteTalk_00005-audio.mp4&subfolder=&type=output"]
    assert result.source_path.read_bytes() == b"result"


def test_adapter_uses_latest_prompt_id_from_cumulative_gradio_logs(tmp_path: Path) -> None:
    old_prompt_id = "0378d235-c28b-4aac-9ac2-540242959f62"
    current_prompt_id = "ad248357-9ff4-4589-ae93-4a35875c7211"

    class CumulativeLogsClient:
        def __init__(self) -> None:
            self.poll_count = 0

        def predict(self, **kwargs: Any) -> Any:
            if kwargs["api_name"] == "/add_to_queue_wrapper":
                return ("accepted", "queue: 1")
            self.poll_count += 1
            logs = f"previous Prompt ID: {old_prompt_id}"
            if self.poll_count > 1:
                logs += f"\ncurrent Prompt ID: {current_prompt_id}"
            return ([], "generating", "queue: 0", "gpu", logs)

    template = tmp_path / "template.mp4"
    audio = tmp_path / "audio.mp3"
    template.write_bytes(b"video")
    audio.write_bytes(b"audio")
    client = CumulativeLogsClient()
    fetched: list[str] = []
    settings = Settings(
        tmp_path,
        "token",
        "https://worker",
        comfyui_url="http://127.0.0.1:8188",
        poll_interval_seconds=0,
        task_timeout_seconds=10,
    )

    def fetch_json(url: str) -> Any:
        fetched.append(url)
        prompt_id = url.rsplit("/", 1)[-1]
        if prompt_id != current_prompt_id:
            return {}
        return {
            current_prompt_id: {
                "outputs": {
                    "131": {
                        "gifs": [{"filename": "InfiniteTalk-current.mp4", "subfolder": "", "type": "output"}]
                    }
                }
            }
        }

    adapter = GradioInfiniteTalkAdapter(
        settings,
        client_factory=lambda _: client,
        file_factory=lambda path: f"file:{path}",
        downloader=lambda _url, target: target.write_bytes(b"current result"),
        json_fetcher=fetch_json,
        sleep=lambda _: None,
    )
    now = utc_now()
    job = JobRecord(
        id="job",
        status=JobStatus.RUNNING,
        template_video_path=template,
        driving_audio_path=audio,
        output_path=None,
        client_ref=None,
        submitted_by=None,
        options=GenerationOptions(),
        message="running",
        logs="",
        error=None,
        cancel_requested=False,
        created_at=now,
        updated_at=now,
        started_at=now,
        finished_at=None,
    )

    result = adapter.generate(job, report=lambda _: None, should_cancel=lambda: False)

    assert fetched[-1].endswith(current_prompt_id)
    assert result.source_path.read_bytes() == b"current result"
