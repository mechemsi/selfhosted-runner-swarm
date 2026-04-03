# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Tests for the scaling logic."""

from unittest.mock import MagicMock

import pytest

from rorch.config import PoolConfig
from rorch.scaler import PoolScaler


@pytest.fixture
def mock_github() -> MagicMock:
    github = MagicMock()
    github.get_queued_count.return_value = 0
    github.get_runner_stats.return_value = (1, 0)  # 1 idle, 0 busy
    github.get_online_runner_names.return_value = set()
    github.deregister_offline_runners.return_value = None
    return github


@pytest.fixture
def mock_docker() -> MagicMock:
    docker = MagicMock()
    docker.running_containers.return_value = ["gh-runner-test-abc123"]
    docker.cleanup_exited.return_value = None
    docker.cleanup_stuck.return_value = None
    docker.spawn_runner.return_value = True
    return docker


@pytest.fixture
def scaler(mock_github: MagicMock, mock_docker: MagicMock) -> PoolScaler:
    return PoolScaler(mock_github, mock_docker)


class TestScalingDecision:
    """Test the desired runner calculation: min(max_runners, max(min_idle, busy + queued))"""

    def test_no_queued_no_busy_respects_min_idle(
        self, scaler: PoolScaler, mock_github: MagicMock, mock_docker: MagicMock, pool: PoolConfig
    ) -> None:
        """With no work, should maintain min_idle runners."""
        mock_github.get_queued_count.return_value = 0
        mock_github.get_runner_stats.return_value = (1, 0)
        mock_docker.running_containers.return_value = ["c1"]

        scaler.tick(pool)

        # 1 running, min_idle=1, desired=1 → no spawn
        mock_docker.spawn_runner.assert_not_called()

    def test_queued_jobs_trigger_spawn(
        self, scaler: PoolScaler, mock_github: MagicMock, mock_docker: MagicMock, pool: PoolConfig
    ) -> None:
        """Queued jobs should trigger spawning additional runners."""
        mock_github.get_queued_count.return_value = 3
        mock_github.get_runner_stats.return_value = (0, 1)  # 0 idle, 1 busy
        mock_docker.running_containers.return_value = ["c1"]

        scaler.tick(pool)

        # desired = min(5, max(1, 1+3)) = 4, running = 1 → spawn 3
        assert mock_docker.spawn_runner.call_count == 3

    def test_max_runners_caps_spawning(
        self, scaler: PoolScaler, mock_github: MagicMock, mock_docker: MagicMock, pool: PoolConfig
    ) -> None:
        """Should never spawn beyond max_runners."""
        pool.max_runners = 2
        mock_github.get_queued_count.return_value = 10
        mock_github.get_runner_stats.return_value = (0, 1)
        mock_docker.running_containers.return_value = ["c1"]

        scaler.tick(pool)

        # desired = min(2, max(1, 1+10)) = 2, running = 1 → spawn 1
        assert mock_docker.spawn_runner.call_count == 1

    def test_no_spawn_when_at_capacity(
        self, scaler: PoolScaler, mock_github: MagicMock, mock_docker: MagicMock, pool: PoolConfig
    ) -> None:
        """No spawning when already at max_runners."""
        pool.max_runners = 2
        mock_github.get_queued_count.return_value = 5
        mock_github.get_runner_stats.return_value = (0, 2)
        mock_docker.running_containers.return_value = ["c1", "c2"]

        scaler.tick(pool)

        mock_docker.spawn_runner.assert_not_called()

    def test_min_idle_ensures_warm_runners(
        self, scaler: PoolScaler, mock_github: MagicMock, mock_docker: MagicMock, pool: PoolConfig
    ) -> None:
        """min_idle should ensure spare runners even with no queued jobs."""
        pool.min_idle = 3
        mock_github.get_queued_count.return_value = 0
        mock_github.get_runner_stats.return_value = (0, 0)
        mock_docker.running_containers.return_value = []

        scaler.tick(pool)

        # desired = min(5, max(3, 0+0)) = 3, running = 0 → spawn 3
        assert mock_docker.spawn_runner.call_count == 3


class TestCleanupCalled:
    def test_tick_calls_cleanup(
        self, scaler: PoolScaler, mock_github: MagicMock, mock_docker: MagicMock, pool: PoolConfig
    ) -> None:
        """Tick should always run cleanup before scaling."""
        scaler.tick(pool)
        mock_docker.cleanup_exited.assert_called_once()
        mock_github.deregister_offline_runners.assert_called_once()
