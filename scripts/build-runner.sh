#!/usr/bin/env bash

# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

#
# Build the gh-runner image with DOCKER_GID matching the host's docker group.
# Required so the runner user inside the container can access the mounted
# /var/run/docker.sock. Override DOCKER_GID via env var if needed.
#
# The image is tagged twice: gh-runner:<version> (pinnable per pool via
# runner_image in config.yml) and gh-runner:latest, unless a version older than
# the current default is being built — see NEWEST below.
#
# Usage:
#   ./scripts/build-runner.sh                      # newest, tags :<version> and :latest
#   RUNNER_VERSION=2.328.0 ./scripts/build-runner.sh   # older agent, tags :2.328.0 only
#   IMAGE_TAG=gh-runner:custom ./scripts/build-runner.sh

set -euo pipefail

# Keep in step with the ARG default in runner-image/Dockerfile.
readonly NEWEST="2.335.1"
readonly RUNNER_VERSION="${RUNNER_VERSION:-${NEWEST}}"
readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly CONTEXT="${REPO_ROOT}/runner-image"
readonly IMAGE_TAG="${IMAGE_TAG:-gh-runner:${RUNNER_VERSION}}"

DOCKER_GID="${DOCKER_GID:-$(getent group docker | cut -d: -f3 || true)}"
readonly DOCKER_GID

if [[ -z "${DOCKER_GID}" ]]; then
    echo "ERROR: could not determine host docker group GID" >&2
    echo "       set DOCKER_GID env var, or ensure the 'docker' group exists" >&2
    exit 1
fi

echo "==> Building ${IMAGE_TAG} (runner ${RUNNER_VERSION}, DOCKER_GID=${DOCKER_GID})"
docker build \
    --build-arg "DOCKER_GID=${DOCKER_GID}" \
    --build-arg "RUNNER_VERSION=${RUNNER_VERSION}" \
    --label "rorch.docker_gid=${DOCKER_GID}" \
    -t "${IMAGE_TAG}" \
    "${CONTEXT}"

# Only the newest agent claims :latest — pools that pin an old version do so
# through their own tag, and must not drag every unpinned pool backwards.
if [[ "${RUNNER_VERSION}" == "${NEWEST}" && "${IMAGE_TAG}" != "gh-runner:latest" ]]; then
    docker tag "${IMAGE_TAG}" gh-runner:latest
    echo "==> Tagged gh-runner:latest -> ${IMAGE_TAG}"
fi

echo "==> Done. Pin a pool to this agent with:  runner_image: ${IMAGE_TAG}"
