#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR_DEFAULT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ROOT_DIR="${BUNDLE_STACK_ROOT_DIR:-${ROOT_DIR_DEFAULT}}"

BUNDLE_META_FILE="${ROOT_DIR}/bundle.meta.env"
ENV_FILE="${ROOT_DIR}/.env"
LLM_FILE="${ROOT_DIR}/llm_models.yml"
IMAGES_TAR="${ROOT_DIR}/images.tar"
INIT_SQL_FILE="${ROOT_DIR}/init.sql"
RUNNER_POOL_COMPOSE="${ROOT_DIR}/docker-compose.runner-pool.generated.yml"
REDIS_DB_RESOLVER="${ROOT_DIR}/scripts/resolve_redis_databases.sh"

# shellcheck disable=SC1091
source "${ROOT_DIR}/scripts/container_env.sh"

COMPOSE_FILES=(
  -f docker-compose.kafka.yml
  -f docker-compose.master-api.yml
  -f docker-compose.runner-pool.generated.yml
  -f docker-compose.web-ui.yml
)

log() {
  printf '[bundle-stack] %s\n' "$*"
}

die() {
  printf '[bundle-stack] ERROR: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

ensure_env_file() {
  [ -f "${ENV_FILE}" ] || die ".env is required in bundle directory"
}

load_bundle_metadata() {
  if [ -f "${BUNDLE_META_FILE}" ]; then
    set -a
    # shellcheck disable=SC1091
    . "${BUNDLE_META_FILE}"
    set +a
  fi
}

resolve_project_name() {
  local env_project="${COMPOSE_PROJECT_NAME:-}"
  local meta_project="${BUNDLE_COMPOSE_PROJECT_NAME:-}"

  [ -n "${meta_project}" ] || die "BUNDLE_COMPOSE_PROJECT_NAME is required in bundle metadata"

  if [ -n "${env_project}" ] && [ "${env_project}" != "${meta_project}" ]; then
    die "COMPOSE_PROJECT_NAME='${env_project}' does not match bundle metadata '${meta_project}'"
  fi

  PROJECT_NAME="${meta_project}"
  container_env_resolve "${PROJECT_NAME}"
}

compose() {
  docker compose "${COMPOSE_FILES[@]}" "$@"
}

ensure_llm_file() {
  [ -f "${LLM_FILE}" ] || die "llm_models.yml is required in bundle directory"
}

ensure_images_tar_file() {
  [ -f "${IMAGES_TAR}" ] || die "images.tar is required in bundle directory"
}

ensure_init_sql_file() {
  if [ -d "${INIT_SQL_FILE}" ]; then
    die "init.sql is a directory in bundle/runtime root (${INIT_SQL_FILE}); remove it and prepare runtime from a fresh bundle"
  fi
  [ -f "${INIT_SQL_FILE}" ] || die "init.sql is required in bundle/runtime root"
}

ensure_network() {
  local network_name="${GIGAEVO_NETWORK_NAME:-gigaevo-network}"
  if ! docker network inspect "${network_name}" >/dev/null 2>&1; then
    log "Creating external network ${network_name}"
    docker network create "${network_name}" >/dev/null
  fi
}

generate_runner_pool() {
  export RUNNER_POOL_SIZE="${RUNNER_POOL_SIZE:-1}"
  export RUNNER_REDIS_DB_START="${RUNNER_REDIS_DB_START:-1}"

  python3 "${ROOT_DIR}/generate_runner_pool_compose.py" --mode deploy --output "${RUNNER_POOL_COMPOSE}"
}

resolve_redis_databases() {
  [ -x "${REDIS_DB_RESOLVER}" ] || die "Missing resolver script: scripts/resolve_redis_databases.sh"
  REDIS_DATABASES="$("${REDIS_DB_RESOLVER}")"
  export REDIS_DATABASES
}

ensure_runner_pool_compose_file() {
  [ -f "${RUNNER_POOL_COMPOSE}" ] || die "docker-compose.runner-pool.generated.yml is missing; run 'up' (or make bundle-deploy) to regenerate it"
}

list_expected_images() {
  compose config --images | sort -u
}

image_exists() {
  docker image inspect "$1" >/dev/null 2>&1
}

ensure_images_loaded() {
  local missing_count image

  missing_count=0
  while IFS= read -r image; do
    [ -n "${image}" ] || continue
    if ! image_exists "${image}"; then
      missing_count=$((missing_count + 1))
    fi
  done < <(list_expected_images)

  if [ "${missing_count}" -gt 0 ]; then
    log "Loading images from images.tar (${missing_count} missing image(s))"
    docker load -i "${IMAGES_TAR}" >/dev/null
  fi

  while IFS= read -r image; do
    [ -n "${image}" ] || continue
    image_exists "${image}" || die "Required image '${image}' is still missing after docker load"
  done < <(list_expected_images)
}

cmd_up() {
  ensure_init_sql_file
  ensure_network
  generate_runner_pool
  resolve_redis_databases
  ensure_images_loaded

  log "Starting bundle stack (RUNNER_POOL_SIZE=${RUNNER_POOL_SIZE})"
  REDIS_DATABASES="${REDIS_DATABASES}" compose up -d --no-build --remove-orphans

  log "Stack started"
  compose ps
  cat <<MSG

Endpoints:
  - Web UI:     http://localhost:${WEB_UI_HOST_PORT:-7860}
  - Master API: http://localhost:${MASTER_API_HOST_PORT:-8000}
  - Runner API: http://localhost:${RUNNER_API_HOST_PORT:-8001}
  - MinIO:      http://localhost:${MINIO_CONSOLE_HOST_PORT:-9001}
MSG
}

cmd_stop() {
  ensure_runner_pool_compose_file
  log "Stopping bundle stack"
  compose stop
}

cmd_clean() {
  ensure_runner_pool_compose_file
  log "Cleaning bundle stack (containers and volumes will be removed)"
  compose down -v --remove-orphans
}

cmd_status() {
  ensure_runner_pool_compose_file
  compose ps
}

cmd_logs() {
  ensure_runner_pool_compose_file
  local service="${1:-}"
  if [ -n "${service}" ]; then
    compose logs -f "${service}"
  else
    compose logs -f
  fi
}

cmd_db_reset() {
  ensure_runner_pool_compose_file
  log "Resetting bundle database (gigaevo_master)"
  compose exec -T postgres psql -U gigaevouser -d postgres -c "DROP DATABASE IF EXISTS gigaevo_master;"
  compose exec -T postgres psql -U gigaevouser -d postgres -c "CREATE DATABASE gigaevo_master;"
  log "Bundle database reset complete"
}

cmd_db_migrate() {
  ensure_runner_pool_compose_file
  log "Running bundle database migrations"
  compose exec -T master-api sh -lc "cd /app && uv run alembic upgrade head"
  log "Bundle database migrations complete"
}

main() {
  cd "${ROOT_DIR}"

  require_cmd docker
  require_cmd python3
  docker compose version >/dev/null 2>&1 || die "docker compose plugin is not available"

  load_bundle_metadata
  ensure_env_file
  container_env_load_file "${ENV_FILE}"
  resolve_project_name
  ensure_llm_file
  ensure_images_tar_file

  local command="${1:-up}"
  case "${command}" in
    up)
      cmd_up
      ;;
    stop)
      cmd_stop
      ;;
    clean)
      cmd_clean
      ;;
    status)
      cmd_status
      ;;
    logs)
      cmd_logs "${2:-}"
      ;;
    db-reset)
      cmd_db_reset
      ;;
    db-migrate)
      cmd_db_migrate
      ;;
    *)
      die "Unknown command '${command}'. Use: up|stop|clean|status|logs [service]|db-reset|db-migrate"
      ;;
  esac
}

main "$@"
