from pathlib import Path

from app.models import AvatarResult, GenerationOptions, JobRecord, JobStatus
from app.store import JobStore, utc_now
from app.worker import JobWorker


class SuccessfulAdapter:
    def generate(self, job, report, should_cancel):  # type: ignore[no-untyped-def]
        assert should_cancel() is False
        report(type("Update", (), {"message": "generating", "logs": "worker log"})())
        provider_result = job.template_video_path.parent / "provider.mp4"
        provider_result.write_bytes(b"generated video")
        return AvatarResult(source_path=provider_result, provider_result_url="https://worker/result.mp4")


class ExternallyCompletedAdapter:
    def __init__(self, store: JobStore, output: Path):
        self.store = store
        self.output = output

    def generate(self, job, report, should_cancel):  # type: ignore[no-untyped-def]
        self.output.write_bytes(b"reconciled video")
        self.store.complete(job.id, self.output, "reconciled")
        provider_result = job.template_video_path.parent / "provider-after-reconcile.mp4"
        provider_result.write_bytes(b"late provider video")
        return AvatarResult(source_path=provider_result)


def test_worker_completes_one_queued_job(tmp_path: Path) -> None:
    uploads = tmp_path / "uploads" / "job"
    uploads.mkdir(parents=True)
    template = uploads / "template.mp4"
    audio = uploads / "voice.mp3"
    template.write_bytes(b"video")
    audio.write_bytes(b"audio")

    store = JobStore(tmp_path / "jobs.sqlite3")
    store.initialize()
    now = utc_now()
    store.create(
        JobRecord(
            id="job",
            status=JobStatus.QUEUED,
            template_video_path=template,
            driving_audio_path=audio,
            output_path=None,
            client_ref="project",
            submitted_by="operator",
            options=GenerationOptions(),
            message="queued",
            logs="",
            error=None,
            cancel_requested=False,
            created_at=now,
            updated_at=now,
            started_at=None,
            finished_at=None,
        )
    )
    worker = JobWorker(store, SuccessfulAdapter(), tmp_path / "outputs")

    assert worker.run_once() is True

    completed = store.get("job")
    assert completed is not None
    assert completed.status == JobStatus.SUCCEEDED
    assert completed.logs == "worker log"
    assert completed.output_path is not None
    assert completed.output_path.read_bytes() == b"generated video"


def test_worker_does_not_overwrite_externally_reconciled_success(tmp_path: Path) -> None:
    uploads = tmp_path / "uploads" / "reconciled"
    uploads.mkdir(parents=True)
    template = uploads / "template.mp4"
    audio = uploads / "voice.mp3"
    template.write_bytes(b"video")
    audio.write_bytes(b"audio")

    store = JobStore(tmp_path / "jobs.sqlite3")
    store.initialize()
    now = utc_now()
    store.create(
        JobRecord(
            id="reconciled",
            status=JobStatus.QUEUED,
            template_video_path=template,
            driving_audio_path=audio,
            output_path=None,
            client_ref=None,
            submitted_by=None,
            options=GenerationOptions(),
            message="queued",
            logs="",
            error=None,
            cancel_requested=False,
            created_at=now,
            updated_at=now,
            started_at=None,
            finished_at=None,
        )
    )
    reconciled_output = tmp_path / "outputs" / "reconciled.mp4"
    reconciled_output.parent.mkdir(parents=True)
    worker = JobWorker(store, ExternallyCompletedAdapter(store, reconciled_output), tmp_path / "outputs")

    assert worker.run_once() is True

    completed = store.get("reconciled")
    assert completed is not None
    assert completed.status == JobStatus.SUCCEEDED
    assert completed.message == "reconciled"
    assert completed.output_path == reconciled_output
    assert completed.output_path.read_bytes() == b"reconciled video"
