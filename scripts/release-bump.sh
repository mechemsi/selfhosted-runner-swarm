#!/usr/bin/env bash

# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

#
# Write a release version into every manifest that carries one. Called by
# semantic-release's prepare step (.releaserc.json); @semantic-release/git then
# commits these files and the tag points at that commit, so a tag checkout
# always reports its own version.
#
# Usage: ./scripts/release-bump.sh 1.2.3

set -euo pipefail

readonly VERSION="${1:?usage: release-bump.sh <version>}"
readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! [[ "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]]; then
    echo "ERROR: '${VERSION}' is not a semver version (no leading v)" >&2
    exit 1
fi

pyproject="${REPO_ROOT}/orchestrator/pyproject.toml"
sed -i -E "0,/^version = \".*\"$/s//version = \"${VERSION}\"/" "${pyproject}"
grep -q "^version = \"${VERSION}\"$" "${pyproject}" || {
    echo "ERROR: failed to bump ${pyproject}" >&2
    exit 1
}

# npm version rewrites package.json and both version fields in package-lock.json
# without touching dependencies or needing node_modules.
(cd "${REPO_ROOT}/dashboard" \
    && npm version "${VERSION}" --no-git-tag-version --allow-same-version >/dev/null)

echo "==> Bumped orchestrator and dashboard to ${VERSION}"
