#!/bin/bash

# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

set -e

# ── Validation ────────────────────────────────────────────────────────────────
required_vars=("GITHUB_PAT" "GITHUB_OWNER" "GITHUB_REPO" "RUNNER_NAME")
for var in "${required_vars[@]}"; do
    if [[ -z "${!var}" ]]; then
        echo "ERROR: $var is required"
        exit 1
    fi
done

RUNNER_LABELS="${RUNNER_LABELS:-self-hosted,linux,x64,docker}"
RUNNER_GROUP="${RUNNER_GROUP:-Default}"
GITHUB_BASE="https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}"
API_BASE="https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}"

echo "==> Starting GitHub runner: ${RUNNER_NAME}"
echo "    Repo:   ${GITHUB_BASE}"
echo "    Labels: ${RUNNER_LABELS}"

# ── Verify Docker socket is accessible ───────────────────────────────────────
echo "==> Checking Docker socket..."
if ! docker info > /dev/null 2>&1; then
    echo "ERROR: Cannot connect to Docker socket at /var/run/docker.sock"
    echo "       Make sure the container is started with -v /var/run/docker.sock:/var/run/docker.sock"
    exit 1
fi
echo "    Docker OK: $(docker version --format '{{.Server.Version}}' 2>/dev/null)"

# ── Get registration token from GitHub ───────────────────────────────────────
echo "==> Fetching registration token..."
REG_TOKEN=$(curl -s -X POST \
    -H "Authorization: Bearer ${GITHUB_PAT}" \
    -H "Accept: application/vnd.github+json" \
    "${API_BASE}/actions/runners/registration-token" \
    | jq -r .token)

if [[ -z "$REG_TOKEN" || "$REG_TOKEN" == "null" ]]; then
    echo "ERROR: Failed to get registration token. Check GITHUB_PAT and repo access."
    exit 1
fi

# ── Register runner ───────────────────────────────────────────────────────────
echo "==> Registering runner..."
./config.sh \
    --url "${GITHUB_BASE}" \
    --token "${REG_TOKEN}" \
    --name "${RUNNER_NAME}" \
    --labels "${RUNNER_LABELS}" \
    --runnergroup "${RUNNER_GROUP}" \
    --unattended \
    --ephemeral

# ── Cleanup on exit ───────────────────────────────────────────────────────────
cleanup() {
    echo "==> Runner exiting, cleaning up..."
    ./config.sh remove --token "${REG_TOKEN}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ── Run ───────────────────────────────────────────────────────────────────────
echo "==> Runner registered, waiting for job..."
./run.sh

echo "==> Job completed (ephemeral runner exiting)"