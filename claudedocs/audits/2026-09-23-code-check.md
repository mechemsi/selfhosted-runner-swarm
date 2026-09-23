---
title: Code-quality audit (clean-code limits)
status: active
date: 2026-09-23
---

# Code check: rorch (whole repo)

First audit 2026-09-23 on `066af90`; re-measured after the fixes on `20c2527`. Scope: tracked source only
(orchestrator Python, dashboard TS/TSX, scripts), 46 files, 569 functions. Measured with the `code-check`
skill's `measure.py`; Python cognitive complexity is exact (complexipy), TS values marked `~` are
approximate.

Limits: clean-code defaults (function 20/40 lines, file 200/400, params 3/5, nesting 2/3, cognitive 8/15).
Repo overrides: none. `orchestrator/pyproject.toml` selects no complexity or size rules (no `C901`, no
`PLR`), so nothing in CI holds these numbers.

## Resolved

| PR | What | Was |
|---|---|---|
| #13 | Queue scan says when its count is a lower bound; failed pool-limit save keeps the form | GitHub errors counted as 0 queued; form wiped on failure |
| #14 | `record_tick` takes a `TickSnapshot`; `main()` split into named steps, characterization tests | 8 params; `main()` 96 lines, cognitive 45, 0% coverage |
| #15 | `server.py` split into `rorch/server/` with read/control blueprints | 822-line file; `create_app` cognitive 94 |
| #16 | Startup banner no longer logs `pat[:20]` | half of a 40-char PAT in plain logs |

Errors: 34 → 25. Functions and files over a ceiling: 24 → 19.

## Errors (over a ceiling)

1. `orchestrator/rorch/config.py:241` `validation_errors()`: cognitive 29.
   Fix: one small check function per rule, collected in a list and run in a loop.
2. `orchestrator/rorch/github_client.py:274` `list_repositories()`: cognitive 28, 46 lines.
   Fix: extract the per-repo decision into `_classify_repo(repo, pool)` and the two skip-summary log blocks
   into one `_log_skipped()`. Keep the public-repo why-comment.
3. `orchestrator/rorch/scaler.py:153` `_tick_personal()` (54 lines, cognitive 21),
   `scaler.py:295` `_run_runner_operations()` (45 lines, cognitive 16), `scaler.py:408` `_record_jobs()`
   (44 lines). Fix: extract the loop bodies into named steps.
4. `orchestrator/rorch/store.py:376` `sync_jobs()`: 68 lines, cognitive 18. `store.py:509` `open_store()`:
   nesting 4. Fix: extract the per-job upsert; guard clauses in `open_store`'s retry loop.
5. `orchestrator/rorch/github_client.py:94` `_request()`: 49 lines, nesting 4.
   Fix: move the 304 and rate-limit branches of the `HTTPError` handler into `_handle_http_error()`.
6. `orchestrator/rorch/docker_client.py:444` `spawn_runner()` (62 lines), `:373` `ensure_image()`
   (55 lines), `:537` `prune_volumes()` (cognitive 16). Fix: extract the `docker run` argv builder and the
   image-build path.
7. `orchestrator/rorch/server/payloads.py:62` `_state_payload()`: 55 lines (moved unchanged in #15).
   Fix: build the per-pool entry in its own function.
8. `orchestrator/rorch/config.py:163` `_load_from_yaml()`: 51 lines.
9. `dashboard/components/Pools.tsx:17` `Pools` (cognitive ~26, 43 lines), `Dashboard.tsx:16`
   `Dashboard` (cognitive ~22). Fix: a `PoolRow` component, and one `callApi()` helper for the duplicated
   fetch-then-alert handling in `save()` and `reset()`.
10. File length over 400: `docker_client.py` 597, `scaler.py` 550, `store.py` 545, `github_client.py` 446.
    These shrink as the functions above are extracted; `server.py` was the one worth splitting outright.

## Warnings

1. Broad `except Exception` in best-effort paths logs at `debug`: `scaler.py` record helpers and
   `docker_client.py:108`. Deliberate (the store is optional), but a store that stays broken is invisible at
   debug level. Log the first failure per tick at `warning`.
2. `docker_client.py:90` parser `except Exception: return None` and `prune_volumes`'
   `except Exception: continue`: catch `(ValueError, KeyError, IndexError)` so a real bug isn't silently
   turned into "unparseable".
3. `Any`: GitHub JSON flows inward as `Any` (`github_client.py`). Parse the few fields used into
   `TypedDict`s at the boundary.
4. `dashboard/lib/rorch.ts:53` `callRorch`: 5 parameters (target 3). Take an options object.
5. 91 warnings in total, mostly functions of 20–40 lines and 4–5 parameters; 17 are in
   `orchestrator/tests/` (tests get double length ceilings; none are errors).

## Notes

- Boolean parameters are passed by keyword everywhere (`paused=`, `draining=`, `write=`); making them
  keyword-only (`*,`) would lock that in.
- No `any` in the dashboard, no TODO/FIXME, no stray `print()` in the package.
- `resolve_token` logs a *generated* dashboard token once, deliberately: that is how the operator learns it.
- Nothing enforces these limits in CI. If wanted later: Ruff `C901` + `PLR0913` + complexipy for the
  orchestrator, `eslint-plugin-sonarjs` for the dashboard.

## Hotspots

| score | file |
|---|---|
| 28 | orchestrator/rorch/scaler.py |
| 23 | orchestrator/rorch/github_client.py |
| 21 | orchestrator/rorch/docker_client.py |
| 18 | orchestrator/rorch/store.py |
| 15 | orchestrator/tests/test_scaler.py |
| 10 | orchestrator/rorch/server/control_routes.py |
| 9 | orchestrator/rorch/config.py |
| 7 | dashboard/components/Pools.tsx |
| 6 | orchestrator/rorch/server/payloads.py |
| 4 | dashboard/components/Dashboard.tsx |

(error = 3 points, warning = 1)
