from pathlib import Path

import pytest

from app.adapter import GenerationCanceled
from app.config import Settings, WorkerEndpoint
from app.heygem_adapter import GradioHeyGemAdapter
from app.models import GenerationOptions, JobRecord, JobStatus
from app.store import utc_now


class CompletedRemoteJob:
    def __init__(self, result: object):
        self._result = result
        self.canceled = False

    def done(self) -> bool:
        return True

    def result(self) -> object:
        return self._result

    def cancel(self) -> None:
        self.canceled = True


class PendingRemoteJob(CompletedRemoteJob):
    def done(self) -> bool:
        return False


class FakeClient:
    def __init__(self, remote_job: CompletedRemoteJob):
        self.remote_job = remote_job
        self.submissions: list[tuple[object, object, str]] = []

    def submit(self, audio: object, video: object, api_name: str) -> CompletedRemoteJob:
        self.submissions.append((audio, video, api_name))
        return self.remote_job


def make_job(tmp_path: Path) -> JobRecord:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    template = job_dir / "template.mp4"
    audio = job_dir / "driving-audio.mp3"
    template.write_bytes(b"template")
    audio.write_bytes(b"audio")
    now = utc_now()
    return JobRecord(
        id="job-1",
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
        worker_id="gpu-0",
    )


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        api_token="secret",
        worker_endpoints=(WorkerEndpoint("gpu-0", "http://127.0.0.1:7860"),),
        poll_interval_seconds=0,
    )


def test_heygem_adapter_submits_process_single_and_copies_result(tmp_path: Path) -> None:
    source = tmp_path / "heygem-result.mp4"
    source.write_bytes(b"generated")
    remote_job = CompletedRemoteJob({"video": {"path": str(source)}})
    client = FakeClient(remote_job)
    messages: list[str] = []
    adapter = GradioHeyGemAdapter(
        make_settings(tmp_path),
        WorkerEndpoint("gpu-0", "http://127.0.0.1:7860"),
        client_factory=lambda _: client,
        file_factory=lambda path: f"file:{path}",
    )
    job = make_job(tmp_path)

    result = adapter.generate(job, lambda update: messages.append(update.message), lambda: False)

    assert client.submissions == [
        (
            f"file:{job.driving_audio_path}",
            {"video": f"file:{job.template_video_path}"},
            "/process_single",
        )
    ]
    assert result.source_path == job.driving_audio_path.parent / "provider-result.mp4"
    assert result.source_path.read_bytes() == b"generated"
    assert messages[-1] == "HeyGem worker gpu-0 completed the task."


def test_heygem_adapter_cancels_remote_job(tmp_path: Path) -> None:
    remote_job = PendingRemoteJob(None)
    client = FakeClient(remote_job)
    adapter = GradioHeyGemAdapter(
        make_settings(tmp_path),
        WorkerEndpoint("gpu-0", "http://127.0.0.1:7860"),
        client_factory=lambda _: client,
        file_factory=lambda path: path,
    )
    cancel_checks = iter((False, True))

    with pytest.raises(GenerationCanceled):
        adapter.generate(make_job(tmp_path), lambda _: None, lambda: next(cancel_checks))

    assert remote_job.canceled is True
