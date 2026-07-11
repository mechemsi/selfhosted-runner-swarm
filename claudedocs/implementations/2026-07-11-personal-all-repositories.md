---
title: Personal account all-repositories runner pool implementation
status: completed
date: 2026-07-11
related:
  - ../prds/2026-07-11-personal-all-repositories.md
  - ../plans/2026-07-11-personal-all-repositories.md
---

# Personal account all-repositories runner pool implementation

RORCH now accepts `scope: personal` for a pool. Each poll discovers active repositories owned
by the authenticated GitHub account through the paginated `/user/repos` endpoint. The scaler
derives repository-scoped runtime pools, reuses the existing queue and runner APIs, and assigns
available capacity round-robin among repositories with demand.

`max_runners` is enforced across all discovered repositories in the personal pool. Personal
pools default `min_idle` to zero because GitHub cannot register one warm runner for every repo
in a personal account.

## Verification

- Ruff: passed.
- Pyright: passed with no errors (one environment warning for unavailable YAML source stubs).
- Pytest: 44 tests passed.
