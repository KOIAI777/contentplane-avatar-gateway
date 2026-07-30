from __future__ import annotations

import hmac
import json
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import ValidationError

from .adapter import AvatarAdapter
from .config import Settings
from .heygem_adapter import GradioHeyGemAdapter
from .models import (
    BatchRecord,
    BatchResponse,
    BatchStatus,
    GenerationOptions,
    JobRecord,
    JobStatus,
    ReferenceAudioUploadResponse,
    TaskCreatedResponse,
    TaskResponse,
)
from .reference_audio import ReferenceAudioStore, valid_reference_audio_id
from .store import JobStore, utc_now
from .worker import JobWorker, JobWorkerPool

VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
CHUNK_SIZE = 1024 * 1024
VERSION = "0.2.0"


def create_app(
    settings: Settings | None = None,
    adapter: AvatarAdapter | None = None,
    start_worker: bool = True,
) -> FastAPI:
    resolved_settings = settings or Settings.from_environment()
    resolved_settings.data_dir.mkdir(parents=True, exist_ok=True)
    resolved_settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    resolved_settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    reference_audio = ReferenceAudioStore(
        resolved_settings.reference_audio_dir,
        resolved_settings.api_token,
        resolved_settings.reference_audio_ttl_seconds,
    )
    reference_audio.initialize()

    store = JobStore(resolved_settings.database_path)
    store.initialize()
    store.recover_after_restart()
    if adapter:
        endpoint = resolved_settings.worker_endpoints[0]
        worker_adapters = ((getattr(adapter, "worker_id", endpoint.id), adapter),)
    else:
        worker_adapters = tuple(
            (endpoint.id, GradioHeyGemAdapter(resolved_settings, endpoint))
            for endpoint in resolved_settings.worker_endpoints
        )
    worker_pool = JobWorkerPool(
        JobWorker(
            worker_id=worker_id,
            store=store,
            adapter=worker_adapter,
            outputs_dir=resolved_settings.outputs_dir,
        )
        for worker_id, worker_adapter in worker_adapters
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if start_worker:
            worker_pool.start()
        try:
            yield
        finally:
            if start_worker:
                worker_pool.stop()

    app = FastAPI(
        title="ContentPlane Avatar Gateway",
        version=VERSION,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.store = store
    app.state.worker = worker_pool
    app.state.worker_pool = worker_pool
    app.state.reference_audio = reference_audio

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
            "version": VERSION,
            "worker_count": worker_pool.worker_count,
            "worker_ids": list(worker_pool.worker_ids),
        }

    @app.post(
        "/v1/reference-audio",
        response_model=ReferenceAudioUploadResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_token)],
    )
    async def upload_reference_audio(
        request: Request,
        audio: Annotated[UploadFile, File()],
    ) -> ReferenceAudioUploadResponse:
        reference_audio.cleanup_expired()
        extension = Path(audio.filename or "").suffix.lower()
        if extension not in AUDIO_EXTENSIONS:
            await audio.close()
            allowed = ", ".join(sorted(AUDIO_EXTENSIONS))
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"Supported file types: {allowed}",
            )
        audio_id, audio_path = reference_audio.allocate(extension)
        try:
            await save_upload(
                audio,
                audio_path.parent,
                "audio",
                AUDIO_EXTENSIONS,
                resolved_settings.max_reference_audio_bytes,
            )
            record = reference_audio.commit(audio_id, audio_path)
        except Exception:
            reference_audio.delete(audio_id)
            raise
        finally:
            await audio.close()

        signature = reference_audio.sign(record.id, record.expires_at)
        public_url = request.url_for("download_reference_audio", audio_id=record.id).include_query_params(
            expires=record.expires_at,
            signature=signature,
        )
        return ReferenceAudioUploadResponse(
            id=record.id,
            url=str(public_url),
            expires_at=datetime.fromtimestamp(record.expires_at, timezone.utc),
        )

    @app.get(
        "/v1/reference-audio/{audio_id}",
        response_class=FileResponse,
        name="download_reference_audio",
    )
    def download_reference_audio(
        audio_id: str,
        expires: Annotated[int, Query(gt=0)],
        signature: Annotated[str, Query()],
    ) -> FileResponse:
        if not valid_reference_audio_id(audio_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference audio not found")
        if not reference_audio.signature_is_valid(audio_id, expires, signature):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Signed URL is invalid")
        if expires <= int(time.time()):
            reference_audio.delete(audio_id)
            reference_audio.cleanup_expired()
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="Signed URL has expired")

        reference_audio.cleanup_expired()
        record = reference_audio.get(audio_id, expires)
        if not record:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference audio not found")
        return FileResponse(
            path=record.path,
            media_type=reference_audio_media_type(record.path.suffix),
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.delete(
        "/v1/reference-audio/{audio_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_token)],
    )
    def delete_reference_audio(audio_id: str) -> Response:
        reference_audio.cleanup_expired()
        reference_audio.delete(audio_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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
        worker_pool.wake()
        return TaskCreatedResponse(id=job.id, status=job.status, position=store.queue_position(job.id))

    @app.post(
        "/v1/avatar/batches",
        response_model=BatchResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_token)],
    )
    async def create_batch(
        template_video: Annotated[UploadFile, File()],
        driving_audios: Annotated[list[UploadFile], File()],
        options: Annotated[str, Form()] = "{}",
        client_ref: Annotated[str | None, Form()] = None,
        client_refs: Annotated[str | None, Form()] = None,
        submitted_by: Annotated[str | None, Form()] = None,
    ) -> BatchResponse:
        if not driving_audios:
            raise HTTPException(status_code=422, detail="At least one driving audio file is required")
        if len(driving_audios) > resolved_settings.max_batch_items:
            raise HTTPException(
                status_code=422,
                detail=f"A batch can contain at most {resolved_settings.max_batch_items} items",
            )

        generation_options = parse_options(options)
        item_refs = parse_client_refs(client_refs, len(driving_audios))
        batch_id = str(uuid.uuid4())
        batch_dir = resolved_settings.uploads_dir / "batches" / batch_id
        batch_dir.mkdir(parents=True, exist_ok=False)
        jobs: list[JobRecord] = []
        try:
            template_path = await save_upload(
                template_video,
                batch_dir,
                "template",
                VIDEO_EXTENSIONS,
                resolved_settings.max_template_video_bytes,
            )
            for index, driving_audio in enumerate(driving_audios):
                job_id = str(uuid.uuid4())
                job_dir = batch_dir / "jobs" / f"{index + 1:03d}-{job_id}"
                job_dir.mkdir(parents=True, exist_ok=False)
                audio_path = await save_upload(
                    driving_audio,
                    job_dir,
                    "driving-audio",
                    AUDIO_EXTENSIONS,
                    resolved_settings.max_driving_audio_bytes,
                )
                now = utc_now()
                jobs.append(
                    JobRecord(
                        id=job_id,
                        status=JobStatus.QUEUED,
                        template_video_path=template_path,
                        driving_audio_path=audio_path,
                        output_path=None,
                        client_ref=item_refs[index] or clean_optional(client_ref, 200),
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
                        batch_id=batch_id,
                        batch_index=index,
                    )
                )
            batch = BatchRecord(
                id=batch_id,
                client_ref=clean_optional(client_ref, 200),
                submitted_by=clean_optional(submitted_by, 200),
                created_at=utc_now(),
            )
            store.create_batch(batch, jobs)
        except Exception:
            shutil.rmtree(batch_dir, ignore_errors=True)
            raise
        finally:
            await template_video.close()
            for driving_audio in driving_audios:
                await driving_audio.close()

        worker_pool.wake()
        return batch_response(batch, jobs)

    @app.get(
        "/v1/avatar/batches",
        response_model=list[BatchResponse],
        dependencies=[Depends(require_token)],
    )
    def list_batches(limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[BatchResponse]:
        return [batch_response(batch, store.list_jobs_for_batch(batch.id)) for batch in store.list_batches(limit)]

    @app.get(
        "/v1/avatar/batches/{batch_id}",
        response_model=BatchResponse,
        dependencies=[Depends(require_token)],
    )
    def get_batch(batch_id: str) -> BatchResponse:
        batch = require_batch(store, batch_id)
        return batch_response(batch, store.list_jobs_for_batch(batch.id))

    @app.post(
        "/v1/avatar/batches/{batch_id}/cancel",
        response_model=BatchResponse,
        dependencies=[Depends(require_token)],
    )
    def cancel_batch(batch_id: str) -> BatchResponse:
        batch = require_batch(store, batch_id)
        jobs = store.request_cancel_batch(batch.id)
        worker_pool.wake()
        return batch_response(batch, jobs)

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
        worker_pool.wake()
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
                raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Uploaded file is too large")
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


def require_batch(store: JobStore, batch_id: str) -> BatchRecord:
    batch = store.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")
    return batch


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
        batch_id=job.batch_id,
        batch_index=job.batch_index,
        worker_id=job.worker_id,
    )


def batch_response(batch: BatchRecord, jobs: list[JobRecord]) -> BatchResponse:
    queued = sum(job.status == JobStatus.QUEUED for job in jobs)
    running = sum(job.status == JobStatus.RUNNING for job in jobs)
    succeeded = sum(job.status == JobStatus.SUCCEEDED for job in jobs)
    failed = sum(job.status in (JobStatus.FAILED, JobStatus.INTERRUPTED) for job in jobs)
    canceled = sum(job.status == JobStatus.CANCELED for job in jobs)
    total = len(jobs)

    if total > 0 and succeeded == total:
        batch_status = BatchStatus.SUCCEEDED
    elif total > 0 and canceled == total:
        batch_status = BatchStatus.CANCELED
    elif queued == total:
        batch_status = BatchStatus.QUEUED
    elif running or queued:
        batch_status = BatchStatus.RUNNING
    elif failed + canceled == total:
        batch_status = BatchStatus.FAILED
    else:
        batch_status = BatchStatus.PARTIAL_FAILED

    return BatchResponse(
        id=batch.id,
        status=batch_status,
        client_ref=batch.client_ref,
        submitted_by=batch.submitted_by,
        created_at=batch.created_at,
        total=total,
        queued=queued,
        running=running,
        succeeded=succeeded,
        failed=failed,
        canceled=canceled,
        tasks=[task_response(job) for job in jobs],
    )


def parse_client_refs(raw_client_refs: str | None, item_count: int) -> list[str | None]:
    if raw_client_refs is None or not raw_client_refs.strip():
        return [None] * item_count
    try:
        payload = json.loads(raw_client_refs)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=422, detail="client_refs must be valid JSON") from error
    if not isinstance(payload, list) or len(payload) != item_count:
        raise HTTPException(status_code=422, detail="client_refs must be an array matching driving_audios")
    if any(value is not None and not isinstance(value, str) for value in payload):
        raise HTTPException(status_code=422, detail="client_refs items must be strings or null")
    return [clean_optional(value, 200) for value in payload]


def clean_optional(value: str | None, max_length: int) -> str | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    return normalized[:max_length]


def reference_audio_media_type(extension: str) -> str:
    return {
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
    }.get(extension.lower(), "application/octet-stream")
