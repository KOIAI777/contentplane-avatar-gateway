from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings, WorkerEndpoint


class UnusedAdapter:
    def generate(self, job, report, should_cancel):  # type: ignore[no-untyped-def]
        raise AssertionError("worker is disabled in API tests")


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values = {
        "data_dir": tmp_path,
        "api_token": "secret",
        "worker_endpoints": (WorkerEndpoint("test-worker", "https://worker"),),
        **overrides,
    }
    return Settings(**values)  # type: ignore[arg-type]


def test_task_api_requires_auth_and_persists_uploads(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings=settings, adapter=UnusedAdapter(), start_worker=False)

    with TestClient(app) as client:
        unauthorized = client.get("/v1/avatar/tasks")
        assert unauthorized.status_code == 401

        response = client.post(
            "/v1/avatar/tasks",
            headers={"Authorization": "Bearer secret"},
            files={
                "template_video": ("template.mp4", b"video", "video/mp4"),
                "driving_audio": ("voice.mp3", b"audio", "audio/mpeg"),
            },
            data={
                "client_ref": "project-123",
                "submitted_by": "operator-a",
                "options": '{"steps": 4, "camera_control": true, "hd_enabled": true, "hd_resolution": 1080}',
            },
        )
        assert response.status_code == 202
        created = response.json()
        assert created["status"] == "queued"
        assert created["position"] == 1

        detail = client.get(
            f"/v1/avatar/tasks/{created['id']}",
            headers={"Authorization": "Bearer secret"},
        )
        assert detail.status_code == 200
        assert detail.json()["client_ref"] == "project-123"
        assert detail.json()["submitted_by"] == "operator-a"

        canceled = client.post(
            f"/v1/avatar/tasks/{created['id']}/cancel",
            headers={"Authorization": "Bearer secret"},
        )
        assert canceled.status_code == 200
        assert canceled.json()["status"] == "canceled"


def test_task_api_rejects_invalid_generation_options(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path), adapter=UnusedAdapter(), start_worker=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/avatar/tasks",
            headers={"Authorization": "Bearer secret"},
            files={
                "template_video": ("template.mp4", b"video", "video/mp4"),
                "driving_audio": ("voice.mp3", b"audio", "audio/mpeg"),
            },
            data={"options": '{"width": 481}'},
        )
    assert response.status_code == 422


def test_batch_api_creates_independent_tasks_and_cancels_them(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path), adapter=UnusedAdapter(), start_worker=False)

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["worker_count"] == 1
        assert health.json()["worker_ids"] == ["test-worker"]

        response = client.post(
            "/v1/avatar/batches",
            headers={"Authorization": "Bearer secret"},
            files=[
                ("template_video", ("template.mp4", b"video", "video/mp4")),
                ("driving_audios", ("first.mp3", b"first audio", "audio/mpeg")),
                ("driving_audios", ("second.wav", b"second audio", "audio/wav")),
            ],
            data={
                "client_ref": "batch-project",
                "client_refs": '["script-1", "script-2"]',
                "submitted_by": "operator-a",
            },
        )

        assert response.status_code == 202
        created = response.json()
        assert created["status"] == "queued"
        assert created["total"] == 2
        assert created["queued"] == 2
        assert [task["batch_index"] for task in created["tasks"]] == [0, 1]
        assert [task["client_ref"] for task in created["tasks"]] == ["script-1", "script-2"]
        assert all(task["batch_id"] == created["id"] for task in created["tasks"])

        batch_detail = client.get(
            f"/v1/avatar/batches/{created['id']}",
            headers={"Authorization": "Bearer secret"},
        )
        assert batch_detail.status_code == 200
        assert batch_detail.json()["total"] == 2

        canceled = client.post(
            f"/v1/avatar/batches/{created['id']}/cancel",
            headers={"Authorization": "Bearer secret"},
        )
        assert canceled.status_code == 200
        assert canceled.json()["status"] == "canceled"
        assert canceled.json()["canceled"] == 2

    stored_audio = sorted((tmp_path / "uploads" / "batches" / created["id"] / "jobs").glob("*/driving-audio.*"))
    assert [path.read_bytes() for path in stored_audio] == [b"first audio", b"second audio"]


def test_batch_api_validates_item_refs_and_batch_limit(tmp_path: Path) -> None:
    app = create_app(
        settings=make_settings(tmp_path, max_batch_items=1),
        adapter=UnusedAdapter(),
        start_worker=False,
    )
    headers = {"Authorization": "Bearer secret"}

    with TestClient(app) as client:
        too_many = client.post(
            "/v1/avatar/batches",
            headers=headers,
            files=[
                ("template_video", ("template.mp4", b"video", "video/mp4")),
                ("driving_audios", ("first.mp3", b"first", "audio/mpeg")),
                ("driving_audios", ("second.mp3", b"second", "audio/mpeg")),
            ],
        )
        assert too_many.status_code == 422

        mismatched_refs = client.post(
            "/v1/avatar/batches",
            headers=headers,
            files=[
                ("template_video", ("template.mp4", b"video", "video/mp4")),
                ("driving_audios", ("first.mp3", b"first", "audio/mpeg")),
            ],
            data={"client_refs": "[]"},
        )
        assert mismatched_refs.status_code == 422
