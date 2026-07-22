#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly RUNTIME_DIR="${REPO_ROOT}/.data/connected"
readonly API_LOG="${RUNTIME_DIR}/api.log"
readonly ACTIONS_LOG="${RUNTIME_DIR}/datahub-actions.log"
readonly WEB_LOG="${RUNTIME_DIR}/web.log"

api_pid=""
actions_pid=""
web_pid=""

cd "${REPO_ROOT}"

load_dotenv() {
  [[ -f .env ]] || return 0
  while IFS= read -r -d '' assignment; do
    export "${assignment}"
  done < <(
    uv run python - <<'PY'
import os
import sys

from dotenv import dotenv_values

for key, value in dotenv_values(".env").items():
    if value is not None and key not in os.environ:
        sys.stdout.buffer.write(f"{key}={value}".encode() + b"\0")
PY
  )
}

require_value() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "Connected demo requires ${name}. Set it in .env or export it." >&2
    exit 2
  fi
}

preflight() {
  command -v curl >/dev/null 2>&1 || {
    echo "Connected demo requires curl." >&2
    exit 2
  }
  command -v gh >/dev/null 2>&1 || {
    echo "Connected demo requires the GitHub CLI (gh)." >&2
    exit 2
  }
  command -v git >/dev/null 2>&1 || {
    echo "Connected demo requires git." >&2
    exit 2
  }

  export DATARESCUE_API_URL="http://127.0.0.1:8000"
  export DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://127.0.0.1:8080}"
  export DATARESCUE_DATAHUB_GMS_URL="${DATARESCUE_DATAHUB_GMS_URL:-${DATAHUB_GMS_URL}}"
  export DATAHUB_KAFKA_BOOTSTRAP="${DATAHUB_KAFKA_BOOTSTRAP:-127.0.0.1:9092}"
  export DATAHUB_SCHEMA_REGISTRY_URL="${DATAHUB_SCHEMA_REGISTRY_URL:-http://127.0.0.1:8081}"
  export DATAHUB_MCL_TOPIC="${DATAHUB_MCL_TOPIC:-MetadataChangeLog_Versioned_v1}"
  export DATARESCUE_DATAHUB_TOKEN="${DATARESCUE_DATAHUB_TOKEN:-${DATAHUB_TOKEN:-}}"
  export DATARESCUE_OPENAI_API_KEY="${DATARESCUE_OPENAI_API_KEY:-${OPENAI_API_KEY:-}}"
  if [[ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]]; then
    export GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
  else
    unset GH_TOKEN
  fi
  export DATARESCUE_GITHUB_REPOSITORY="${DATARESCUE_GITHUB_REPOSITORY:-girginomer10/datarescue}"
  export DATARESCUE_GITHUB_REPO_ROOT="${DATARESCUE_GITHUB_REPO_ROOT:-${REPO_ROOT}}"

  require_value DATARESCUE_DATAHUB_MCP_URL
  require_value DATARESCUE_OPENAI_API_KEY
  require_value DATARESCUE_GITHUB_REPOSITORY
  require_value DATAHUB_KAFKA_BOOTSTRAP
  require_value DATAHUB_SCHEMA_REGISTRY_URL

  export DATARESCUE_REPLAY_MODE=false
  export DATARESCUE_EXECUTION_MODE=postgres
  export DATARESCUE_RUNTIME_DIR="${RUNTIME_DIR}/runtime"
  export DATARESCUE_DATABASE_PATH="${RUNTIME_DIR}/state.sqlite3"
  export DATARESCUE_POSTGRES_DSN="${DATARESCUE_POSTGRES_DSN:-postgresql://${POSTGRES_USER:-datarescue}:${POSTGRES_PASSWORD:-datarescue}@${POSTGRES_HOST:-127.0.0.1}:${POSTGRES_PORT:-55432}/${POSTGRES_DB:-datarescue}}"
  export DATARESCUE_GITHUB_WRITE_ENABLED=true

  gh auth status >/dev/null
  gh repo view "${DATARESCUE_GITHUB_REPOSITORY}" --json nameWithOwner >/dev/null
  git -C "${DATARESCUE_GITHUB_REPO_ROOT}" rev-parse --verify main >/dev/null
  bash scripts/datahub-actions.sh validate
}

check_mcp_context() {
  uv run python - <<'PY'
import os

from apps.api.config import CANONICAL_ASSET_URN
from apps.api.models import IntegrationStatus
from packages.datahub.mcp import DataHubMCPAdapter

result = DataHubMCPAdapter(
    endpoint=os.environ["DATARESCUE_DATAHUB_MCP_URL"],
    token=os.environ.get("DATARESCUE_DATAHUB_TOKEN") or None,
).fetch_context(CANONICAL_ASSET_URN)
if result.integration.status is not IntegrationStatus.SUCCEEDED:
    raise SystemExit(f"DataHub MCP preflight failed: {result.integration.message}")
print("DataHub MCP returned live context for the monitored asset.")
PY
}

wait_for_api() {
  local response=""
  for _attempt in $(seq 1 60); do
    if response="$(curl --fail --silent "${DATARESCUE_API_URL}/health" 2>/dev/null)"; then
      RESPONSE="${response}" uv run python - <<'PY'
import json
import os

health = json.loads(os.environ["RESPONSE"])
if health != {"status": "ok", "mode": "postgres"}:
    raise SystemExit(f"Unexpected API health payload: {health!r}")
PY
      echo "DataRescue API is ready in postgres mode."
      return
    fi
    if ! kill -0 "${api_pid}" 2>/dev/null; then
      echo "DataRescue API exited before becoming ready." >&2
      tail -n 80 "${API_LOG}" >&2 || true
      exit 1
    fi
    sleep 1
  done
  echo "DataRescue API did not become ready in 60 seconds." >&2
  tail -n 80 "${API_LOG}" >&2 || true
  exit 1
}

wait_for_actions() {
  local marker="Subscribing to the following topics"
  for _attempt in $(seq 1 90); do
    if grep -Fq "${marker}" "${ACTIONS_LOG}" 2>/dev/null; then
      bash scripts/datahub-actions.sh ready
      echo "DataHub Actions subscribed and received a Kafka group assignment."
      return
    fi
    if ! kill -0 "${actions_pid}" 2>/dev/null; then
      echo "DataHub Actions exited before subscribing." >&2
      tail -n 120 "${ACTIONS_LOG}" >&2 || true
      exit 1
    fi
    sleep 1
  done
  echo "DataHub Actions did not subscribe in 90 seconds." >&2
  tail -n 120 "${ACTIONS_LOG}" >&2 || true
  exit 1
}

cleanup() {
  [[ -z "${web_pid}" ]] || kill "${web_pid}" 2>/dev/null || true
  [[ -z "${actions_pid}" ]] || kill -INT "${actions_pid}" 2>/dev/null || true
  [[ -z "${api_pid}" ]] || kill "${api_pid}" 2>/dev/null || true
  [[ -z "${web_pid}" ]] || wait "${web_pid}" 2>/dev/null || true
  [[ -z "${actions_pid}" ]] || wait "${actions_pid}" 2>/dev/null || true
  [[ -z "${api_pid}" ]] || wait "${api_pid}" 2>/dev/null || true
}

load_dotenv
preflight
mkdir -p "${RUNTIME_DIR}"
trap cleanup EXIT INT TERM

make datahub-up
echo "Ingesting the healthy baseline before the schema drift consumer starts."
make datahub-seed-healthy
check_mcp_context

uv run uvicorn apps.api.main:app --host 0.0.0.0 --port 8000 >"${API_LOG}" 2>&1 &
api_pid=$!
wait_for_api

bash scripts/datahub-actions.sh run >"${ACTIONS_LOG}" 2>&1 &
actions_pid=$!
wait_for_actions

echo "Consumer is ready; applying drift and ingesting the changed schema."
make datahub-apply-drift

apps/web/node_modules/.bin/vite --host 0.0.0.0 --port 5173 >"${WEB_LOG}" 2>&1 &
web_pid=$!
echo "Connected DataRescue is running at http://127.0.0.1:5173"
echo "Logs: ${API_LOG}, ${ACTIONS_LOG}, ${WEB_LOG}"
wait -n "${api_pid}" "${actions_pid}" "${web_pid}"
