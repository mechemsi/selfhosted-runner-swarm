#!/usr/bin/env bash
set -euo pipefail

# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

# license-header.sh — Check or add MIT license headers to source files.
#
# Usage:
#   ./scripts/license-header.sh check [files...]   # Exit 1 if any file missing header
#   ./scripts/license-header.sh fix   [files...]   # Add header to files missing it
#
# If no files are given, scans the repo for all .py and .sh files.

HEADER_PY="# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root."

HEADER_SH="# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root."

MARKER="Copyright (c) 2026 Mechemsi"

MODE="${1:-check}"
shift || true

REPO_ROOT="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)"

# Collect files: either from args or by scanning the repo.
collect_files() {
    if [[ $# -gt 0 ]]; then
        for f in "$@"; do
            [[ "$f" == *.py || "$f" == *.sh ]] && echo "$f"
        done
    else
        git -C "$REPO_ROOT" ls-files '*.py' '*.sh'
    fi
}

add_header() {
    local file="$1"
    local tmp
    tmp="$(mktemp)"

    case "$file" in
        *.py)
            # Preserve shebang if present
            if head -1 "$file" | grep -q '^#!'; then
                head -1 "$file" > "$tmp"
                echo "" >> "$tmp"
                echo "$HEADER_PY" >> "$tmp"
                echo "" >> "$tmp"
                tail -n +2 "$file" >> "$tmp"
            else
                echo "$HEADER_PY" > "$tmp"
                echo "" >> "$tmp"
                cat "$file" >> "$tmp"
            fi
            ;;
        *.sh)
            # Preserve shebang
            if head -1 "$file" | grep -q '^#!'; then
                head -1 "$file" > "$tmp"
                echo "" >> "$tmp"
                echo "$HEADER_SH" >> "$tmp"
                echo "" >> "$tmp"
                tail -n +2 "$file" >> "$tmp"
            else
                echo "$HEADER_SH" > "$tmp"
                echo "" >> "$tmp"
                cat "$file" >> "$tmp"
            fi
            ;;
    esac

    mv "$tmp" "$file"
}

missing=0
fixed=0

while IFS= read -r file; do
    [[ -z "$file" ]] && continue
    filepath="$REPO_ROOT/$file"
    [[ -f "$filepath" ]] || filepath="$file"
    [[ -f "$filepath" ]] || continue

    if ! grep -qF "$MARKER" "$filepath"; then
        case "$MODE" in
            check)
                echo "MISSING: $file"
                missing=$((missing + 1))
                ;;
            fix)
                add_header "$filepath"
                echo "FIXED:   $file"
                fixed=$((fixed + 1))
                ;;
            *)
                echo "Unknown mode: $MODE (use 'check' or 'fix')" >&2
                exit 2
                ;;
        esac
    fi
done < <(collect_files "$@")

if [[ "$MODE" == "check" && $missing -gt 0 ]]; then
    echo ""
    echo "$missing file(s) missing license header."
    echo "Run: ./scripts/license-header.sh fix"
    exit 1
elif [[ "$MODE" == "check" ]]; then
    echo "All files have license headers."
elif [[ "$MODE" == "fix" ]]; then
    echo ""
    echo "$fixed file(s) updated."
fi
