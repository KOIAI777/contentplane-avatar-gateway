from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings


class UnusedAdapter:
    def generate(self, job, report, should_cancel):  # type: ignore[no-untyped-def]
        raise AssertionError("worker is disabled in API tests")


def test_task_api_requires_auth_and_persists_uploads(tmp_path: Path) -> None:
    settings = Settings(tmp_path, "secret", "https://worker")
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
                "options": '{"steps": 4, "camera_control": true}',
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
    app = create_app(
        settings=Settings(tmp_path, "secret", "https://worker"), adapter=UnusedAdapter(), start_worker=False
    )
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
