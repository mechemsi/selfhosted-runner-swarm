---
title: Observability and control dashboard
status: completed
date: 2026-08-15
related: [../decisions/001-config-yml-stays-the-default.md]
---

# Observability and control dashboard

Built the six Notion tasks of the dashboard series plus the four older open tasks in the
Github Runner ORchestrator project.

## What shipped

| Area | Module | Notes |
|------|--------|-------|
| Persistence | `rorch/store.py` | stdlib `sqlite3`, WAL, one lock. Snapshots, events, overrides, control state, runner status, idempotency, audit. Retention prune on the existing periodic-prune tick. |
| Config overlay | `rorch/resolver.py` | `config.yml` → DB override → `EffectiveConfig`, rebuilt every tick. |
| HTTP | `rorch/server.py` | Flask in a daemon thread. Read API, control actions, config CRUD, Prometheus `/metrics`, YAML export. |
| UI | `rorch/dashboard.html` | One file, vanilla JS polling, inline SVG sparklines. No npm, no build step, no vendored library. |
| Control | `docker_client.stop_container` / `container_details` / `container_logs`, `scaler.tick(pool, PoolState)` | pause / drain / stop / restart / ±1 / protect. |

## Decisions worth remembering

**SQLite, not Postgres.** Single writer, a handful of rows per tick, and `rm rorch.db`
is the revert path. A server database would add an operational dependency to a tool whose
job is to keep CI running.

**No Store protocol.** The codebase uses protocols for DI (`RunnerAPIClient`,
`ContainerManager`) because both have real alternate implementations in tests. The store
has one implementation and tests drive the real thing against a tmp file, which is better
testing than a fake. `store: SqliteStore | None` instead.

**No htmx.** The plan said one vendored JS file; ~40 lines of `fetch` + DOM turned out to
be less code than vendoring, and keeps the dependency count at zero.

**Failures degrade, never propagate.** `open_store` returns `None` on an unusable path,
and every store call site swallows exceptions at debug level. A full disk must not stop
runners being provisioned.

**Idempotency caches successes only.** A failed spawn returns 502 and is *not* replayed —
a retry of a failed operation should genuinely retry. Confirmed live: two `scale_up`
attempts with the same key both executed while the image was missing, and both were
audited.

**Busy-runner guard.** `POST /stop` on a runner GitHub reports as busy returns 409 unless
`confirm: true`. Runner busy state comes from a `runner_status` table written each tick,
so the dashboard never spends GitHub rate budget on a page refresh.

## Security

The API port is root-equivalent — the process holds `/var/run/docker.sock`. Mitigations:
loopback-only publish in compose, `server.start()` refuses a non-loopback bind without a
token, `_is_runner_container()` rejects anything outside the `gh-runner-*` namespace
(verified live against the host's real `openbook-database-1`), PATs never enter the
database or any response, and every mutating request writes an audit row.

## Other tasks closed in the same pass

- **Runner agent / Node24** — the pin was already 2.335.1 (≥ 2.327). Made it an `ARG`,
  stamped `rorch.runner_version`, and added a CI assertion plus a weekly upstream watcher
  so it cannot silently rot again.
- **Multi-version runners** — `scripts/build-runner.sh` tags `gh-runner:<version>`, and
  only the newest claims `:latest`. `ensure_image` parses a pinned tag back into
  `--build-arg RUNNER_VERSION` so an auto-build of `gh-runner:2.328.0` cannot produce a
  different agent under that tag.
- **Host port collisions** — added per-pool `network_mode` (`host` default, `bridge` to
  isolate). Documented all three workarounds; the workflow-side fix is usually right.
- **One-command setup** — `Makefile` (no new deps).
- **Docker builds in CI** — `docker-lint` (`buildx build --check` + hadolint) everywhere,
  `docker-build-scan` for real builds gated on lint+test.

## Verification

- 168 tests pass (83 pre-existing, unchanged, + 85 new); pyright 0 errors; ruff clean.
- `actionlint` clean on both workflows.
- Live end-to-end against a built image: auth (401/200), state, metrics, config patch →
  effective on next tick, validation rejection, reset → back to `config.yml`, pause survives
  restart, override survives restart, and **deleting the database returned the orchestrator
  to exactly its `config.yml` behaviour**.
