"""Pool scaling logic — the core orchestration loop."""

import logging
import threading
import time

from rorch.config import PoolConfig
from rorch.protocols import ContainerManager, RunnerAPIClient

log = logging.getLogger(__name__)

SPAWN_STAGGER_SECONDS = 0.3


class PoolScaler:
    """Manages runner scaling for a single pool.

    Depends on abstract RunnerAPIClient and ContainerManager interfaces,
    not concrete implementations (Dependency Inversion Principle).
    """

    def __init__(self, github: RunnerAPIClient, docker: ContainerManager) -> None:
        self._github = github
        self._docker = docker

    def tick(self, pool: PoolConfig) -> None:
        """Run one scaling cycle for a pool."""
        self._cleanup(pool)
        stats = self._collect_stats(pool)
        self._log_stats(pool, stats)
        self._scale(pool, stats)

    def _cleanup(self, pool: PoolConfig) -> None:
        """Remove exited containers and deregister offline runners."""
        prefix = pool.container_prefix
        self._docker.cleanup_exited(prefix)

        running = set(self._docker.running_containers(prefix))
        self._github.deregister_offline_runners(pool, running)

        # Kill containers that never came online
        online_names = self._github.get_online_runner_names(pool)
        self._docker.cleanup_stuck(prefix, running & online_names, timeout_minutes=3)

    def _collect_stats(self, pool: PoolConfig) -> dict[str, int]:
        running = self._docker.running_containers(pool.container_prefix)
        n_queued = self._github.get_queued_count(pool)
        n_idle, n_busy = self._github.get_runner_stats(pool)
        return {
            "running": len(running),
            "queued": n_queued,
            "idle": n_idle,
            "busy": n_busy,
            "online": n_idle + n_busy,
        }

    @staticmethod
    def _log_stats(pool: PoolConfig, stats: dict[str, int]) -> None:
        log.info(
            "[%s] %s | containers=%d  online=%d (idle=%d busy=%d)  queued=%d",
            pool.name,
            pool.display,
            stats["running"],
            stats["online"],
            stats["idle"],
            stats["busy"],
            stats["queued"],
        )

    def _scale(self, pool: PoolConfig, stats: dict[str, int]) -> None:
        """Decide how many runners to spawn and do it."""
        desired = min(pool.max_runners, max(pool.min_idle, stats["busy"] + stats["queued"]))
        to_spawn = max(0, desired - stats["running"])

        if to_spawn == 0:
            log.info(
                "[%s] ✓ OK  online=%d  queued=%d  containers=%d/%d",
                pool.name,
                stats["online"],
                stats["queued"],
                stats["running"],
                pool.max_runners,
            )
            return

        log.info(
            "[%s] online=%d queued=%d containers=%d/%d → spawning %d in parallel",
            pool.name,
            stats["online"],
            stats["queued"],
            stats["running"],
            pool.max_runners,
            to_spawn,
        )
        self._spawn_parallel(pool, to_spawn)

    def _spawn_parallel(self, pool: PoolConfig, count: int) -> None:
        """Spawn multiple runners in parallel with a small stagger."""
        threads = [
            threading.Thread(target=self._docker.spawn_runner, args=(pool,), daemon=True)
            for _ in range(count)
        ]
        for t in threads:
            t.start()
            time.sleep(SPAWN_STAGGER_SECONDS)
        for t in threads:
            t.join(timeout=60)
