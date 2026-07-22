#!/usr/bin/env bash
set -euo pipefail

# A deliberately small Docker CLI runner for contributors whose Docker daemon
# works but whose Compose plugin is unavailable. It manages only the fixed
# DataRescue demo resources below; docker-compose.yml remains the canonical
# declarative definition for environments with Compose.

readonly POSTGRES_CONTAINER="datarescue-postgres"
readonly POSTGRES_VOLUME="datarescue-postgres-data"
readonly DEMO_NETWORK="datarescue-demo"

readonly POSTGRES_DB="${POSTGRES_DB:-datarescue}"
readonly POSTGRES_USER="${POSTGRES_USER:-datarescue}"
readonly POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-datarescue}"
readonly POSTGRES_PORT="${POSTGRES_PORT:-55432}"
readonly DATAHUB_INGESTION_IMAGE="${DATAHUB_INGESTION_IMAGE:-acryldata/datahub-ingestion:v1.6.0}"
readonly DATAHUB_GMS_URL_CONTAINER="${DATAHUB_GMS_URL_CONTAINER:-http://host.docker.internal:8080}"
readonly DATAHUB_TOKEN="${DATAHUB_TOKEN:-}"
readonly DATAHUB_INGEST_DRY_RUN="${DATAHUB_INGEST_DRY_RUN:-0}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docker_config_fallback=""

prepare_docker_client() {
  local docker_config_file="${DOCKER_CONFIG:-${HOME}/.docker}/config.json"
  local credential_store=""
  local current_context=""
  local current_host=""

  if [[ -f "${docker_config_file}" ]]; then
    credential_store="$(
      sed -n 's/.*"credsStore"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        "${docker_config_file}" | head -n 1
    )"
  fi

  # A removed Docker Desktop can leave a broken credential-helper entry while
  # another engine (for example Colima) remains healthy. Preserve the selected
  # engine endpoint and use an isolated empty client config for public images.
  if [[ -n "${credential_store}" ]] && ! command -v "docker-credential-${credential_store}" >/dev/null 2>&1; then
    current_context="$(docker context show)"
    current_host="$(docker context inspect "${current_context}" --format '{{.Endpoints.docker.Host}}')"
    docker_config_fallback="$(mktemp -d "${TMPDIR:-/tmp}/datarescue-docker.XXXXXX")"
    export DOCKER_HOST="${current_host}"
    export DOCKER_CONFIG="${docker_config_fallback}"
    trap 'rm -rf -- "${docker_config_fallback}"' EXIT
  fi
}

prepare_docker_client

require_docker() {
  command -v docker >/dev/null 2>&1 || {
    echo "Docker CLI is required." >&2
    exit 1
  }
  docker info >/dev/null 2>&1 || {
    echo "Docker daemon is not reachable." >&2
    exit 1
  }
}

ensure_network() {
  docker network inspect "${DEMO_NETWORK}" >/dev/null 2>&1 || \
    docker network create "${DEMO_NETWORK}" >/dev/null
}

postgres_up() {
  require_docker
  ensure_network
  docker volume inspect "${POSTGRES_VOLUME}" >/dev/null 2>&1 || \
    docker volume create "${POSTGRES_VOLUME}" >/dev/null

  if docker container inspect "${POSTGRES_CONTAINER}" >/dev/null 2>&1; then
    if [[ "$(docker inspect -f '{{.State.Running}}' "${POSTGRES_CONTAINER}")" != "true" ]]; then
      docker start "${POSTGRES_CONTAINER}" >/dev/null
    fi
    return
  fi

  docker run --detach \
    --name "${POSTGRES_CONTAINER}" \
    --network "${DEMO_NETWORK}" \
    --publish "${POSTGRES_PORT}:5432" \
    --env "POSTGRES_DB=${POSTGRES_DB}" \
    --env "POSTGRES_USER=${POSTGRES_USER}" \
    --env "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}" \
    --volume "${POSTGRES_VOLUME}:/var/lib/postgresql/data" \
    --volume "${repo_root}/demo/postgres/init:/docker-entrypoint-initdb.d:ro" \
    --health-cmd "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}" \
    --health-interval 2s \
    --health-timeout 3s \
    --health-retries 30 \
    postgres:16-alpine >/dev/null
}

postgres_wait() {
  local container_logs=""
  for _attempt in $(seq 1 30); do
    # The official image briefly exposes its temporary initialization server.
    # Do not accept that transient pg_isready success: wait until entrypoint
    # initialization has completed (or was skipped for an existing volume),
    # then prove the initialized fixture is reachable on the final server.
    container_logs="$(docker logs "${POSTGRES_CONTAINER}" 2>&1 || true)"
    if ! grep -Eq \
      'PostgreSQL init process complete; ready for start up|Skipping initialization' \
      <<<"${container_logs}"; then
      sleep 1
      continue
    fi
    if docker exec "${POSTGRES_CONTAINER}" \
      pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1 && \
      [[ "$(docker exec "${POSTGRES_CONTAINER}" psql -X -A -t \
        -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
        -c "SELECT CASE WHEN to_regclass('audit.payments_fct_last_good') IS NOT NULL THEN 1 ELSE 0 END" \
        2>/dev/null | tr -d '[:space:]')" == "1" ]]; then
      echo "PostgreSQL is ready."
      return
    fi
    sleep 1
  done
  echo "PostgreSQL did not become ready in 30 seconds." >&2
  exit 1
}

postgres_down() {
  require_docker
  if docker container inspect "${POSTGRES_CONTAINER}" >/dev/null 2>&1; then
    docker rm --force "${POSTGRES_CONTAINER}" >/dev/null
  fi
}

postgres_logs() {
  require_docker
  docker logs --follow "${POSTGRES_CONTAINER}"
}

psql_exec() {
  require_docker
  docker exec --interactive \
    "${POSTGRES_CONTAINER}" \
    psql -X -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" "$@"
}

datahub_ingest_postgres() {
  local -a run_options=()
  require_docker
  ensure_network
  if [[ "${DATAHUB_INGEST_DRY_RUN}" == "1" ]]; then
    run_options=(--dry-run --no-default-report)
  fi
  docker run --rm \
    --network "${DEMO_NETWORK}" \
    --add-host host.docker.internal:host-gateway \
    --env POSTGRES_HOST="${POSTGRES_CONTAINER}" \
    --env POSTGRES_PORT=5432 \
    --env POSTGRES_DB="${POSTGRES_DB}" \
    --env POSTGRES_USER="${POSTGRES_USER}" \
    --env POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
    --env DATAHUB_GMS_URL="${DATAHUB_GMS_URL_CONTAINER}" \
    --env DATAHUB_TOKEN="${DATAHUB_TOKEN}" \
    --volume "${repo_root}/demo/datahub:/recipes:ro" \
    "${DATAHUB_INGESTION_IMAGE}" ingest run -c /recipes/postgres-ingestion.yml "${run_options[@]}"
}

datahub_ingest_dbt() {
  local -a run_options=()
  require_docker
  if [[ "${DATAHUB_INGEST_DRY_RUN}" == "1" ]]; then
    run_options=(--dry-run --no-default-report)
  fi
  docker run --rm \
    --add-host host.docker.internal:host-gateway \
    --env DATAHUB_GMS_URL="${DATAHUB_GMS_URL_CONTAINER}" \
    --env DATAHUB_TOKEN="${DATAHUB_TOKEN}" \
    --env DBT_ARTIFACT_ROOT=/workspace/demo/dbt/target \
    --volume "${repo_root}:/workspace:ro" \
    "${DATAHUB_INGESTION_IMAGE}" ingest run -c /workspace/demo/datahub/dbt-ingestion.yml "${run_options[@]}"
}

clean_all() {
  postgres_down
  if docker volume inspect "${POSTGRES_VOLUME}" >/dev/null 2>&1; then
    docker volume rm "${POSTGRES_VOLUME}" >/dev/null
  fi
  if docker network inspect "${DEMO_NETWORK}" >/dev/null 2>&1; then
    docker network rm "${DEMO_NETWORK}" >/dev/null
  fi
}

case "${1:-}" in
  postgres-up)
    postgres_up
    postgres_wait
    ;;
  postgres-down)
    postgres_down
    ;;
  postgres-logs)
    postgres_logs
    ;;
  psql)
    shift
    psql_exec "$@"
    ;;
  datahub-ingest-postgres)
    datahub_ingest_postgres
    ;;
  datahub-ingest-dbt)
    datahub_ingest_dbt
    ;;
  clean)
    clean_all
    ;;
  *)
    echo "Usage: $0 {postgres-up|postgres-down|postgres-logs|psql|datahub-ingest-postgres|datahub-ingest-dbt|clean}" >&2
    exit 2
    ;;
esac
