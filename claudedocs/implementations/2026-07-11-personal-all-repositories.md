---
title: Personal account all-repositories runner pool implementation
status: completed
date: 2026-07-11
related:
  - ../prds/2026-07-11-personal-all-repositories.md
  - ../plans/2026-07-11-personal-all-repositories.md
---

# Personal account all-repositories runner pool implementation

RORCH now accepts `scope: personal` for a pool. Periodic discovery finds active repositories owned
by the authenticated GitHub account through the paginated `/user/repos` endpoint. The scaler
derives repository-scoped runtime pools, reuses the existing queue and runner APIs, and assigns
available capacity round-robin among repositories with demand.

`max_runners` is enforced across all discovered repositories in the personal pool. Personal
pools default `min_idle` to zero because GitHub cannot register one warm runner for every repo
in a personal account.

## Repository discovery cache

Repository lists use a bounded in-memory cache with a default 600-second TTL. Queue and runner
polling remains controlled by `github_poll_interval`. Empty repository lists are cached, successful
entries may be stale for at most the configured TTL during normal operation. A failed refresh
may serve the last known list beyond the TTL while retrying on the next poll. The cache is keyed
by pool, owner, and a digest of the PAT, is capped at 128 entries, and is intentionally cleared
by an orchestrator restart.

## Bounded parallel orchestration

Personal repository inspections run through a bounded thread pool (six workers by default).
Each inspection fetches GitHub runner state once and reuses that snapshot for online counts,
stuck-container detection, and offline-runner cleanup. Scaling remains a centralized phase so
the account-wide `max_runners` limit is calculated from a complete snapshot. Runner removals
and provisions then execute together through a separate bounded pool (four workers by default).
If any repository runner snapshot fails, the personal pool skips provisioning for that tick to
avoid making a capacity decision from incomplete state. Tick and per-repository durations are
included in operational logs. Docker cleanup helpers are also capped at four concurrent workers
to prevent nested repository checks from creating an unbounded number of cleanup threads.

## GitHub API rate budget

Each pool has a 60-second GitHub scan interval independent of the 15-second orchestrator loop.
The HTTP client stores authorized GET responses and their ETags in a bounded 2,048-entry cache;
unchanged `304 Not Modified` responses reuse the cached body without consuming primary quota.
GitHub requests are serialized to avoid secondary concurrency limits, and mutative requests are
paced one second apart.

Rate-limit headers form an enforced circuit breaker. The client preserves 100 requests by
default, stops until `X-RateLimit-Reset` when primary quota is depleted, honors `Retry-After`,
and uses bounded exponential cooldown for secondary limits without an explicit retry time.
Rate-limit failures propagate as typed errors, causing the pool to skip scaling instead of
treating unavailable queue data as zero jobs.

## Verification

- Ruff: passed.
- Pyright: passed with no errors (one environment warning for unavailable YAML source stubs).
- Pytest: 68 tests passed.
