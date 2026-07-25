from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    api_token: str
    gradio_url: str
    comfyui_url: str | None = None
    poll_interval_seconds: float = 5.0
    task_timeout_seconds: int = 60 * 60
    max_template_video_bytes: int = 500 * 1024 * 1024
    max_driving_audio_bytes: int = 100 * 1024 * 1024

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    @classmethod
    def from_environment(cls) -> Settings:
        token = os.environ.get("GATEWAY_API_TOKEN", "").strip()
        if not token:
            raise RuntimeError("GATEWAY_API_TOKEN must be set before starting the gateway")

        gradio_url = os.environ.get("INFINITETALK_GRADIO_URL", "").strip()
        if not gradio_url:
            raise RuntimeError("INFINITETALK_GRADIO_URL must be set before starting the gateway")

        data_dir = Path(os.environ.get("GATEWAY_DATA_DIR", "./data")).expanduser().resolve()
        comfyui_url = os.environ.get("INFINITETALK_COMFYUI_URL", "").strip()
        return cls(
            data_dir=data_dir,
            api_token=token,
            gradio_url=gradio_url.rstrip("/"),
            comfyui_url=comfyui_url.rstrip("/") or None,
            poll_interval_seconds=float(os.environ.get("GATEWAY_POLL_INTERVAL_SECONDS", "5")),
            task_timeout_seconds=int(os.environ.get("GATEWAY_TASK_TIMEOUT_SECONDS", str(60 * 60))),
        )
