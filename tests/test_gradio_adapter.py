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
    assert submit["cam_ctrl"] is True
    assert submit["pose_stabilize"] is True
    assert downloaded == ["https://worker/new.mp4"]
    assert result.source_path.read_bytes() == b"result"
