#!/usr/bin/env bash

set -euo pipefail

RUNNER_POOL_SIZE="${RUNNER_POOL_SIZE:-1}"
RUNNER_REDIS_DB_START="${RUNNER_REDIS_DB_START:-1}"
REDIS_DATABASES_MIN="${REDIS_DATABASES_MIN:-512}"

req_dbs=$((RUNNER_REDIS_DB_START + RUNNER_POOL_SIZE))
if [ "${req_dbs}" -gt "${REDIS_DATABASES_MIN}" ]; then
    max_pool=$((REDIS_DATABASES_MIN - RUNNER_REDIS_DB_START))
    echo "❌ ERROR: RUNNER_POOL_SIZE=${RUNNER_POOL_SIZE} exceeds Redis DB limit." >&2
    echo "   Required databases: RUNNER_REDIS_DB_START(${RUNNER_REDIS_DB_START}) + RUNNER_POOL_SIZE(${RUNNER_POOL_SIZE}) = ${req_dbs}" >&2
    echo "   Configured REDIS_DATABASES_MIN=${REDIS_DATABASES_MIN}" >&2
    echo "   Max supported RUNNER_POOL_SIZE is ${max_pool} (for RUNNER_REDIS_DB_START=${RUNNER_REDIS_DB_START})." >&2
    exit 1
fi

# Caller should export this value for docker compose commands.
echo "${REDIS_DATABASES_MIN}"
