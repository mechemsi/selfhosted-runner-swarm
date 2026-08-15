---
title: config.yml stays the default; the database is only an overlay
status: active
date: 2026-08-15
related: [../implementations/2026-08-15-observability-dashboard.md]
---

# 001 — config.yml stays the default, the database is only an overlay

## Context

The dashboard needs to change orchestrator settings at runtime, which needs somewhere to
put them. `config.yml` is mounted `:ro` and is the file operators already know, version,
and copy between hosts. Three options were on the table.

## Options

**A — UI writes config.yml.** Remount read-write, rewrite YAML on save.
Familiar, single source of truth. But it destroys comments and ordering on every write,
races with an operator editing the file, and gives a network-reachable service write access
to the file that holds `${GITHUB_PAT}` references. Rejected.

**B — Database becomes the source of truth, config.yml seeds it once.** Clean model, and
what most tools do. But it makes the database load-bearing: losing it loses the config, the
YAML in the repo silently stops matching reality, and there is no obvious revert. Rejected —
this is a tool whose job is keeping CI up, so its failure modes matter more than its
elegance.

**C — config.yml is the baseline, the database is an overlay.** Chosen.

## Decision

Resolution order is `config.yml`/env → database override row → effective `PoolConfig`,
recomputed every tick.

- No override row for a field → the YAML value is used.
- "Reset" deletes the row rather than writing the old value back.
- **An empty or absent database is byte-identical to the pre-dashboard orchestrator.**
- `rm rorch.db` is a complete, obvious revert. Verified end-to-end.
- `RORCH_DB=off` disables store and dashboard entirely.

Identity fields (`name`, `owner`, `repo`, `scope`) and `pat` are not overridable — an
override may tune a pool, never repoint it at a different account. Pools created through the
API store `pat_env`, the *name* of an environment variable; the secret itself never enters
the database.

## Consequences

- Two places to look when a value is surprising. Mitigated by the dashboard showing
  overridden values as editable fields with a per-pool reset, and `GET /api/config`
  returning `base_pools` alongside `pools`.
- Config changes are not versioned in git. `GET /api/config/export` renders the effective
  config as YAML to copy back into the repo when a change should become permanent.
- A YAML-defined pool cannot be deleted from the database, only disabled — deleting the row
  would just resurrect it on the next tick.
- Validation had to be split: `validation_errors()` returns messages, `validate_pools()`
  keeps the old exit-on-failure behaviour for startup. The API rejects a bad value with 400
  rather than letting a tick crash later.
