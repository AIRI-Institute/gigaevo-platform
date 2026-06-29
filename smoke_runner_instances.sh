#!/usr/bin/env bash
set -euo pipefail

MASTER_URL="${MASTER_URL:-http://localhost:8000}"

usage() {
  cat <<'EOF'
Smoke test helper for "Runner Instances" actions via Master API.

Usage:
  MASTER_URL=http://localhost:8000 ./smoke_runner_instances.sh status
  MASTER_URL=http://localhost:8000 ./smoke_runner_instances.sh list
  MASTER_URL=http://localhost:8000 ./smoke_runner_instances.sh health
  MASTER_URL=http://localhost:8000 ./smoke_runner_instances.sh logs <runner-id> [lines]
  MASTER_URL=http://localhost:8000 ./smoke_runner_instances.sh init <runner-id>
  MASTER_URL=http://localhost:8000 ./smoke_runner_instances.sh restart <runner-id>
  MASTER_URL=http://localhost:8000 ./smoke_runner_instances.sh stop <runner-id>
  MASTER_URL=http://localhost:8000 ./smoke_runner_instances.sh init-all
EOF
}

cmd="${1:-status}"

case "$cmd" in
  -h|--help|help)
    usage
    exit 0
    ;;
  status)
    echo "Master: $MASTER_URL/health"
    curl -fsS "$MASTER_URL/health" | sed -e 's/^/  /'
    echo
    echo "Instances:"
    curl -fsS "$MASTER_URL/api/v1/instances/" | sed -e 's/^/  /'
    ;;
  list)
    curl -fsS "$MASTER_URL/api/v1/instances/"
    ;;
  health)
    curl -fsS "$MASTER_URL/api/v1/instances/health/summary"
    ;;
  logs)
    runner_id="${2:-}"
    lines="${3:-50}"
    if [[ -z "$runner_id" ]]; then
      echo "Missing runner-id" >&2
      usage
      exit 2
    fi
    curl -fsS "$MASTER_URL/api/v1/instances/$runner_id/logs?lines=$lines"
    ;;
  init)
    runner_id="${2:-}"
    if [[ -z "$runner_id" ]]; then
      echo "Missing runner-id" >&2
      usage
      exit 2
    fi
    curl -fsS -X POST "$MASTER_URL/api/v1/instances/$runner_id/initialize"
    ;;
  restart)
    runner_id="${2:-}"
    if [[ -z "$runner_id" ]]; then
      echo "Missing runner-id" >&2
      usage
      exit 2
    fi
    curl -fsS -X POST "$MASTER_URL/api/v1/instances/$runner_id/restart"
    ;;
  stop)
    runner_id="${2:-}"
    if [[ -z "$runner_id" ]]; then
      echo "Missing runner-id" >&2
      usage
      exit 2
    fi
    curl -fsS -X POST "$MASTER_URL/api/v1/instances/$runner_id/stop"
    ;;
  init-all)
    curl -fsS -X POST "$MASTER_URL/api/v1/instances/initialize-all"
    ;;
  *)
    echo "Unknown command: $cmd" >&2
    usage
    exit 2
    ;;
esac

