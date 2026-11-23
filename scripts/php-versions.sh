#!/usr/bin/env bash

# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

# php-versions.sh: compare runner-image/php-versions with the newest PHP patch
# releases deb.sury.org publishes for the runner image's Ubuntu release.
#
# Usage:
#   ./scripts/php-versions.sh check   # print each series: pinned, newest
#   ./scripts/php-versions.sh bump    # rewrite the file to the newest releases;
#                                     # prints changed=true|false and summary=...
#                                     # (GitHub Actions output format)
#
# Used by .github/workflows/php-refresh.yml.

set -euo pipefail

readonly MODE="${1:-check}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT
readonly LOCK="${REPO_ROOT}/runner-image/php-versions"
readonly DOCKERFILE="${REPO_ROOT}/runner-image/Dockerfile"
readonly SURY="https://packages.sury.org/php"

# The codename comes from the base image itself, so a base bump is followed
# without a second place to edit.
base_image="$(awk '/^FROM /{print $2; exit}' "$DOCKERFILE")"
codename="$(docker run --rm --quiet "$base_image" sh -c '. /etc/os-release && echo "$VERSION_CODENAME"')"

packages="$(curl -fsSL "${SURY}/dists/${codename}/main/binary-amd64/Packages.gz" | gunzip)"

# Newest upstream release of php<series>-cli: "8.4.28-3+debsuryorg26.04.1+1" -> "8.4.28".
newest() {
    awk -v pkg="php$1-cli" '
        $1 == "Package:" { hit = ($2 == pkg) }
        hit && $1 == "Version:" { v = $2; sub(/^[0-9]+:/, "", v); sub(/-.*/, "", v); print v }
    ' <<<"$packages" | sort -V | tail -n 1
}

changed=false
summary=()
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
while IFS= read -r line; do
    if [[ "$line" =~ ^[[:space:]]*(#|$) ]]; then
        echo "$line" >> "$tmp"
        continue
    fi
    series="${line%.*}"
    latest="$(newest "$series")"
    if [[ -z "$latest" ]]; then
        echo "ERROR: deb.sury.org has no php${series}-cli for ${codename}" >&2
        exit 1
    fi
    [[ "$MODE" == check ]] && echo "php${series}: pinned ${line}, newest ${latest}"
    if [[ "$latest" != "$line" ]] && printf '%s\n%s\n' "$line" "$latest" | sort -V -C; then
        echo "$latest" >> "$tmp"
        changed=true
        summary+=("$latest")
    else
        echo "$line" >> "$tmp"
    fi
done < "$LOCK"

case "$MODE" in
check) ;;
bump)
    cp "$tmp" "$LOCK"
    echo "changed=${changed}"
    joined="$(IFS=,; echo "${summary[*]}")"
    echo "summary=${joined//,/, }"
    ;;
*) sed -n '7,15p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
