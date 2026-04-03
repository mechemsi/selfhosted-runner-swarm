# Code Style Rules

## Python (orchestrator)
- Follow PEP 8
- Use type hints for all function signatures
- Use f-strings for string formatting
- Use dataclasses for structured config
- Wrap errors with context: `raise RuntimeError(f"failed to {action}: {e}") from e`
- Use `logging` module, not `print()` for operational output
- Keep functions under 50 lines
- Use snake_case for functions and variables

## Bash (entrypoint, scripts)
- Start with `#!/usr/bin/env bash` and `set -euo pipefail`
- Quote all variable expansions: `"${VAR}"`
- Validate required env vars at the top of the script
- Use `readonly` for constants
- Add error messages that help troubleshooting

## YAML (config files)
- 2-space indentation
- Comment non-obvious fields
- Use environment variable interpolation for secrets: `"${GITHUB_PAT}"`

## General
- No trailing whitespace
- Files end with a newline
- UTF-8 encoding everywhere
