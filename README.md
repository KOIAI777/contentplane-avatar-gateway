# ContentPlane Avatar Gateway

An authenticated, persistent task gateway between ContentPlane and one or more HeyGem Gradio workers.

The gateway exposes one stable API and token, stores tasks in SQLite, assigns each queued task to one available worker, supports batch submission, and keeps generated videos available for authenticated download. Each configured worker owns its own Gradio client and processes one task at a time.

```text
ContentPlane clients
        |
        v
FastAPI gateway + SQLite queue
        |
        +---- gpu-0 -> HeyGem :7860
        |
        +---- gpu-1 -> HeyGem :7861
```

## Requirements

- Python 3.10, 3.11, or 3.12
- One or more reachable HeyGem Gradio services exposing `/process_single`
- `uv` for the documented installation flow

Each HeyGem instance must run in its own process and be pinned to its intended GPU. The gateway does not start or isolate the model processes itself.

## Install

```bash
uv sync --python 3.11 --extra dev
```

Configure the environment:

```bash
export GATEWAY_API_TOKEN="replace-with-a-long-random-token"
export HEYGEM_WORKERS="gpu-0=http://127.0.0.1:7860,gpu-1=http://127.0.0.1:7861"
export GATEWAY_DATA_DIR="./data"
export GATEWAY_MAX_BATCH_ITEMS="100"
```

For a one-GPU deployment, configure a single entry. `HEYGEM_GRADIO_URL` remains available as a one-worker shorthand.

Start exactly one API process:

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8787 --workers 1
```

Interactive API documentation is available at `/docs`. The unauthenticated `/health` endpoint reports configured worker IDs. All task and batch endpoints require `Authorization: Bearer <token>`.

## Single task

The existing single-task contract remains available:

```bash
curl -X POST "http://127.0.0.1:8787/v1/avatar/tasks" \
  -H "Authorization: Bearer ${GATEWAY_API_TOKEN}" \
  -F "template_video=@/path/to/template.mp4" \
  -F "driving_audio=@/path/to/speech.mp3" \
  -F "client_ref=content-project-id" \
  -F "submitted_by=operator-id"
```

The response contains a gateway task ID and queue position:

```json
{"id":"018f...","status":"queued","position":1}
```

Poll and download the result:

```bash
curl -H "Authorization: Bearer ${GATEWAY_API_TOKEN}" \
  "http://127.0.0.1:8787/v1/avatar/tasks/<task-id>"

curl -H "Authorization: Bearer ${GATEWAY_API_TOKEN}" \
  -o result.mp4 \
  "http://127.0.0.1:8787/v1/avatar/tasks/<task-id>/video"
```

The optional `options` JSON form field is retained for ContentPlane compatibility. HeyGem's current `/process_single` endpoint receives the driving audio and template video only, so InfiniteTalk-specific generation fields are stored but not forwarded.

## Batch tasks

A batch reuses one template video and creates one independent task for each uploaded driving audio. Repeat the `driving_audios` form field:

```bash
curl -X POST "http://127.0.0.1:8787/v1/avatar/batches" \
  -H "Authorization: Bearer ${GATEWAY_API_TOKEN}" \
  -F "template_video=@/path/to/template.mp4" \
  -F "driving_audios=@/path/to/01.mp3" \
  -F "driving_audios=@/path/to/02.mp3" \
  -F 'client_refs=["script-01","script-02"]' \
  -F "client_ref=content-project-id" \
  -F "submitted_by=operator-id"
```

`client_refs` is optional. When present, it must be a JSON array with one string or `null` per audio. It lets ContentPlane reconcile each generated video with its source script. `client_ref` remains the batch-level reference and is also the fallback task reference.

Batch status is computed from its tasks and can be `queued`, `running`, `succeeded`, `partial_failed`, `failed`, or `canceled`.

```bash
curl -H "Authorization: Bearer ${GATEWAY_API_TOKEN}" \
  "http://127.0.0.1:8787/v1/avatar/batches/<batch-id>"

curl -X POST \
  -H "Authorization: Bearer ${GATEWAY_API_TOKEN}" \
  "http://127.0.0.1:8787/v1/avatar/batches/<batch-id>/cancel"
```

Cancellation is best effort for a task already executing inside HeyGem. Queued tasks are canceled immediately.

## Temporary reference audio

The gateway can expose a short-lived reference-audio URL for providers that need to fetch an audio sample over HTTP. Upload is authenticated and limited to 20 MB; the returned download URL uses an HMAC signature derived from `GATEWAY_API_TOKEN` and expires after 15 minutes by default.

```bash
curl -X POST "https://gateway.example/v1/reference-audio" \
  -H "Authorization: Bearer ${GATEWAY_API_TOKEN}" \
  -F "audio=@/path/to/reference.mp3"
```

The signed `GET` URL intentionally needs no Bearer header so an external speech provider can fetch it. Do not log or share it. ContentPlane can remove the temporary asset early:

```bash
curl -X DELETE \
  -H "Authorization: Bearer ${GATEWAY_API_TOKEN}" \
  "https://gateway.example/v1/reference-audio/<reference-id>"
```

`GATEWAY_MAX_REFERENCE_AUDIO_BYTES` changes the byte limit. `GATEWAY_REFERENCE_AUDIO_TTL_SECONDS` changes the signed URL lifetime. Deploy behind a reverse proxy that forwards the public host and protocol correctly.

## API

```text
GET    /health
POST   /v1/reference-audio
GET    /v1/reference-audio/{id}?expires=...&signature=...
DELETE /v1/reference-audio/{id}
POST   /v1/avatar/tasks
GET    /v1/avatar/tasks
GET    /v1/avatar/tasks/{id}
POST   /v1/avatar/tasks/{id}/cancel
GET    /v1/avatar/tasks/{id}/video
POST   /v1/avatar/batches
GET    /v1/avatar/batches
GET    /v1/avatar/batches/{id}
POST   /v1/avatar/batches/{id}/cancel
```

## Storage and recovery

```text
data/
  jobs.sqlite3
  reference-audio/{reference-id}/
  uploads/{task-id}/
  uploads/batches/{batch-id}/
    template.mp4
    jobs/{index}-{task-id}/driving-audio.*
  outputs/{task-id}.mp4
```

Mount `GATEWAY_DATA_DIR` on persistent storage. Queued tasks survive a gateway restart. A task that was already running is marked `interrupted` instead of being submitted twice.

## Deployment notes

- Run exactly one Uvicorn process. Multiple Uvicorn processes would each create their own in-process worker pool for the same GPU endpoints.
- Bind HeyGem ports to localhost or restrict them with the platform firewall. Only the authenticated gateway should be public.
- Keep one HeyGem process per GPU and use distinct ports, working directories, and output directories.
- Configure only one gateway URL and token in ContentPlane. Worker URLs remain private deployment details.
- Large uploads stay beside the gateway, so place the gateway near the HeyGem instances and persist `GATEWAY_DATA_DIR`.
- The gateway performs avatar generation only. Reference-audio transport exists for zero-shot speech providers; voice cloning and speech synthesis remain outside the HeyGem adapter.

### Compshare startup

The Compshare image runs every `/start.d/*.sh` file when the container starts. Link the versioned gateway startup script after cloning and installing:

```bash
ln -s /root/contentplane-avatar-gateway/deploy/compshare-start.sh \
  /start.d/contentplane-avatar-gateway.sh
```

Keep the production token and `HEYGEM_WORKERS` outside Git in `/root/contentplane-avatar-gateway/.env`, readable only by root:

```bash
chmod 600 /root/contentplane-avatar-gateway/.env
```

## Verify

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```
