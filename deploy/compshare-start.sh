#!/usr/bin/env bash
set -euo pipefail

GATEWAY_ROOT="${CONTENTPLANE_GATEWAY_ROOT:-/root/contentplane-avatar-gateway}"
ENV_FILE="${CONTENTPLANE_GATEWAY_ENV_FILE:-${GATEWAY_ROOT}/.env}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing gateway environment file: ${ENV_FILE}" >&2
  exit 1
fi

set -a
source "${ENV_FILE}"
set +a

cd "${GATEWAY_ROOT}"
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${GATEWAY_PORT:-8787}" --workers 1
