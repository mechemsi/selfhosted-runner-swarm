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
from rorch.protocols import ContainerManager, RunnerAPIClient, RunnerInfo

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
    ) -> None:
        self._github = github
        self._docker = docker
        self._clock = clock
        self._max_total_runners = max_total_runners
        self._repository_cache: dict[tuple[str, str, bytes], RepositoryCacheEntry] = {}
        self._next_poll_at: dict[tuple[str, str, str, bytes], float] = {}

    def tick(self, pool: PoolConfig) -> None:
        """Run one complete scaling cycle for a pool."""
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
                self._tick_personal(pool, now)
            else:
                self._tick_single(pool)
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

    def _tick_single(self, pool: PoolConfig) -> None:
        inspection = self._inspect_pool(pool)
        self._log_inspection(inspection)
        to_spawn = self._calculate_spawn_count(pool, inspection)
        to_spawn = self._cap_to_global_limit(pool.name, to_spawn)
        self._run_runner_operations(
            [inspection],
            [(pool, to_spawn)],
            pool.runner_operation_workers,
        )

    def _tick_personal(self, pool: PoolConfig, now: float) -> None:
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

        self._docker.cleanup_stuck(
            prefix,
            running_names & online_names,
            timeout_minutes=3,
        )
        queued = self._github.get_queued_count(pool)

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

    @staticmethod
    def _log_inspection(inspection: PoolInspection) -> None:
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
