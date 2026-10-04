#!/bin/bash

# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

set -e

# ── Root phase: join the Docker socket's group, then drop to `runner` ────────
# The image is published once and run on many hosts, each with its own docker
# group GID. Read it from the mounted socket, give `runner` a group with that
# GID, and re-exec this script as `runner` with the new group list. Every
# runner container is fresh (ephemeral), so this edits only its own /etc/group.
DOCKER_SOCK="${DOCKER_SOCK:-/var/run/docker.sock}"
if [[ "$(id -u)" == "0" ]]; then
    if [[ -S "$DOCKER_SOCK" ]]; then
        sock_gid=$(stat -c %g "$DOCKER_SOCK")
        sock_group=$(getent group "$sock_gid" | cut -d: -f1)
        if [[ -z "$sock_group" ]]; then
            sock_group=docker-host
            groupadd -g "$sock_gid" "$sock_group"
        fi
        if ! id -nG runner | tr ' ' '\n' | grep -qx "$sock_group"; then
            usermod -aG "$sock_group" runner
        fi
        echo "==> Docker socket GID ${sock_gid}: runner joined group ${sock_group}"
    else
        echo "WARN: ${DOCKER_SOCK} is not a socket; starting without Docker access"
    fi
    exec setpriv --reuid=runner --regid=runner --init-groups \
        env HOME=/home/runner USER=runner LOGNAME=runner "$0" "$@"
fi

# ── Validation ────────────────────────────────────────────────────────────────
required_vars=("GITHUB_PAT" "GITHUB_OWNER" "GITHUB_REPO" "RUNNER_NAME")
for var in "${required_vars[@]}"; do
    if [[ -z "${!var}" ]]; then
        echo "ERROR: $var is required"
        exit 1
    fi
done

# ── Fix ownership of mounted cache dirs ──────────────────────────────────────
# Docker creates bind-mount host dirs as root; the runner user needs write access.
for d in ~/.cache ~/.npm ~/.composer ~/.nuget; do
    if [ -d "$d" ] && [ ! -w "$d" ]; then
        sudo chown -R runner:runner "$d"
    fi
done

# ── Warm toolcache from staged binaries ──────────────────────────────────────
# Pre-built Node/Python are staged in the image at /opt/toolcache-staging.
# Copy them into the shared /opt/hostedtoolcache (host bind-mount) if missing.
# Only the first runner to boot does the copy; subsequent runners find them.
STAGING="/opt/toolcache-staging"
TOOLCACHE="/opt/hostedtoolcache"
if [ -d "$STAGING" ]; then
    for tool_dir in "$STAGING"/*/; do
        tool=$(basename "$tool_dir")
        for version_dir in "$tool_dir"/*/; do
            version=$(basename "$version_dir")
            dest="$TOOLCACHE/$tool/$version"
            marker="$dest/x64.complete"
            if [ ! -f "$marker" ]; then
                echo "==> Warming toolcache: $tool $version"
                sudo mkdir -p "$dest"
                sudo cp -a "$version_dir"* "$dest/"
                sudo cp -a "$STAGING/$tool/$version/x64.complete" "$dest/" 2>/dev/null || true
                sudo chown -R runner:runner "$dest"
            fi
        done
    done
fi

# ── Let jobs install into the toolcache ──────────────────────────────────────
# Docker creates the bind-mount dir as root, and the warm step above creates
# tool dirs (node/, Python/) as root too, so setup-* could not add a version the
# image did not stage. Non-recursive: version dirs are already the runner's.
sudo mkdir -p "$TOOLCACHE"
sudo chown runner:runner "$TOOLCACHE"
for tool_dir in "$TOOLCACHE"/*/; do
    [ -d "$tool_dir" ] && [ ! -O "$tool_dir" ] && sudo chown runner:runner "$tool_dir"
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