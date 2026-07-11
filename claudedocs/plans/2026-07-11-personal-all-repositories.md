---
title: Implement personal account all-repositories runner pool
status: completed
date: 2026-07-11
related: [../prds/2026-07-11-personal-all-repositories.md]
---

# Implement personal account all-repositories runner pool

## Approach

1. Add an explicit `scope: personal` pool mode while preserving existing configuration defaults.
2. Discover owned repositories through the authenticated `/user/repos` endpoint with pagination.
3. Derive repository-scoped runtime pool configurations from the personal parent pool.
4. Collect and clean up each repository independently, then allocate the parent's remaining
   `max_runners` capacity fairly across repositories with queued work.
5. Cover configuration, discovery, global-cap, and allocation behavior with unit tests.
6. Update the README and example configuration.

## Verification

- `ruff check rorch/ tests/`
- `pyright rorch/`
- `pytest -v --cov=rorch`

All implementation steps and verification checks are complete.
