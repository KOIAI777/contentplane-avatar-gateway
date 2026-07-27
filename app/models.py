from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_POSITIVE_PROMPT = "人物正在说话，手势动作自然，头部动作自然，富有感染力"
DEFAULT_NEGATIVE_PROMPT = (
    "bright tones, overexposed, static, blurred details, subtitles, style, works, paintings, images, "
    "static, overall gray, worst quality, low quality, JPEG compression residue, ugly, incomplete, "
    "extra fingers, poorly drawn hands, poorly drawn faces, deformed, disfigured, misshapen limbs, "
    "fused fingers, still picture, messy background, three legs, many people in the background, walking backwards"
)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"


class GenerationOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(default=480, ge=256, le=1920)
    height: int = Field(default=832, ge=256, le=1920)
    steps: int = Field(default=4, ge=2, le=50)
    blocks_to_swap: int = Field(default=40, ge=0, le=40)
    frame_window: int = Field(default=61, ge=41, le=101)
    fps: int = Field(default=25, ge=1, le=60)
    seed: int = -1
    hd_enabled: bool = True
    hd_resolution: int = Field(default=1080, ge=720, le=1440)
    camera_control: bool = True
    pose_stabilize: bool = True
    positive_prompt: str = Field(default=DEFAULT_POSITIVE_PROMPT, min_length=1, max_length=2000)
    negative_prompt: str = Field(default=DEFAULT_NEGATIVE_PROMPT, min_length=1, max_length=4000)

    @field_validator("width", "height")
    @classmethod
    def validate_dimension_step(cls, value: int) -> int:
        if value % 32:
            raise ValueError("width and height must be multiples of 32")
        return value

    @field_validator("steps")
    @classmethod
    def validate_steps(cls, value: int) -> int:
        if value % 2:
            raise ValueError("steps must be an even number")
        return value

    @field_validator("blocks_to_swap")
    @classmethod
    def validate_block_swap(cls, value: int) -> int:
        if value % 5:
            raise ValueError("blocks_to_swap must be a multiple of 5")
        return value

    @field_validator("frame_window")
    @classmethod
    def validate_frame_window(cls, value: int) -> int:
        if (value - 41) % 4:
            raise ValueError("frame_window must follow the Gradio slider step of 4")
        return value

    @field_validator("hd_resolution")
    @classmethod
    def validate_hd_resolution(cls, value: int) -> int:
        if value % 8:
            raise ValueError("hd_resolution must be a multiple of 8")
        return value


@dataclass(frozen=True)
class JobRecord:
    id: str
    status: JobStatus
    template_video_path: Path
    driving_audio_path: Path
    output_path: Path | None
    client_ref: str | None
    submitted_by: str | None
    options: GenerationOptions
    message: str
    logs: str
    error: str | None
    cancel_requested: bool
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True)
class ProgressUpdate:
    message: str
    logs: str = ""


@dataclass(frozen=True)
class AvatarResult:
    source_path: Path
    provider_result_url: str | None = None


class TaskCreatedResponse(BaseModel):
    id: str
    status: JobStatus
    position: int


class TaskResponse(BaseModel):
    id: str
    status: JobStatus
    client_ref: str | None
    submitted_by: str | None
    message: str
    logs: str
    error: str | None
    cancel_requested: bool
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    result_url: str | None
