#!/usr/bin/env bash

# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

set -euo pipefail

: "${RORCH_MAIN_JIT_CONFIG:?GitHub JIT config is required}"
readonly socket=/var/run/docker.sock
if [[ -e "$socket" || -L "$socket" ]]; then
    echo "ERROR: Main-trusted runner requires an unused Docker socket path" >&2
    exit 1
fi

# VFS works when the outer container already uses overlay storage.
export DOCKER_HOST="unix://$socket"
dockerd --host "$DOCKER_HOST" --group runner \
    --data-root /var/lib/rorch-docker --storage-driver vfs &
readonly daemon_pid=$!

while ! timeout 2 docker info >/dev/null 2>&1; do
    if ! kill -0 "$daemon_pid" 2>/dev/null || (( SECONDS >= 30 )); then
        echo "ERROR: The main-trusted runner's private Docker daemon did not start" >&2
        exit 1
    fi
    sleep 1
done

exec /entrypoint.sh
