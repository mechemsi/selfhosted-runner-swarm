# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Pool scaling logic — the core orchestration loop."""

import hashlib
import logging
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from rorch.config import PoolConfig
from rorch.errors import GitHubRateLimitError
from rorch.protocols import ContainerManager, JobInfo, RunnerAPIClient, RunnerInfo
from rorch.store import EVENT_DEREGISTER, PoolState, SqliteStore

log = logging.getLogger(__name__)

MAX_REPOSITORY_CACHE_ENTRIES = 128

# Matches every pool's containers: each prefix is "gh-runner-{pool-name}".
GLOBAL_CONTAINER_PREFIX = "gh-runner"


@dataclass(frozen=True)
class RepositoryCacheEntry:
    """Last successful personal-account repository discovery."""

    repositories: tuple[str, ...]
    expires_at: float


@dataclass(frozen=True)
class PoolInspection:
    """One consistent repository snapshot used for cleanup and scaling."""

    pool: PoolConfig
    running: int
    queued: int
    idle: int
    busy: int
    offline_runners: tuple[RunnerInfo, ...]
    duration_seconds: float

    @property
    def online(self) -> int:
        return self.idle + self.busy


class PoolScaler:
    """Manage runner scaling through bounded inspection and operation pools."""

    def __init__(
        self,
        github: RunnerAPIClient,
        docker: ContainerManager,
        clock: Callable[[], float] = time.monotonic,
        max_total_runners: int = 0,
        store: SqliteStore | None = None,
    ) -> None:
        self._github = github
        self._docker = docker
        self._clock = clock
        self._max_total_runners = max_total_runners
        self._store = store
        self._repository_cache: dict[tuple[str, str, bytes], RepositoryCacheEntry] = {}
        self._next_poll_at: dict[tuple[str, str, str, bytes], float] = {}

    @property
    def max_total_runners(self) -> int:
        return self._max_total_runners

    @max_total_runners.setter
    def max_total_runners(self, value: int) -> None:
        """Kept settable so a config change from the API applies on the next tick."""
        self._max_total_runners = value

    def tick(self, pool: PoolConfig, state: PoolState | None = None) -> None:
        """Run one complete scaling cycle for a pool.

        A paused pool is skipped entirely; a draining one is still inspected and
        cleaned up but never provisions, so busy runners finish their jobs and
        the pool empties itself.
        """
        state = state or PoolState()
        if state.paused:
            log.info("[%s] Paused — skipping tick", pool.name)
            return

        started = time.monotonic()
        now = self._clock()
        pool_key = self._pool_key(pool)
        next_poll_at = self._next_poll_at.get(pool_key, 0.0)
        if now < next_poll_at:
            log.debug("[%s] GitHub scan deferred for %.1fs", pool.name, next_poll_at - now)
            return
        if pool.github_poll_interval > 0:
            self._next_poll_at[pool_key] = now + pool.github_poll_interval

        try:
            if pool.is_personal_level:
                self._tick_personal(pool, now, draining=state.draining)
            else:
                self._tick_single(pool, draining=state.draining)
        except GitHubRateLimitError as error:
            retry_after = max(1.0, error.retry_at_epoch - time.time())
            self._next_poll_at[pool_key] = max(
                self._next_poll_at.get(pool_key, 0.0),
                now + retry_after,
            )
            log.warning(
                "[%s] GitHub scan paused for %.0fs: %s",
                pool.name,
                retry_after,
                error.reason,
            )
        finally:
            log.info("[%s] Tick completed in %.2fs", pool.name, time.monotonic() - started)

    def _cap_to_global_limit(self, pool_name: str, to_spawn: int) -> int:
        """Cap spawn count so total containers across all pools stay under the ceiling."""
        if self._max_total_runners <= 0 or to_spawn <= 0:
            return to_spawn
        total = len(self._docker.running_containers(GLOBAL_CONTAINER_PREFIX))
        headroom = max(0, self._max_total_runners - total)
        if to_spawn > headroom:
            log.warning(
                "[%s] Global runner cap %d reached (%d running); capping spawn %d → %d",
                pool_name,
                self._max_total_runners,
                total,
                to_spawn,
                headroom,
            )
        return min(to_spawn, headroom)

    def _tick_single(self, pool: PoolConfig, draining: bool = False) -> None:
        inspection = self._inspect_pool(pool)
        self._log_inspection(inspection)
        if draining:
            log.info("[%s] Draining — no provisioning, busy runners finish", pool.name)
            to_spawn = 0
        else:
            to_spawn = self._calculate_spawn_count(pool, inspection)
        to_spawn = self._cap_to_global_limit(pool.name, to_spawn)
        self._run_runner_operations(
            [inspection],
            [(pool, to_spawn)],
            pool.runner_operation_workers,
        )

    def _tick_personal(self, pool: PoolConfig, now: float, draining: bool = False) -> None:
        """Scale repository runners under one personal-account capacity limit."""
        repo_names = self._get_personal_repositories(pool, now)
        if not repo_names:
            log.warning("[%s] No accessible repositories found for %s", pool.name, pool.owner)
            return

        repo_pools = [pool.for_repository(repo_name) for repo_name in repo_names]
        inspections = self._inspect_pools_parallel(repo_pools, pool.repo_check_workers)
        if inspections is None:
            log.error("[%s] Repository inspection failed; skipping scaling", pool.name)
            return

        for inspection in inspections:
            self._log_inspection(inspection)

        total_running = sum(inspection.running for inspection in inspections)
        if draining:
            log.info("[%s] Draining — no provisioning, busy runners finish", pool.name)
            capacity = 0
        else:
            capacity = self._cap_to_global_limit(
                pool.name, max(0, pool.max_runners - total_running)
            )
        allocations = {inspection.pool.repo: 0 for inspection in inspections}
        ordered = sorted(inspections, key=lambda item: (-item.queued, item.pool.repo))

        while capacity > 0:
            allocated_in_round = False
            for inspection in ordered:
                desired = inspection.busy + inspection.queued
                needed = max(0, desired - inspection.running)
                repo = inspection.pool.repo
                if allocations[repo] >= needed:
                    continue
                allocations[repo] += 1
                capacity -= 1
                allocated_in_round = True
                if capacity == 0:
                    break
            if not allocated_in_round:
                break

        spawn_allocations = [
            (inspection.pool, allocations[inspection.pool.repo]) for inspection in ordered
        ]
        total_to_spawn = sum(count for _, count in spawn_allocations)
        log.info(
            "[%s] personal pool | repos=%d containers=%d/%d spawning=%d",
            pool.name,
            len(inspections),
            total_running,
            pool.max_runners,
            total_to_spawn,
        )
        self._run_runner_operations(
            inspections,
            spawn_allocations,
            pool.runner_operation_workers,
        )

    def _inspect_pools_parallel(
        self, pools: list[PoolConfig], max_workers: int
    ) -> list[PoolInspection] | None:
        """Inspect independent repositories concurrently with a hard worker cap."""
        worker_count = min(max_workers, len(pools))
        inspections: list[PoolInspection] = []
        failed = False
        rate_limit_error: GitHubRateLimitError | None = None

        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="repo-check",
        ) as executor:
            futures = {executor.submit(self._inspect_pool, pool): pool for pool in pools}
            for future in as_completed(futures):
                repo_pool = futures[future]
                try:
                    inspections.append(future.result())
                except GitHubRateLimitError as error:
                    rate_limit_error = error
                except Exception:
                    failed = True
                    log.error(
                        "[%s] Repository inspection failed",
                        repo_pool.name,
                        exc_info=True,
                    )

        if rate_limit_error is not None:
            raise rate_limit_error
        if failed:
            return None
        return sorted(inspections, key=lambda item: item.pool.repo)

    def _inspect_pool(self, pool: PoolConfig) -> PoolInspection:
        """Fetch Docker and GitHub state once for one pool."""
        started = time.monotonic()
        prefix = pool.container_prefix
        self._docker.cleanup_exited(prefix)
        running_names = set(self._docker.running_containers(prefix))

        runners = self._github.list_runners(pool)
        if runners is None:
            raise RuntimeError(f"failed to list runners for {pool.display}")
        runner_list = runners
        online = [runner for runner in runner_list if runner.status == "online"]
        online_names = {runner.name for runner in online}
        idle = sum(1 for runner in online if not runner.busy)
        busy = sum(1 for runner in online if runner.busy)
        offline_runners = tuple(
            runner
            for runner in runner_list
            if runner.status == "offline"
            and runner.name.startswith(prefix)
            and runner.name not in running_names
        )

        self._record_runner_status(pool, runner_list, prefix)

        self._docker.cleanup_stuck(
            prefix,
            running_names & online_names,
            timeout_minutes=3,
        )
        # The queue scan already fetches every job of every active run, so
        # collecting them here is free and answers "what did this runner run".
        jobs: list[JobInfo] | None = [] if self._store is not None else None
        queued = self._github.get_queued_count(pool, jobs)
        if jobs is not None:
            self._record_jobs(pool, jobs)

        return PoolInspection(
            pool=pool,
            running=len(running_names),
            queued=queued,
            idle=idle,
            busy=busy,
            offline_runners=offline_runners,
            duration_seconds=time.monotonic() - started,
        )

    def _run_runner_operations(
        self,
        inspections: list[PoolInspection],
        spawn_allocations: list[tuple[PoolConfig, int]],
        max_workers: int,
    ) -> None:
        """Run deregistration and provisioning together through one bounded pool."""
        operation_count = sum(len(item.offline_runners) for item in inspections) + sum(
            count for _, count in spawn_allocations
        )
        if operation_count == 0:
            return

        worker_count = min(max_workers, operation_count)
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="runner-op",
        ) as executor:
            futures: dict[Future[bool], str] = {}
            deregistration_jobs = [
                (inspection.pool, runner)
                for inspection in inspections
                for runner in inspection.offline_runners
            ]
            spawn_jobs = [
                spawn_pool for spawn_pool, count in spawn_allocations for _ in range(count)
            ]

            while deregistration_jobs or spawn_jobs:
                if deregistration_jobs:
                    deregister_pool, runner = deregistration_jobs.pop()
                    future = executor.submit(
                        self._deregister_runner,
                        deregister_pool,
                        runner,
                    )
                    futures[future] = f"deregister {runner.name}"
                if spawn_jobs:
                    spawn_pool = spawn_jobs.pop()
                    future = executor.submit(self._docker.spawn_runner, spawn_pool)
                    futures[future] = f"spawn for {spawn_pool.display}"

            for future in as_completed(futures):
                operation = futures[future]
                try:
                    if not future.result():
                        log.warning("Runner operation failed: %s", operation)
                except Exception:
                    log.error("Runner operation raised: %s", operation, exc_info=True)

    def _deregister_runner(self, pool: PoolConfig, runner: RunnerInfo) -> bool:
        log.info("  🧹  Deregistering offline runner: %s (id=%s)", runner.name, runner.id)
        if self._github.deregister_runner(pool, runner.id):
            log.info("  ✓  Deregistered %s", runner.name)
            self._record_event(
                EVENT_DEREGISTER, container=runner.name, pool=pool.name, reason="offline"
            )
            return True
        log.warning("  ✗  Failed to deregister %s", runner.name)
        return False

    def _get_personal_repositories(self, pool: PoolConfig, now: float) -> list[str]:
        """Return cached repositories, refreshing after the configured TTL."""
        token_digest = hashlib.sha256(pool.pat.encode()).digest()
        cache_key = (pool.name, pool.owner.lower(), token_digest)
        cached = self._repository_cache.get(cache_key)

        if cached is not None and now < cached.expires_at:
            log.debug("[%s] Repository discovery cache hit", pool.name)
            return list(cached.repositories)

        repositories = self._github.list_repositories(pool)
        if repositories is None:
            if cached is not None:
                log.warning("[%s] Repository refresh failed; using stale cache", pool.name)
                return list(cached.repositories)
            return []

        if pool.repo_discovery_ttl == 0:
            self._repository_cache.pop(cache_key, None)
            return repositories

        if (
            cache_key not in self._repository_cache
            and len(self._repository_cache) >= MAX_REPOSITORY_CACHE_ENTRIES
        ):
            oldest_key = min(
                self._repository_cache,
                key=lambda key: self._repository_cache[key].expires_at,
            )
            del self._repository_cache[oldest_key]

        self._repository_cache[cache_key] = RepositoryCacheEntry(
            repositories=tuple(repositories),
            expires_at=now + pool.repo_discovery_ttl,
        )
        log.info(
            "[%s] Discovered %d repositories (cache TTL=%ds)",
            pool.name,
            len(repositories),
            pool.repo_discovery_ttl,
        )
        return repositories

    @staticmethod
    def _pool_key(pool: PoolConfig) -> tuple[str, str, str, bytes]:
        return (
            pool.name,
            pool.owner.lower(),
            pool.repo.lower(),
            hashlib.sha256(pool.pat.encode()).digest(),
        )

    def _record_jobs(self, pool: PoolConfig, jobs: list[JobInfo]) -> None:
        """Persist what ran, and log one line per job start and finish.

        Without this the logs only ever said how many runners exist — never
        which workflow a runner picked up, or how it ended.
        """
        if self._store is None:
            return
        try:
            started, finished = self._store.sync_jobs(
                pool.name,
                [
                    (
                        job.job_id,
                        job.repo,
                        job.workflow,
                        job.job_name,
                        job.runner,
                        job.status,
                        job.conclusion,
                        job.url,
                        job.started_at,
                    )
                    for job in jobs
                ],
            )
        except Exception:
            log.debug("Could not record jobs for %s", pool.name, exc_info=True)
            return

        for job in started:
            log.info(
                "  ▶  %s picked up %s · %s / %s",
                job["runner"] or "(unassigned)",
                job["repo"],
                job["workflow"],
                job["job_name"],
            )
        for job in finished:
            conclusion = job.get("conclusion") or "unknown"
            mark = "✓" if conclusion == "success" else "✗" if conclusion == "failure" else "•"
            log.info(
                "  %s  %s · %s / %s finished on %s (%s)",
                mark,
                job["repo"],
                job["workflow"],
                job["job_name"],
                job["runner"] or "(unassigned)",
                conclusion,
            )

    def _record_runner_status(
        self, pool: PoolConfig, runners: list[RunnerInfo], prefix: str
    ) -> None:
        """Cache what GitHub reports so the dashboard can show busy/idle per runner.

        Without this the UI would have to hit the GitHub API on every page
        refresh, which competes with the scaler for the same rate budget.
        """
        if self._store is None:
            return
        try:
            self._store.replace_runner_status(
                pool.name,
                [
                    (runner.name, runner.status, runner.busy)
                    for runner in runners
                    if runner.name.startswith(prefix)
                ],
            )
        except Exception:
            log.debug("Could not record runner status for %s", pool.name, exc_info=True)

    def _record_snapshot(self, inspection: PoolInspection) -> None:
        """Persist one tick's numbers for the dashboard and its history charts."""
        if self._store is None:
            return
        try:
            self._store.record_tick(
                pool=inspection.pool.name,
                display=inspection.pool.display,
                containers=inspection.running,
                online=inspection.online,
                idle=inspection.idle,
                busy=inspection.busy,
                queued=inspection.queued,
                duration=inspection.duration_seconds,
            )
        except Exception:
            log.debug("Could not record snapshot for %s", inspection.pool.name, exc_info=True)

    def _record_event(self, event: str, container: str, pool: str, reason: str) -> None:
        """Persist a lifecycle event; a store failure must never break a tick."""
        if self._store is None:
            return
        try:
            self._store.record_event(event, container=container, pool=pool, reason=reason)
        except Exception:
            log.debug("Could not record %s for %s", event, container, exc_info=True)

    def _log_inspection(self, inspection: PoolInspection) -> None:
        self._record_snapshot(inspection)
        log.info(
            "[%s] %s | containers=%d online=%d (idle=%d busy=%d) queued=%d check=%.2fs",
            inspection.pool.name,
            inspection.pool.display,
            inspection.running,
            inspection.online,
            inspection.idle,
            inspection.busy,
            inspection.queued,
            inspection.duration_seconds,
        )

    @staticmethod
    def _calculate_spawn_count(pool: PoolConfig, inspection: PoolInspection) -> int:
        desired = min(
            pool.max_runners,
            max(pool.min_idle, inspection.busy + inspection.queued),
        )
        to_spawn = max(0, desired - inspection.running)
        if to_spawn == 0:
            log.info(
                "[%s] ✓ OK online=%d queued=%d containers=%d/%d",
                pool.name,
                inspection.online,
                inspection.queued,
                inspection.running,
                pool.max_runners,
            )
        else:
            log.info(
                "[%s] online=%d queued=%d containers=%d/%d → provisioning %d",
                pool.name,
                inspection.online,
                inspection.queued,
                inspection.running,
                pool.max_runners,
                to_spawn,
            )
        return to_spawn
