from pathlib import Path

from app.models import GenerationOptions, JobRecord, JobStatus
from app.store import JobStore, utc_now


def make_job(job_id: str, root: Path) -> JobRecord:
    now = utc_now()
    return JobRecord(
        id=job_id,
        status=JobStatus.QUEUED,
        template_video_path=root / f"{job_id}.mp4",
        driving_audio_path=root / f"{job_id}.mp3",
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


def test_store_assigns_different_queued_jobs_to_two_workers(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.initialize()
    store.create(make_job("first", tmp_path))
    store.create(make_job("second", tmp_path))

    first = store.claim_next_queued("gpu-0")
    assert first is not None
    assert first.id == "first"
    assert first.status == JobStatus.RUNNING
    assert first.worker_id == "gpu-0"

    second = store.claim_next_queued("gpu-1")
    assert second is not None
    assert second.id == "second"
    assert second.status == JobStatus.RUNNING
    assert second.worker_id == "gpu-1"
    assert store.claim_next_queued("gpu-2") is None


def test_store_cancels_queued_job_without_claiming_it(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.initialize()
    store.create(make_job("queued", tmp_path))

    canceled = store.request_cancel("queued")

    assert canceled is not None
    assert canceled.status == JobStatus.CANCELED
    assert store.claim_next_queued("gpu-0") is None


def test_late_worker_updates_do_not_overwrite_succeeded_job(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.initialize()
    store.create(make_job("completed", tmp_path))
    claimed = store.claim_next_queued("gpu-0")
    assert claimed is not None

    output = tmp_path / "completed-result.mp4"
    output.write_bytes(b"video")
    store.complete(claimed.id, output, "done")
    store.update_progress(claimed.id, "late progress", "late logs")
    store.mark_canceled(claimed.id)
    store.fail(claimed.id, "late failure")

    completed = store.get(claimed.id)
    assert completed is not None
    assert completed.status == JobStatus.SUCCEEDED
    assert completed.message == "done"
    assert completed.output_path == output
    assert completed.cancel_requested is False
    assert completed.error is None
