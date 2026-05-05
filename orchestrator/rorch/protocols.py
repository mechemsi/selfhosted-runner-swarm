# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Protocol interfaces for dependency inversion."""

from typing import Protocol

from rorch.config import PoolConfig


class RunnerAPIClient(Protocol):
    """Interface for GitHub Actions runner API operations."""

    def get_queued_count(self, pool: PoolConfig) -> int: ...

    def get_runner_stats(self, pool: PoolConfig) -> tuple[int, int]: ...

    def get_online_runner_names(self, pool: PoolConfig) -> set[str]: ...

    def deregister_offline_runners(self, pool: PoolConfig, running_names: set[str]) -> None: ...


class ContainerManager(Protocol):
    """Interface for container lifecycle operations."""

    def running_containers(self, prefix: str) -> list[str]: ...

    def cleanup_exited(self, prefix: str) -> None: ...

    def cleanup_stuck(
        self,
        prefix: str,
        online_names: set[str],
        timeout_minutes: int = 3,
    ) -> None: ...

    def spawn_runner(self, pool: PoolConfig) -> bool: ...

    def prune_images(self, until: str = "24h", all_unused: bool = True) -> None: ...

    def prune_build_cache(self, until: str = "24h") -> None: ...

    def prune_volumes(self, max_age_hours: float = 5.0) -> None: ...
