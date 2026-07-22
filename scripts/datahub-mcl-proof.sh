#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly PROOF_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/datarescue-mcl-proof.XXXXXX")"
readonly API_LOG="${PROOF_ROOT}/api.log"
readonly ACTIONS_LOG="${PROOF_ROOT}/actions.log"
readonly BASELINE="${PROOF_ROOT}/case-baseline.json"
readonly OFFSETS="${PROOF_ROOT}/pre-drift-offsets.json"
readonly API_DATABASE_PATH="${PROOF_ROOT}/state.sqlite3"
source "${REPO_ROOT}/scripts/api-process-identity.sh"

api_pid=""
actions_pid=""
api_host=""
api_port=""
api_run_nonce=""
api_state_identity=""

cd "${REPO_ROOT}"

export DATAHUB_MAPPED_GMS_PORT="${DATAHUB_MAPPED_GMS_PORT:-18080}"
export DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://127.0.0.1:${DATAHUB_MAPPED_GMS_PORT}}"
export DATAHUB_GMS_URL_CONTAINER="${DATAHUB_GMS_URL_CONTAINER:-http://host.docker.internal:${DATAHUB_MAPPED_GMS_PORT}}"
export DATAHUB_KAFKA_BOOTSTRAP="${DATAHUB_KAFKA_BOOTSTRAP:-127.0.0.1:9092}"
export DATAHUB_SCHEMA_REGISTRY_URL="${DATAHUB_SCHEMA_REGISTRY_URL:-${DATAHUB_GMS_URL}/schema-registry/api/}"
export DATAHUB_MCL_TOPIC="${DATAHUB_MCL_TOPIC:-MetadataChangeLog_Versioned_v1}"
if [[ -z "${DATAHUB_TOKEN:-}" && -n "${DATARESCUE_DATAHUB_TOKEN:-}" ]]; then
  export DATAHUB_TOKEN="${DATARESCUE_DATAHUB_TOKEN}"
elif [[ -z "${DATARESCUE_DATAHUB_TOKEN:-}" && -n "${DATAHUB_TOKEN:-}" ]]; then
  export DATARESCUE_DATAHUB_TOKEN="${DATAHUB_TOKEN}"
fi
# A fresh group starts exactly at the post-baseline log end. Reusing a named
# group could replay a prior run's healthy-reset MCL into this run's fresh API.
export DATAHUB_ACTIONS_GROUP="${DATAHUB_ACTIONS_GROUP:-datarescue-mcl-proof-$$}"
export DATARESCUE_API_URL="${DATARESCUE_API_URL:-http://127.0.0.1:8000}"

configure_api_identity() {
  datarescue_configure_api_identity "${DATARESCUE_API_URL}" "${API_DATABASE_PATH}"
  api_host="${DATARESCUE_API_HOST_RESULT}"
  api_port="${DATARESCUE_API_PORT_RESULT}"
  api_run_nonce="${DATARESCUE_API_RUN_NONCE_RESULT}"
  api_state_identity="${DATARESCUE_API_STATE_IDENTITY_RESULT}"
}

assert_api_port_available() {
  datarescue_assert_api_port_available "${api_host}" "${api_port}"
}

cleanup_children() {
  [[ -z "${actions_pid}" ]] || kill -INT "${actions_pid}" 2>/dev/null || true
  [[ -z "${api_pid}" ]] || kill "${api_pid}" 2>/dev/null || true
  [[ -z "${actions_pid}" ]] || wait "${actions_pid}" 2>/dev/null || true
  [[ -z "${api_pid}" ]] || wait "${api_pid}" 2>/dev/null || true
  actions_pid=""
  api_pid=""
}

cleanup() {
  cleanup_children
  rm -rf -- "${PROOF_ROOT}"
}
trap cleanup EXIT INT TERM

wait_for_api() {
  for _attempt in $(seq 1 60); do
    if ! kill -0 "${api_pid}" 2>/dev/null; then
      echo "DataRescue API exited before it became ready." >&2
      tail -n 80 "${API_LOG}" >&2 || true
      exit 1
    fi
    if datarescue_api_identity_ready \
      "${DATARESCUE_API_URL}" "${api_run_nonce}" "${api_state_identity}"; then
      kill -0 "${api_pid}" 2>/dev/null || {
        echo "DataRescue API exited while its identity was being verified." >&2
        exit 1
      }
      return
    fi
    sleep 1
  done
  echo "DataRescue API did not become ready." >&2
  tail -n 80 "${API_LOG}" >&2 || true
  exit 1
}

start_api() {
  assert_api_port_available
  DATARESCUE_REPLAY_MODE=false \
    DATARESCUE_EXECUTION_MODE=postgres \
    DATARESCUE_RUNTIME_DIR="${PROOF_ROOT}/runtime" \
    DATARESCUE_DATABASE_PATH="${API_DATABASE_PATH}" \
    DATARESCUE_POSTGRES_DSN="postgresql://${POSTGRES_USER:-datarescue}:${POSTGRES_PASSWORD:-datarescue}@${POSTGRES_HOST:-127.0.0.1}:${POSTGRES_PORT:-55432}/${POSTGRES_DB:-datarescue}" \
    DATARESCUE_DATAHUB_GMS_URL="${DATAHUB_GMS_URL}" \
    DATARESCUE_DATAHUB_TOKEN="${DATAHUB_TOKEN:-}" \
    DATARESCUE_DATAHUB_MCP_URL= \
    DATARESCUE_OPENAI_API_KEY= \
    DATARESCUE_GITHUB_WRITE_ENABLED=false \
    uv run uvicorn apps.api.main:app --host "${api_host}" --port "${api_port}" \
      --header "X-DataRescue-Run-Nonce:${api_run_nonce}" \
      --header "X-DataRescue-State-Identity:${api_state_identity}" \
      >"${API_LOG}" 2>&1 &
  api_pid=$!
  wait_for_api
}

start_actions() {
  : >"${ACTIONS_LOG}"
  bash scripts/datahub-actions.sh run >"${ACTIONS_LOG}" 2>&1 &
  actions_pid=$!
  bash scripts/datahub-actions.sh ready
}

stop_children_for_restart() {
  cleanup_children
  sleep 2
}

command -v docker >/dev/null 2>&1 || { echo "Docker is required." >&2; exit 2; }
command -v curl >/dev/null 2>&1 || { echo "curl is required." >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required." >&2; exit 2; }
configure_api_identity

make datahub-actions-validate
make datahub-up
curl --fail --silent --show-error "${DATAHUB_GMS_URL}/config" >/dev/null
curl --fail --silent --show-error "${DATAHUB_SCHEMA_REGISTRY_URL%/}/subjects" >/dev/null

echo "Ingesting a healthy schema before the consumer starts."
make datahub-seed-healthy
start_api
start_actions
bash scripts/datahub-actions.sh capture-end "${OFFSETS}"

echo "Applying amount -> gross_amount + net_amount and waiting for the real MCL."
make demo-drift
bash scripts/demo-runtime.sh datahub-ingest-postgres
uv run python scripts/verify-datahub-mcl-proof.py \
  --api-url "${DATARESCUE_API_URL}" --write-baseline "${BASELINE}"
bash scripts/datahub-actions.sh caught-up

echo "Restarting the API and replaying the same Kafka range to prove durable deduplication."
stop_children_for_restart
start_api
bash scripts/datahub-actions.sh restore "${OFFSETS}"
start_actions
bash scripts/datahub-actions.sh caught-up
uv run python scripts/verify-datahub-mcl-proof.py \
  --api-url "${DATARESCUE_API_URL}" --expect-unchanged "${BASELINE}"

echo "DataHub MCL proof passed: automatic case, live ACTIVE incident, fail-closed containment, restart-safe deduplication."
