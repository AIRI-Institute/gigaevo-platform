#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" = "0" ]; then
  if [ -S /var/run/docker.sock ]; then
    sock_gid="$(stat -c '%g' /var/run/docker.sock 2>/dev/null || stat -f '%g' /var/run/docker.sock 2>/dev/null || echo '')"
    if [ -n "${sock_gid}" ]; then
      group_name="$(getent group "${sock_gid}" | cut -d: -f1 || true)"
      if [ -z "${group_name}" ]; then
        group_name="dockerhost"
        groupadd -g "${sock_gid}" "${group_name}" 2>/dev/null || true
      fi
      usermod -aG "${group_name}" gigaevouser 2>/dev/null || true
    fi
  fi

  exec gosu gigaevouser "$@"
fi

exec "$@"

