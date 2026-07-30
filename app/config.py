from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkerEndpoint:
    id: str
    url: str


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    api_token: str
    worker_endpoints: tuple[WorkerEndpoint, ...]
    poll_interval_seconds: float = 5.0
    task_timeout_seconds: int = 60 * 60
    max_template_video_bytes: int = 500 * 1024 * 1024
    max_driving_audio_bytes: int = 100 * 1024 * 1024
    max_reference_audio_bytes: int = 20 * 1024 * 1024
    reference_audio_ttl_seconds: int = 15 * 60
    max_batch_items: int = 100

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def reference_audio_dir(self) -> Path:
        return self.data_dir / "reference-audio"

    @classmethod
    def from_environment(cls) -> Settings:
        token = os.environ.get("GATEWAY_API_TOKEN", "").strip()
        if not token:
            raise RuntimeError("GATEWAY_API_TOKEN must be set before starting the gateway")

        worker_endpoints = _worker_endpoints_from_environment()

        data_dir = Path(os.environ.get("GATEWAY_DATA_DIR", "./data")).expanduser().resolve()
        return cls(
            data_dir=data_dir,
            api_token=token,
            worker_endpoints=worker_endpoints,
            poll_interval_seconds=float(os.environ.get("GATEWAY_POLL_INTERVAL_SECONDS", "5")),
            task_timeout_seconds=int(os.environ.get("GATEWAY_TASK_TIMEOUT_SECONDS", str(60 * 60))),
            max_reference_audio_bytes=_positive_environment_int("GATEWAY_MAX_REFERENCE_AUDIO_BYTES", 20 * 1024 * 1024),
            reference_audio_ttl_seconds=_positive_environment_int("GATEWAY_REFERENCE_AUDIO_TTL_SECONDS", 15 * 60),
            max_batch_items=_positive_environment_int("GATEWAY_MAX_BATCH_ITEMS", 100),
        )


def _positive_environment_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive integer") from error
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def _worker_endpoints_from_environment() -> tuple[WorkerEndpoint, ...]:
    raw_workers = os.environ.get("HEYGEM_WORKERS", "").strip()
    if not raw_workers:
        single_url = os.environ.get("HEYGEM_GRADIO_URL", "").strip()
        if single_url:
            raw_workers = f"gpu-0={single_url}"
    if not raw_workers:
        raise RuntimeError(
            "HEYGEM_WORKERS must define at least one worker, for example "
            "gpu-0=http://127.0.0.1:7860,gpu-1=http://127.0.0.1:7861"
        )

    endpoints: list[WorkerEndpoint] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for index, item in enumerate(raw_workers.split(","), start=1):
        value = item.strip()
        if not value:
            continue
        if "=" in value:
            worker_id, url = value.split("=", 1)
        else:
            worker_id, url = f"worker-{index}", value
        normalized_id = worker_id.strip()[:80]
        normalized_url = url.strip().rstrip("/")
        if not normalized_id or not normalized_url.startswith(("http://", "https://")):
            raise RuntimeError(f"Invalid HEYGEM_WORKERS entry: {value}")
        if normalized_id in seen_ids:
            raise RuntimeError(f"Duplicate HeyGem worker id: {normalized_id}")
        if normalized_url in seen_urls:
            raise RuntimeError(f"Duplicate HeyGem worker URL: {normalized_url}")
        seen_ids.add(normalized_id)
        seen_urls.add(normalized_url)
        endpoints.append(WorkerEndpoint(id=normalized_id, url=normalized_url))

    if not endpoints:
        raise RuntimeError("HEYGEM_WORKERS must define at least one valid worker")
    return tuple(endpoints)
