# ContentPlane Avatar Gateway

An authenticated, persistent single-GPU task gateway between ContentPlane and remote avatar-generation workers.

The first adapter targets the InfiniteTalk Gradio API while exposing stable task IDs, a shared SQLite queue, status polling, cancellation, logs, and authenticated result downloads. It controls Gradio through its public API and never automates the webpage.

## Why this gateway exists

The current InfiniteTalk image exposes a global Gradio queue without per-task IDs. This gateway assigns its own UUID before a job reaches Gradio and submits only one active job at a time. Multiple ContentPlane clients can enqueue work safely while one GPU processes the queue serially.

## Requirements

- Python 3.10, 3.11, or 3.12
- An accessible InfiniteTalk Gradio 5.x service
- `uv` for the documented installation flow

## Install

```bash
uv sync --python 3.11 --extra dev
```

Configure the environment:

```bash
export GATEWAY_API_TOKEN="replace-with-a-long-random-token"
export INFINITETALK_GRADIO_URL="http://127.0.0.1:7860"
export GATEWAY_DATA_DIR="./data"
```

Start one API process and one GPU worker:

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8787 --workers 1
```

Interactive API documentation is available at `/docs`. The unauthenticated liveness endpoint is `/health`; all task endpoints require `Authorization: Bearer <token>`.

## Create a task

```bash
curl -X POST "http://127.0.0.1:8787/v1/avatar/tasks" \
  -H "Authorization: Bearer ${GATEWAY_API_TOKEN}" \
  -F "template_video=@/path/to/template.mp4" \
  -F "driving_audio=@/path/to/speech.mp3" \
  -F 'client_ref=content-project-id' \
  -F 'submitted_by=operator-id' \
  -F 'options={"steps":4,"blocks_to_swap":40,"frame_window":61}'
```

The response contains the gateway task ID and its queue position:

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

## API

```text
GET    /health
POST   /v1/avatar/tasks
GET    /v1/avatar/tasks
GET    /v1/avatar/tasks/{id}
POST   /v1/avatar/tasks/{id}/cancel
GET    /v1/avatar/tasks/{id}/video
```

## Storage

```text
data/
  jobs.sqlite3
  uploads/{task-id}/
  outputs/{task-id}.mp4
```

Mount `GATEWAY_DATA_DIR` on persistent storage. Queued tasks survive a gateway restart. A task that was already running is marked `interrupted` instead of being submitted twice.

## Deployment notes

- Run exactly one Uvicorn worker. SQLite also prevents a second process from claiming another job while one is already marked `running`.
- Deploy the gateway beside InfiniteTalk and connect through `127.0.0.1:7860` to avoid uploading large media through the public network twice.
- The upstream Gradio port is still unauthenticated. For production, bind it to localhost or restrict it with the platform firewall so only this gateway is public.
- The first release intentionally supports video-driven avatars only. Voice cloning and speech synthesis remain separate ContentPlane providers; the gateway receives only the final driving audio.

### Compshare image

The Compshare image runs every `/start.d/*.sh` file when the container starts. After cloning and installing the gateway, link the versioned startup script into that directory:

```bash
ln -s /root/contentplane-avatar-gateway/deploy/compshare-start.sh \
  /start.d/contentplane-avatar-gateway.sh
```

Keep the production token outside Git in `/root/contentplane-avatar-gateway/.env`, and make it readable only by root:

```bash
chmod 600 /root/contentplane-avatar-gateway/.env
```

## Verify

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```
