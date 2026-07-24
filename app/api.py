from __future__ import annotations

import hmac
import json
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import ValidationError

from .config import Settings
from .gradio_adapter import AvatarAdapter, GradioInfiniteTalkAdapter
from .models import (
    GenerationOptions,
    JobRecord,
    JobStatus,
    TaskCreatedResponse,
    TaskResponse,
)
from .store import JobStore, utc_now
from .worker import JobWorker

VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
CHUNK_SIZE = 1024 * 1024


def create_app(
    settings: Settings | None = None,
    adapter: AvatarAdapter | None = None,
    start_worker: bool = True,
) -> FastAPI:
    resolved_settings = settings or Settings.from_environment()
    resolved_settings.data_dir.mkdir(parents=True, exist_ok=True)
    resolved_settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    resolved_settings.outputs_dir.mkdir(parents=True, exist_ok=True)

    store = JobStore(resolved_settings.database_path)
    store.initialize()
    store.recover_after_restart()
    worker = JobWorker(
        store=store,
        adapter=adapter or GradioInfiniteTalkAdapter(resolved_settings),
        outputs_dir=resolved_settings.outputs_dir,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            if start_worker:
                worker.stop()

    app = FastAPI(
        title="ContentPlane Avatar Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.store = store
    app.state.worker = worker

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token is required")
        supplied = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied, resolved_settings.api_token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token is invalid")

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "contentplane-avatar-gateway",
            "version": "0.1.0",
            "worker": "single",
        }

    @app.post(
        "/v1/avatar/tasks",
        response_model=TaskCreatedResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_token)],
    )
    async def create_task(
        template_video: Annotated[UploadFile, File()],
        driving_audio: Annotated[UploadFile, File()],
        options: Annotated[str, Form()] = "{}",
        client_ref: Annotated[str | None, Form()] = None,
        submitted_by: Annotated[str | None, Form()] = None,
    ) -> TaskCreatedResponse:
        generation_options = parse_options(options)
        job_id = str(uuid.uuid4())
        job_dir = resolved_settings.uploads_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=False)

        try:
            template_path = await save_upload(
                template_video,
                job_dir,
                "template",
                VIDEO_EXTENSIONS,
                resolved_settings.max_template_video_bytes,
            )
            audio_path = await save_upload(
                driving_audio,
                job_dir,
                "driving-audio",
                AUDIO_EXTENSIONS,
                resolved_settings.max_driving_audio_bytes,
            )
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        finally:
            await template_video.close()
            await driving_audio.close()

        now = utc_now()
        job = JobRecord(
            id=job_id,
            status=JobStatus.QUEUED,
            template_video_path=template_path,
            driving_audio_path=audio_path,
            output_path=None,
            client_ref=clean_optional(client_ref, 200),
            submitted_by=clean_optional(submitted_by, 200),
            options=generation_options,
            message="Task queued for avatar generation.",
            logs="",
            error=None,
            cancel_requested=False,
            created_at=now,
            updated_at=now,
            started_at=None,
            finished_at=None,
        )
        store.create(job)
        worker.wake()
        return TaskCreatedResponse(id=job.id, status=job.status, position=store.queue_position(job.id))

    @app.get(
        "/v1/avatar/tasks",
        response_model=list[TaskResponse],
        dependencies=[Depends(require_token)],
    )
    def list_tasks(limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[TaskResponse]:
        return [task_response(job) for job in store.list_jobs(limit)]

    @app.get(
        "/v1/avatar/tasks/{job_id}",
        response_model=TaskResponse,
        dependencies=[Depends(require_token)],
    )
    def get_task(job_id: str) -> TaskResponse:
        return task_response(require_job(store, job_id))

    @app.post(
        "/v1/avatar/tasks/{job_id}/cancel",
        response_model=TaskResponse,
        dependencies=[Depends(require_token)],
    )
    def cancel_task(job_id: str) -> TaskResponse:
        require_job(store, job_id)
        updated = store.request_cancel(job_id)
        if not updated:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        worker.wake()
        return task_response(updated)

    @app.get(
        "/v1/avatar/tasks/{job_id}/video",
        dependencies=[Depends(require_token)],
        response_class=FileResponse,
    )
    def download_video(job_id: str) -> FileResponse:
        job = require_job(store, job_id)
        if job.status != JobStatus.SUCCEEDED or not job.output_path or not job.output_path.is_file():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task video is not available")
        return FileResponse(
            path=job.output_path,
            media_type="video/mp4",
            filename=f"{job.id}.mp4",
        )

    return app


def parse_options(raw_options: str) -> GenerationOptions:
    try:
        payload = json.loads(raw_options or "{}")
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=422, detail="options must be valid JSON") from error
    try:
        return GenerationOptions.model_validate(payload)
    except ValidationError as error:
        raise HTTPException(
            status_code=422,
            detail=error.errors(include_context=False, include_url=False),
        ) from error


async def save_upload(
    upload: UploadFile,
    target_dir: Path,
    stem: str,
    allowed_extensions: set[str],
    byte_limit: int,
) -> Path:
    extension = Path(upload.filename or "").suffix.lower()
    if extension not in allowed_extensions:
        allowed = ", ".join(sorted(allowed_extensions))
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"Supported file types: {allowed}"
        )

    target = target_dir / f"{stem}{extension}"
    bytes_written = 0
    with target.open("wb") as destination:
        while chunk := await upload.read(CHUNK_SIZE):
            bytes_written += len(chunk)
            if bytes_written > byte_limit:
                target.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Uploaded file is too large"
                )
            destination.write(chunk)
    if bytes_written == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="Uploaded file is empty")
    return target


def require_job(store: JobStore, job_id: str) -> JobRecord:
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return job


def task_response(job: JobRecord) -> TaskResponse:
    return TaskResponse(
        id=job.id,
        status=job.status,
        client_ref=job.client_ref,
        submitted_by=job.submitted_by,
        message=job.message,
        logs=job.logs,
        error=job.error,
        cancel_requested=job.cancel_requested,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        result_url=f"/v1/avatar/tasks/{job.id}/video" if job.status == JobStatus.SUCCEEDED else None,
    )


def clean_optional(value: str | None, max_length: int) -> str | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    return normalized[:max_length]
