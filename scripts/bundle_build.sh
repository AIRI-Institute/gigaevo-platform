#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUT_DIR="${OUT_DIR:-${ROOT_DIR}/dist/bundle}"
BUILD_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BUNDLE_NAME="${BUNDLE_NAME:-gigaevo-bundle-${BUILD_STAMP}.tar.gz}"
BUNDLE_PATH="${OUT_DIR}/${BUNDLE_NAME}"
STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gigaevo-bundle-stage.XXXXXX")"

COMPOSE_FILES=(
  -f docker-compose.kafka.yml
  -f docker-compose.master-api.yml
  -f "${STAGE_DIR}/docker-compose.runner-pool.generated.yml"
  -f docker-compose.web-ui.yml
)

cleanup() {
  rm -rf "${STAGE_DIR}"
}
trap cleanup EXIT

log() {
  printf '[bundle-build] %s\n' "$*"
}

die() {
  printf '[bundle-build] ERROR: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

compose() {
  docker compose "${COMPOSE_FILES[@]}" "$@"
}

rewrite_staged_env_example() {
  local env_example="${STAGE_DIR}/.env.example"

  python3 - "${env_example}" "${COMPOSE_PROJECT_NAME}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
project_name = sys.argv[2]
lines = path.read_text(encoding="utf-8").splitlines()

for index, line in enumerate(lines):
    if line.startswith("COMPOSE_PROJECT_NAME="):
        lines[index] = f"COMPOSE_PROJECT_NAME={project_name}"
        break
else:
    raise SystemExit("Missing COMPOSE_PROJECT_NAME entry in staged .env.example")

path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

main() {
  cd "${ROOT_DIR}"

  require_cmd docker
  require_cmd python3
  require_cmd tar
  docker compose version >/dev/null 2>&1 || die "docker compose plugin is not available"

  # shellcheck disable=SC1091
  source "${ROOT_DIR}/scripts/container_env.sh"
  container_env_load_and_resolve "${ROOT_DIR}/.env"

  [ -f "${ROOT_DIR}/llm_models.yml" ] || die "llm_models.yml is required"
  if [ -n "${GITHUB_PAT:-}" ]; then
    log "GITHUB_PAT found; private repo clone is available"
  else
    log "GITHUB_PAT not set; public GitHub HTTPS repos can still build"
    log "Private repos will fail during runner image clone"
  fi

  mkdir -p "${OUT_DIR}"
  mkdir -p "${STAGE_DIR}/scripts" "${STAGE_DIR}/docs"

  log "Generating deploy runner pool compose with RUNNER_POOL_SIZE=1"
  (
    export RUNNER_POOL_SIZE=1
    python3 generate_runner_pool_compose.py --mode deploy --output "${STAGE_DIR}/docker-compose.runner-pool.generated.yml"
  )

  log "Copying required manifests and scripts to staging"
  cp docker-compose.kafka.yml "${STAGE_DIR}/"
  cp docker-compose.master-api.yml "${STAGE_DIR}/"
  cp docker-compose.web-ui.yml "${STAGE_DIR}/"
  cp init.sql "${STAGE_DIR}/"
  cp generate_runner_pool_compose.py "${STAGE_DIR}/"
  cp scripts/container_env.sh "${STAGE_DIR}/scripts/"
  cp scripts/resolve_redis_databases.sh "${STAGE_DIR}/scripts/"
  cp scripts/bundle_stack.sh "${STAGE_DIR}/scripts/"
  cp docs/bundle.md "${STAGE_DIR}/docs/"
  cp .env.example "${STAGE_DIR}/"
  cp llm_models.yml.example "${STAGE_DIR}/"
  rewrite_staged_env_example

  log "Pulling infrastructure images"
  compose pull postgres redis redis-gigavolve kafka minio

  log "Building shared runner image"
  MODE=prod ENV_FILE="${ROOT_DIR}/.env" "${ROOT_DIR}/scripts/build_runner_image.sh"

  log "Building application images (master-api, web-ui)"
  compose build master-api web-ui

  log "Resolving exact image list for compose project ${COMPOSE_PROJECT_NAME}"
  compose config --images | sort -u > "${STAGE_DIR}/images.list"

  if [ ! -s "${STAGE_DIR}/images.list" ]; then
    die "No images resolved from compose config"
  fi

  image_count="$(wc -l < "${STAGE_DIR}/images.list" | tr -d ' ')"
  log "Saving ${image_count} images to images.tar"
  xargs docker save -o "${STAGE_DIR}/images.tar" < "${STAGE_DIR}/images.list"

  git_sha=""
  if command -v git >/dev/null 2>&1; then
    git_sha="$(git rev-parse HEAD 2>/dev/null || true)"
  fi

  cat > "${STAGE_DIR}/bundle.meta.env" <<META
BUNDLE_COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME}
BUNDLE_BUILT_AT_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)
BUNDLE_GIT_SHA=${git_sha}
META

  log "Creating bundle archive: ${BUNDLE_PATH}"
  tar -C "${STAGE_DIR}" -czf "${BUNDLE_PATH}" .

  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "${BUNDLE_PATH}" > "${BUNDLE_PATH}.sha256"
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${BUNDLE_PATH}" > "${BUNDLE_PATH}.sha256"
  else
    die "No SHA256 tool found (shasum or sha256sum)"
  fi

  log "Bundle archive ready"
  log "  Archive: ${BUNDLE_PATH}"
  log "  Checksum: ${BUNDLE_PATH}.sha256"
}

main "$@"
