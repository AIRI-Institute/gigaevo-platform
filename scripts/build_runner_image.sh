#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

log() {
  printf '[build-runner-image] %s\n' "$*"
}

die() {
  printf '[build-runner-image] ERROR: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

main() {
  local mode="${MODE:-${1:-}}"
  local target=""
  local env_file="${ENV_FILE:-${ROOT_DIR}/.env}"
  local project_name_override="${PROJECT_NAME_OVERRIDE:-}"

  case "${mode}" in
    dev)
      target="dev"
      ;;
    prod)
      target="prod"
      ;;
    *)
      die "MODE must be 'dev' or 'prod'"
      ;;
  esac

  require_cmd docker

  # shellcheck disable=SC1091
  source "${ROOT_DIR}/scripts/container_env.sh"
  container_env_load_and_resolve "${env_file}" "${project_name_override}"

  local -a docker_args=(
    build
    --file "${ROOT_DIR}/runner_api/Dockerfile"
    --target "${target}"
    --tag "${RUNNER_IMAGE_NAME}"
    --build-arg "GIGAEVO_CORE_REF=${GIGAEVO_CORE_REF:-main}"
    --build-arg "GIGAEVO_CORE_REPO_URL=${GIGAEVO_CORE_REPO_URL:-https://github.com/FusionBrainLab/gigaevo-core}"
  )

  if [ -n "${GITHUB_PAT:-}" ]; then
    docker_args+=(--secret id=github_pat,env=GITHUB_PAT)
  fi

  docker_args+=("${ROOT_DIR}")

  log "Building ${RUNNER_IMAGE_NAME} (mode=${mode}, target=${target})"
  DOCKER_BUILDKIT=1 docker "${docker_args[@]}"
}

main "$@"
