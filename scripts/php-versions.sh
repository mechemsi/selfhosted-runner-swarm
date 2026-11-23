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

# Same key and fingerprint as runner-image/install-php.sh.
readonly KEY_ASC="${REPO_ROOT}/runner-image/sury-php.asc"
readonly SURY_FPR="15058500A0235D97F5D10063B188E2B695BD4743"
readonly INDEX="main/binary-amd64/Packages.gz"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Authenticate the index the way apt does: InRelease must carry a valid
# signature by the vendored key, and Packages.gz must match the SHA256 listed
# in the signed part. Nothing unverified reaches the lock file.
gpg --dearmor < "$KEY_ASC" > "$work/sury.gpg"
primaries="$(gpg --show-keys --with-colons "$work/sury.gpg" | awk -F: '$1 == "pub" { getline; print $10 }')"
[[ "$primaries" == "$SURY_FPR" ]] || { echo "ERROR: $KEY_ASC is not exactly the key $SURY_FPR" >&2; exit 1; }
curl -fsSL "${SURY}/dists/${codename}/InRelease" -o "$work/InRelease"
gpgv --keyring "$work/sury.gpg" --output "$work/Release" "$work/InRelease" 2>/dev/null \
    || { echo "ERROR: ${SURY}/dists/${codename}/InRelease has no valid signature by $SURY_FPR" >&2; exit 1; }
want_sha="$(awk -v f="$INDEX" '/^SHA256:/ { in_sha = 1; next } /^[^ ]/ { in_sha = 0 } in_sha && $3 == f { print $1 }' "$work/Release")"
[[ "$want_sha" =~ ^[0-9a-f]{64}$ ]] || { echo "ERROR: signed Release lists no SHA256 for $INDEX" >&2; exit 1; }
curl -fsSL "${SURY}/dists/${codename}/${INDEX}" -o "$work/Packages.gz"
echo "${want_sha}  $work/Packages.gz" | sha256sum -c --quiet - \
    || { echo "ERROR: $INDEX does not match the signed Release" >&2; exit 1; }
packages="$(gunzip < "$work/Packages.gz")"

# Newest upstream release of php<series>-cli: "8.4.28-3+debsuryorg26.04.1+1" -> "8.4.28".
newest() {
    awk -v pkg="php$1-cli" '
        $1 == "Package:" { hit = ($2 == pkg) }
        hit && $1 == "Version:" { v = $2; sub(/^[0-9]+:/, "", v); sub(/-.*/, "", v); print v }
    ' <<<"$packages" | sort -V | tail -n 1
}

changed=false
summary=()
tmp="$work/php-versions"
while IFS= read -r line; do
    if [[ "$line" =~ ^[[:space:]]*(#|$) ]]; then
        echo "$line" >> "$tmp"
        continue
    fi
    if ! [[ "$line" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "ERROR: php-versions line '${line}' is not X.Y.Z" >&2
        exit 1
    fi
    series="${line%.*}"
    latest="$(newest "$series")"
    # Only a plain X.Y.Z of the same series may reach the lock file.
    if ! [[ "$latest" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && "${latest%.*}" == "$series" ]]; then
        echo "ERROR: deb.sury.org has no usable php${series}-cli for ${codename} (got '${latest}')" >&2
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
