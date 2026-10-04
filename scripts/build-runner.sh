#!/usr/bin/env bash

# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

#
# Build the runner image locally, for development or a custom agent version.
# Production pulls the published image (ghcr.io/mechemsi/rorch-runner) instead.
# No per-host docker GID any more: the entrypoint joins the socket's group at
# start, so the same image works on every host.
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
readonly NEWEST="2.337.0"
readonly RUNNER_VERSION="${RUNNER_VERSION:-${NEWEST}}"
readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly CONTEXT="${REPO_ROOT}/runner-image"
readonly IMAGE_TAG="${IMAGE_TAG:-gh-runner:${RUNNER_VERSION}}"

echo "==> Building ${IMAGE_TAG} (runner ${RUNNER_VERSION})"
docker build \
    --build-arg "RUNNER_VERSION=${RUNNER_VERSION}" \
    -t "${IMAGE_TAG}" \
    "${CONTEXT}"

# Only the newest agent claims :latest — pools that pin an old version do so
# through their own tag, and must not drag every unpinned pool backwards.
if [[ "${RUNNER_VERSION}" == "${NEWEST}" && "${IMAGE_TAG}" != "gh-runner:latest" ]]; then
    docker tag "${IMAGE_TAG}" gh-runner:latest
    echo "==> Tagged gh-runner:latest -> ${IMAGE_TAG}"
fi

echo "==> Done. Pin a pool to this agent with:  runner_image: ${IMAGE_TAG}"
