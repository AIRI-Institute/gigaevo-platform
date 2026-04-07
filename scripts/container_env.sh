#!/usr/bin/env bash

container_env_load_file() {
  local env_file="${1:-.env}"

  if [ -f "${env_file}" ]; then
    set -a
    # shellcheck disable=SC1090
    . "${env_file}"
    set +a
  fi
}

container_env_resolve() {
  local project_name_override="${1:-}"

  if [ -n "${project_name_override}" ]; then
    COMPOSE_PROJECT_NAME="${project_name_override}"
  fi

  if [ -z "${COMPOSE_PROJECT_NAME:-}" ]; then
    echo "❌ ERROR: COMPOSE_PROJECT_NAME is required for container flows." >&2
    echo "Set it in .env or export it before running make/deploy/bundle commands." >&2
    return 1
  fi

  export COMPOSE_PROJECT_NAME
  export RUNNER_IMAGE_NAME="${COMPOSE_PROJECT_NAME}-runner-api"
}

container_env_load_and_resolve() {
  local env_file="${1:-.env}"
  local project_name_override="${2:-}"
  local existing_project_name="${COMPOSE_PROJECT_NAME:-}"
  local existing_github_pat="${GITHUB_PAT:-}"
  local existing_core_ref="${GIGAEVO_CORE_REF:-}"
  local existing_core_repo_url="${GIGAEVO_CORE_REPO_URL:-}"
  local existing_memory_api_url="${MEMORY_API_URL:-}"
  local existing_runner_pool_size="${RUNNER_POOL_SIZE:-}"
  local existing_runner_redis_db_start="${RUNNER_REDIS_DB_START:-}"
  local has_project_name=0
  local has_github_pat=0
  local has_core_ref=0
  local has_core_repo_url=0
  local has_memory_api_url=0
  local has_runner_pool_size=0
  local has_runner_redis_db_start=0

  if [ "${COMPOSE_PROJECT_NAME+x}" = "x" ]; then
    has_project_name=1
  fi
  if [ "${GITHUB_PAT+x}" = "x" ]; then
    has_github_pat=1
  fi
  if [ "${GIGAEVO_CORE_REF+x}" = "x" ]; then
    has_core_ref=1
  fi
  if [ "${GIGAEVO_CORE_REPO_URL+x}" = "x" ]; then
    has_core_repo_url=1
  fi
  if [ "${MEMORY_API_URL+x}" = "x" ]; then
    has_memory_api_url=1
  fi
  if [ "${RUNNER_POOL_SIZE+x}" = "x" ]; then
    has_runner_pool_size=1
  fi
  if [ "${RUNNER_REDIS_DB_START+x}" = "x" ]; then
    has_runner_redis_db_start=1
  fi

  container_env_load_file "${env_file}"

  if [ "${has_project_name}" -eq 1 ]; then
    COMPOSE_PROJECT_NAME="${existing_project_name}"
  fi
  if [ "${has_github_pat}" -eq 1 ]; then
    export GITHUB_PAT="${existing_github_pat}"
  fi
  if [ "${has_core_ref}" -eq 1 ]; then
    export GIGAEVO_CORE_REF="${existing_core_ref}"
  fi
  if [ "${has_core_repo_url}" -eq 1 ]; then
    export GIGAEVO_CORE_REPO_URL="${existing_core_repo_url}"
  fi
  if [ "${has_memory_api_url}" -eq 1 ]; then
    export MEMORY_API_URL="${existing_memory_api_url}"
  fi
  if [ "${has_runner_pool_size}" -eq 1 ]; then
    export RUNNER_POOL_SIZE="${existing_runner_pool_size}"
  fi
  if [ "${has_runner_redis_db_start}" -eq 1 ]; then
    export RUNNER_REDIS_DB_START="${existing_runner_redis_db_start}"
  fi

  container_env_resolve "${project_name_override}"
}
