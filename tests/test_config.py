import pytest

from app.config import Settings


def test_settings_parse_multiple_heygem_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_API_TOKEN", "secret")
    monkeypatch.setenv(
        "HEYGEM_WORKERS",
        "gpu-0=http://127.0.0.1:7860,gpu-1=http://127.0.0.1:7861/",
    )

    settings = Settings.from_environment()

    assert [(worker.id, worker.url) for worker in settings.worker_endpoints] == [
        ("gpu-0", "http://127.0.0.1:7860"),
        ("gpu-1", "http://127.0.0.1:7861"),
    ]


def test_settings_reject_duplicate_worker_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_API_TOKEN", "secret")
    monkeypatch.setenv(
        "HEYGEM_WORKERS",
        "gpu-0=http://127.0.0.1:7860,gpu-1=http://127.0.0.1:7860",
    )

    with pytest.raises(RuntimeError, match="Duplicate HeyGem worker URL"):
        Settings.from_environment()
