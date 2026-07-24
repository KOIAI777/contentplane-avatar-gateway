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


def test_store_claims_only_one_running_job(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.initialize()
    store.create(make_job("first", tmp_path))
    store.create(make_job("second", tmp_path))

    first = store.claim_next_queued()
    assert first is not None
    assert first.id == "first"
    assert first.status == JobStatus.RUNNING
    assert store.claim_next_queued() is None

    store.complete(first.id, tmp_path / "first-result.mp4", "done")
    second = store.claim_next_queued()
    assert second is not None
    assert second.id == "second"


def test_store_cancels_queued_job_without_claiming_it(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.initialize()
    store.create(make_job("queued", tmp_path))

    canceled = store.request_cancel("queued")

    assert canceled is not None
    assert canceled.status == JobStatus.CANCELED
    assert store.claim_next_queued() is None
