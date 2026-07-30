from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings, WorkerEndpoint
from app.reference_audio import ReferenceAudioStore


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


def test_reference_audio_upload_requires_auth_and_signed_url_is_public(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings=settings, adapter=UnusedAdapter(), start_worker=False)

    with TestClient(app, base_url="https://gateway.example") as client:
        unauthorized = client.post(
            "/v1/reference-audio",
            files={"audio": ("voice.mp3", b"reference voice", "audio/mpeg")},
        )
        assert unauthorized.status_code == 401

        uploaded = client.post(
            "/v1/reference-audio",
            headers={"Authorization": "Bearer secret"},
            files={"audio": ("voice.mp3", b"reference voice", "audio/mpeg")},
        )
        assert uploaded.status_code == 201
        payload = uploaded.json()
        assert payload["url"].startswith(f"https://gateway.example/v1/reference-audio/{payload['id']}?")
        assert payload["expires_at"].endswith("Z")

        downloaded = client.get(payload["url"])
        assert downloaded.status_code == 200
        assert downloaded.content == b"reference voice"
        assert downloaded.headers["content-type"].startswith("audio/mpeg")
        assert downloaded.headers["cache-control"] == "private, no-store"
        assert (settings.reference_audio_dir / payload["id"] / "audio.mp3").read_bytes() == b"reference voice"


def test_reference_audio_rejects_bad_signature_and_can_be_deleted(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings=settings, adapter=UnusedAdapter(), start_worker=False)

    with TestClient(app) as client:
        uploaded = client.post(
            "/v1/reference-audio",
            headers={"Authorization": "Bearer secret"},
            files={"audio": ("voice.wav", b"reference voice", "audio/wav")},
        ).json()
        signed_url = urlsplit(uploaded["url"])
        query = parse_qs(signed_url.query)
        query["signature"] = ["0" * 64]
        tampered_url = urlunsplit((*signed_url[:3], urlencode(query, doseq=True), signed_url.fragment))
        assert client.get(tampered_url).status_code == 401

        deleted = client.delete(
            f"/v1/reference-audio/{uploaded['id']}",
            headers={"Authorization": "Bearer secret"},
        )
        assert deleted.status_code == 204
        assert client.get(uploaded["url"]).status_code == 404


def test_reference_audio_validates_extension_and_size(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_reference_audio_bytes=4)
    app = create_app(settings=settings, adapter=UnusedAdapter(), start_worker=False)
    headers = {"Authorization": "Bearer secret"}

    with TestClient(app) as client:
        unsupported = client.post(
            "/v1/reference-audio",
            headers=headers,
            files={"audio": ("voice.txt", b"voice", "text/plain")},
        )
        assert unsupported.status_code == 415

        oversized = client.post(
            "/v1/reference-audio",
            headers=headers,
            files={"audio": ("voice.mp3", b"voice", "audio/mpeg")},
        )
        assert oversized.status_code == 413
        assert list(settings.reference_audio_dir.iterdir()) == []


def test_reference_audio_expiry_removes_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, reference_audio_ttl_seconds=0)
    app = create_app(settings=settings, adapter=UnusedAdapter(), start_worker=False)

    with TestClient(app) as client:
        uploaded = client.post(
            "/v1/reference-audio",
            headers={"Authorization": "Bearer secret"},
            files={"audio": ("voice.m4a", b"reference voice", "audio/mp4")},
        ).json()
        expired = client.get(uploaded["url"])
        assert expired.status_code == 410
        assert not (settings.reference_audio_dir / uploaded["id"]).exists()


def test_reference_audio_cleanup_runs_at_startup(tmp_path: Path) -> None:
    stale_store = ReferenceAudioStore(tmp_path / "reference-audio", "secret", ttl_seconds=-1)
    stale_store.initialize()
    stale_id, stale_path = stale_store.allocate(".mp3")
    stale_path.write_bytes(b"stale")
    stale_store.commit(stale_id, stale_path)
    assert (stale_store.root_dir / stale_id).exists()

    create_app(settings=make_settings(tmp_path), adapter=UnusedAdapter(), start_worker=False)

    assert not (stale_store.root_dir / stale_id).exists()
