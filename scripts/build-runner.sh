#!/usr/bin/env bash
#
# Build the gh-runner image with DOCKER_GID matching the host's docker group.
# Required so the runner user inside the container can access the mounted
# /var/run/docker.sock. Override DOCKER_GID via env var if needed.

set -euo pipefail

readonly IMAGE_TAG="${IMAGE_TAG:-gh-runner:latest}"
readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly CONTEXT="${REPO_ROOT}/runner-image"

DOCKER_GID="${DOCKER_GID:-$(getent group docker | cut -d: -f3 || true)}"
readonly DOCKER_GID

if [[ -z "${DOCKER_GID}" ]]; then
    echo "ERROR: could not determine host docker group GID" >&2
    echo "       set DOCKER_GID env var, or ensure the 'docker' group exists" >&2
    exit 1
fi

echo "==> Building ${IMAGE_TAG} with DOCKER_GID=${DOCKER_GID}"
docker build \
    --build-arg "DOCKER_GID=${DOCKER_GID}" \
    -t "${IMAGE_TAG}" \
    "${CONTEXT}"
