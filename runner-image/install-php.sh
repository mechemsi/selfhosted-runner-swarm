#!/usr/bin/env bash

# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

# Bake PHP into the runner image so shivammathur/setup-php finds the requested
# version installed and never goes to apt.
#
# On a self-hosted runner setup-php installs PHP from the ondrej/php PPA on
# Launchpad, in every job, because each runner is a fresh container. When
# Launchpad is slow that step takes 25 minutes (cfieds, 2026-11-23). setup-php
# skips the install when /usr/bin/php<v> and /usr/bin/php-config<v> exist and
# `update` is not set, and enables an extension without apt when its .so is
# already in the extension dir. So: every series in php-versions, with the
# packages setup-php installs itself (configs/php_packages) plus the extensions
# our workflows ask for.
#
# Source: deb.sury.org, the same builds as the PPA, served from a CDN. On
# Ubuntu 26.04 setup-php itself uses this repository, so anything not baked
# here (another series, an extension not listed) still installs from sury in a
# few seconds, not from Launchpad. The list file has the name and format
# setup-php writes, so it recognises the repository instead of adding a
# duplicate.
#
# Usage: install-php.sh <php-versions file>

set -euo pipefail

readonly LOCK="$1"
readonly DEFAULT_SERIES="8.4"
readonly SURY="https://packages.sury.org/php/"
readonly KEYRING="/usr/share/keyrings/ondrej-php-keyring.gpg"
# DEB.SURY.ORG Automatic Signing Key
readonly SURY_FPR="15058500A0235D97F5D10063B188E2B695BD4743"

# setup-php's own package set, then the extensions the repositories on these
# runners request (extensions: lines and coverage drivers, 2026-11-23).
readonly PACKAGES="cgi cli common dev fpm curl intl mbstring mysql opcache pgsql xml zip"
readonly EXTENSIONS="amqp bcmath gd gmp igbinary redis soap sqlite3 pcov xdebug"
# Built into PHP since 8.5, no package of its own.
readonly BUILTIN_FROM_85="opcache"
# setup-php's self-hosted helper installs these before anything else; present,
# its `apt-get install` is a no-op that needs no package lists.
readonly HELPER_PACKAGES="apt-transport-https ca-certificates curl file make jq unzip autoconf automake gcc g++ gnupg"

# shellcheck source=/dev/null
codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"

curl -fsSL "${SURY}apt.gpg" -o "$KEYRING"
gpg --show-keys --with-colons "$KEYRING" | grep -q "^fpr:*${SURY_FPR}:" || {
    echo "ERROR: ${SURY}apt.gpg is not the deb.sury.org key ${SURY_FPR}" >&2
    exit 1
}
cat > "/etc/apt/sources.list.d/ondrej-ubuntu-php-${codename}.sources" <<EOF
Types: deb
URIs: ${SURY}
Suites: ${codename}
Components: main
Architectures: $(dpkg --print-architecture)
Signed-By: ${KEYRING}
EOF
apt-get update

mapfile -t wanted < <(grep -Ev '^[[:space:]]*(#|$)' "$LOCK")
read -ra packages <<<"php-pear ${HELPER_PACKAGES}"
for want in "${wanted[@]}"; do
    series="${want%.*}"
    for name in $PACKAGES $EXTENSIONS; do
        if [[ "$name" == "$BUILTIN_FROM_85" ]] && printf '8.5\n%s\n' "$series" | sort -V -C; then
            continue
        fi
        packages+=("php${series}-${name}")
    done
done
apt-get install -y --no-install-recommends "${packages[@]}"

# Coverage drivers stay off unless a job asks for them (setup-php `coverage:`
# enables the installed .so), as on GitHub-hosted images: a loaded xdebug slows
# every PHP process.
phpdismod -v ALL -s ALL xdebug pcov

# Jobs that call `php` without setup-php get the default series.
for tool in php phar phar.phar php-cgi php-config phpize phpdbg; do
    target="/usr/bin/${tool}${DEFAULT_SERIES}"
    if [[ -e "$target" ]]; then
        update-alternatives --set "$tool" "$target"
    fi
done

# Each series must be at least the patch release php-versions names, so a
# stale mirror or a cached layer cannot ship an older PHP than the lock says.
mkdir -p /etc/rorch
: > /etc/rorch/php-versions
for want in "${wanted[@]}"; do
    series="${want%.*}"
    got="$("php${series}" -r 'echo PHP_VERSION;')"
    printf '%s\n%s\n' "$want" "$got" | sort -V -C || {
        echo "ERROR: php${series} is ${got}, php-versions asks for at least ${want}" >&2
        exit 1
    }
    test -e "/usr/bin/php-config${series}"
    echo "$got" >> /etc/rorch/php-versions
done
echo "==> PHP baked: $(paste -sd ' ' /etc/rorch/php-versions), default ${DEFAULT_SERIES}"

rm -rf /var/lib/apt/lists/*
