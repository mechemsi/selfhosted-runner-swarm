---
title: Personal account all-repositories runner pool
status: approved
date: 2026-07-11
related: [../plans/2026-07-11-personal-all-repositories.md]
---

# Personal account all-repositories runner pool

## Problem

RORCH can register runners for one repository or an organization, but a GitHub personal
account owner must add every repository to `config.yml`. New repositories are therefore
not covered automatically, and omitting `repo` incorrectly sends personal accounts to
organization-only API endpoints.

## Users

Operators who own several repositories under one GitHub personal account and want one
RORCH configuration entry to cover every repository accessible to their PAT.

## Success Criteria

- A personal pool discovers every non-archived repository owned by the authenticated account.
- A queued job in any discovered repository can launch a repository-scoped runner.
- New repositories are picked up on a later poll without changing `config.yml`.
- `max_runners` limits the total running containers across the personal pool.
- Existing repository and organization pool configuration remains compatible.

## Scope

### In scope

- Explicit personal-account scope in pool configuration.
- Paginated discovery of repositories owned by the authenticated account.
- Reuse of existing repository queue, runner, registration, and cleanup behavior.
- Fair allocation of available runner capacity among repositories with demand.
- Unit tests and user-facing configuration documentation.

### Out of scope

- A GitHub account-level runner registration, which GitHub does not provide.
- Warm idle runners for personal pools; a warm runner cannot serve arbitrary personal repos.
- Automatic management of repositories the token cannot access.
- Cleanup for a repository deleted while it still has a running container.

## Risks & Open Questions

- Fine-grained PATs discover only repositories selected for that token.
- Polling many repositories increases GitHub API usage.
- Personal pools intentionally treat `min_idle` as zero because runners remain repo-scoped.
