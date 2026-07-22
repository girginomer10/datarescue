#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly RUNTIME_DIR="${REPO_ROOT}/.data/connected"
readonly API_LOG="${RUNTIME_DIR}/api.log"
readonly ACTIONS_LOG="${RUNTIME_DIR}/datahub-actions.log"
readonly WEB_LOG="${RUNTIME_DIR}/web.log"
source "${REPO_ROOT}/scripts/api-process-identity.sh"

api_pid=""
actions_pid=""
web_pid=""
mcp_started="false"
manage_mcp="false"
api_host=""
api_port=""
api_run_nonce=""
api_state_identity=""
api_database_path=""

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

configure_api_identity() {
  datarescue_configure_api_identity "${DATARESCUE_API_URL}" "${api_database_path}"
  api_host="${DATARESCUE_API_HOST_RESULT}"
  api_port="${DATARESCUE_API_PORT_RESULT}"
  api_run_nonce="${DATARESCUE_API_RUN_NONCE_RESULT}"
  api_state_identity="${DATARESCUE_API_STATE_IDENTITY_RESULT}"
}

assert_api_port_available() {
  datarescue_assert_api_port_available "${api_host}" "${api_port}"
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
  command -v python3 >/dev/null 2>&1 || {
    echo "Connected demo requires python3." >&2
    exit 2
  }

  export DATARESCUE_API_URL="${DATARESCUE_API_URL:-http://127.0.0.1:8000}"
  export DATAHUB_MAPPED_GMS_PORT="${DATAHUB_MAPPED_GMS_PORT:-18080}"
  export DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://127.0.0.1:${DATAHUB_MAPPED_GMS_PORT}}"
  export DATAHUB_GMS_URL_CONTAINER="${DATAHUB_GMS_URL_CONTAINER:-http://host.docker.internal:${DATAHUB_MAPPED_GMS_PORT}}"
  export DATARESCUE_DATAHUB_GMS_URL="${DATARESCUE_DATAHUB_GMS_URL:-${DATAHUB_GMS_URL}}"
  export DATAHUB_KAFKA_BOOTSTRAP="${DATAHUB_KAFKA_BOOTSTRAP:-127.0.0.1:9092}"
  export DATAHUB_SCHEMA_REGISTRY_URL="${DATAHUB_SCHEMA_REGISTRY_URL:-${DATAHUB_GMS_URL}/schema-registry/api/}"
  export DATAHUB_MCL_TOPIC="${DATAHUB_MCL_TOPIC:-MetadataChangeLog_Versioned_v1}"
  # A fresh group begins at the healthy baseline's log end. Reusing a prior
  # group's offsets could replay an old transition into the newly reset scope.
  export DATAHUB_ACTIONS_GROUP="${DATAHUB_ACTIONS_GROUP:-datarescue-connected-$$}"
  if [[ -z "${DATAHUB_TOKEN:-}" && -n "${DATARESCUE_DATAHUB_TOKEN:-}" ]]; then
    export DATAHUB_TOKEN="${DATARESCUE_DATAHUB_TOKEN}"
  elif [[ -z "${DATARESCUE_DATAHUB_TOKEN:-}" && -n "${DATAHUB_TOKEN:-}" ]]; then
    export DATARESCUE_DATAHUB_TOKEN="${DATAHUB_TOKEN}"
  fi
  export DATAHUB_MCP_HOST="${DATAHUB_MCP_HOST:-127.0.0.1}"
  export DATAHUB_MCP_PORT="${DATAHUB_MCP_PORT:-8001}"
  if [[ -z "${DATARESCUE_DATAHUB_MCP_URL:-}" ]]; then
    export DATARESCUE_DATAHUB_MCP_URL="http://${DATAHUB_MCP_HOST}:${DATAHUB_MCP_PORT}/mcp"
    manage_mcp="true"
  fi
  export DATARESCUE_OPENAI_API_KEY="${DATARESCUE_OPENAI_API_KEY:-${OPENAI_API_KEY:-}}"
  if [[ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]]; then
    export GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
  else
    unset GH_TOKEN
  fi
  export DATARESCUE_GITHUB_REPOSITORY="${DATARESCUE_GITHUB_REPOSITORY:-girginomer10/datarescue}"
  export DATARESCUE_GITHUB_REPO_ROOT="${DATARESCUE_GITHUB_REPO_ROOT:-${REPO_ROOT}}"
  export DATARESCUE_CONNECTED_PROOF_TIMEOUT_SECONDS="${DATARESCUE_CONNECTED_PROOF_TIMEOUT_SECONDS:-600}"

  require_value DATARESCUE_OPENAI_API_KEY
  require_value DATARESCUE_GITHUB_REPOSITORY
  require_value DATAHUB_KAFKA_BOOTSTRAP
  require_value DATAHUB_SCHEMA_REGISTRY_URL

  export DATARESCUE_REPLAY_MODE=false
  export DATARESCUE_EXECUTION_MODE=postgres
  export DATARESCUE_RUNTIME_DIR="${RUNTIME_DIR}/runtime"
  api_database_path="${RUNTIME_DIR}/state.sqlite3"
  export DATARESCUE_DATABASE_PATH="${api_database_path}"
  export DATARESCUE_POSTGRES_DSN="${DATARESCUE_POSTGRES_DSN:-postgresql://${POSTGRES_USER:-datarescue}:${POSTGRES_PASSWORD:-datarescue}@${POSTGRES_HOST:-127.0.0.1}:${POSTGRES_PORT:-55432}/${POSTGRES_DB:-datarescue}}"
  export DATARESCUE_GITHUB_WRITE_ENABLED=true

  gh auth status >/dev/null
  gh repo view "${DATARESCUE_GITHUB_REPOSITORY}" --json nameWithOwner >/dev/null
  git -C "${DATARESCUE_GITHUB_REPO_ROOT}" rev-parse --verify main >/dev/null
  bash scripts/datahub-actions.sh validate
  configure_api_identity
  assert_api_port_available
}

check_mcp_context() {
  uv run python - <<'PY'
import os

from apps.api.config import CANONICAL_ASSET_URN
from apps.api.models import IntegrationStatus
from packages.datahub.mcp import (
    CANONICAL_CONTEXT_DOCUMENT_URN,
    CANONICAL_REQUIRED_LINEAGE_URNS,
    DataHubMCPAdapter,
)

result = DataHubMCPAdapter(
    endpoint=os.environ["DATARESCUE_DATAHUB_MCP_URL"],
    token=os.environ.get("DATARESCUE_DATAHUB_MCP_TOKEN") or None,
    gms_url=os.environ["DATARESCUE_DATAHUB_GMS_URL"],
    gms_token=os.environ.get("DATARESCUE_DATAHUB_TOKEN") or None,
).fetch_context(CANONICAL_ASSET_URN)
if result.integration.status is not IntegrationStatus.SUCCEEDED:
    raise SystemExit(f"DataHub MCP preflight failed: {result.integration.message}")
context = result.context
fields = context.get("schema_fields")
if not isinstance(fields, list):
    raise SystemExit("DataHub MCP preflight returned no schema fields")
field_names = {
    field.get("fieldPath")
    for field in fields
    if isinstance(field, dict) and isinstance(field.get("fieldPath"), str)
}
common = {"payment_id", "customer_id", "paid_at", "currency", "status"}
healthy = "amount" in field_names and not {"gross_amount", "net_amount"} & field_names
drifted = "amount" not in field_names and {"gross_amount", "net_amount"}.issubset(
    field_names
)
lineage = context.get("lineage_urns")
glossary = context.get("glossary_definition")
documents = context.get("context_documents")
if context.get("asset_urn") != CANONICAL_ASSET_URN:
    raise SystemExit("DataHub MCP preflight returned the wrong monitored asset")
if not common.issubset(field_names) or not (healthy or drifted):
    raise SystemExit("DataHub MCP preflight returned an incomplete schema contract")
if context.get("owner") != "Finance Data":
    raise SystemExit("DataHub MCP preflight returned an unexpected owner")
if not isinstance(glossary, str) or not all(
    token in glossary for token in ("net_amount", "gross_amount")
):
    raise SystemExit("DataHub MCP preflight returned no exact NetRevenue rule")
if not isinstance(documents, list) or CANONICAL_CONTEXT_DOCUMENT_URN not in documents:
    raise SystemExit("DataHub MCP preflight returned no canonical context document")
if (
    not isinstance(lineage, list)
    or not CANONICAL_REQUIRED_LINEAGE_URNS.issubset(lineage)
    or context.get("lineage_current") is not True
):
    raise SystemExit("DataHub MCP preflight returned stale or incomplete lineage")
print("DataHub MCP returned the complete live semantic contract.")
PY
}

wait_for_api() {
  for _attempt in $(seq 1 60); do
    if ! kill -0 "${api_pid}" 2>/dev/null; then
      echo "DataRescue API exited before becoming ready." >&2
      tail -n 80 "${API_LOG}" >&2 || true
      exit 1
    fi
    if datarescue_api_identity_ready \
      "${DATARESCUE_API_URL}" "${api_run_nonce}" "${api_state_identity}"; then
      kill -0 "${api_pid}" 2>/dev/null || {
        echo "DataRescue API exited while its identity was being verified." >&2
        exit 1
      }
      echo "DataRescue API is ready in postgres mode."
      return
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
  if [[ "${mcp_started}" == "true" ]]; then
    DATAHUB_MCP_HOST="${DATAHUB_MCP_HOST}" DATAHUB_MCP_PORT="${DATAHUB_MCP_PORT}" \
      bash scripts/datahub-mcp.sh stop >/dev/null 2>&1 || true
  fi
}

load_dotenv
preflight
mkdir -p "${RUNTIME_DIR}"
trap cleanup EXIT INT TERM

make datahub-up
echo "Ingesting the healthy baseline before the schema drift consumer starts."
make datahub-seed-healthy
if [[ "${manage_mcp}" == "true" ]] && ! \
  DATAHUB_MCP_HOST="${DATAHUB_MCP_HOST}" DATAHUB_MCP_PORT="${DATAHUB_MCP_PORT}" \
    bash scripts/datahub-mcp.sh status >/dev/null 2>&1; then
  DATAHUB_GMS_URL="${DATAHUB_GMS_URL}" DATAHUB_MCP_HOST="${DATAHUB_MCP_HOST}" \
    DATAHUB_MCP_PORT="${DATAHUB_MCP_PORT}" bash scripts/datahub-mcp.sh start
  mcp_started="true"
fi
check_mcp_context

assert_api_port_available
uv run uvicorn apps.api.main:app --host "${api_host}" --port "${api_port}" \
  --header "X-DataRescue-Run-Nonce:${api_run_nonce}" \
  --header "X-DataRescue-State-Identity:${api_state_identity}" \
  >"${API_LOG}" 2>&1 &
api_pid=$!
wait_for_api

curl --fail --silent --show-error --request POST \
  "${DATARESCUE_API_URL}/api/v1/demo/reset" >/dev/null
echo "Connected proof scope reset; historical evidence remains append-only."

bash scripts/datahub-actions.sh run >"${ACTIONS_LOG}" 2>&1 &
actions_pid=$!
wait_for_actions

echo "Consumer is ready; applying drift and ingesting the changed schema."
make datahub-apply-drift

uv run python scripts/verify-connected-case.py \
  --api-url "${DATARESCUE_API_URL}" \
  --timeout "${DATARESCUE_CONNECTED_PROOF_TIMEOUT_SECONDS}"
echo "Connected first slice proved one live PR_OPEN case; the incident remains ACTIVE."

apps/web/node_modules/.bin/vite --host 0.0.0.0 --port 5173 >"${WEB_LOG}" 2>&1 &
web_pid=$!
echo "Connected DataRescue is running at http://127.0.0.1:5173"
echo "Logs: ${API_LOG}, ${ACTIONS_LOG}, ${WEB_LOG}"
wait -n "${api_pid}" "${actions_pid}" "${web_pid}"
