# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Tests for the scaling logic."""

import time
from pathlib import Path
from threading import Barrier
from unittest.mock import MagicMock

import pytest

from rorch.config import PoolConfig
from rorch.errors import GitHubRateLimitError
from rorch.protocols import RunnerInfo
from rorch.scaler import PoolScaler
from rorch.store import EVENT_DEREGISTER, PoolState, SqliteStore


def _online_runners(idle: int = 0, busy: int = 0) -> list[RunnerInfo]:
    runners = [
        RunnerInfo(id=index, name=f"idle-{index}", status="online", busy=False)
        for index in range(idle)
    ]
    runners.extend(
        RunnerInfo(id=idle + index, name=f"busy-{index}", status="online", busy=True)
        for index in range(busy)
    )
    return runners


@pytest.fixture
def mock_github() -> MagicMock:
    github = MagicMock()
    github.get_queued_count.return_value = 0
    github.list_runners.return_value = _online_runners(idle=1)
    github.deregister_runner.return_value = True
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
        mock_github.list_runners.return_value = _online_runners(idle=1)
        mock_docker.running_containers.return_value = ["c1"]

        scaler.tick(pool)

        # 1 running, min_idle=1, desired=1 → no spawn
        mock_docker.spawn_runner.assert_not_called()

    def test_queued_jobs_trigger_spawn(
        self, scaler: PoolScaler, mock_github: MagicMock, mock_docker: MagicMock, pool: PoolConfig
    ) -> None:
        """Queued jobs should trigger spawning additional runners."""
        mock_github.get_queued_count.return_value = 3
        mock_github.list_runners.return_value = _online_runners(busy=1)
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
        mock_github.list_runners.return_value = _online_runners(busy=1)
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
        mock_github.list_runners.return_value = _online_runners(busy=2)
        mock_docker.running_containers.return_value = ["c1", "c2"]

        scaler.tick(pool)

        mock_docker.spawn_runner.assert_not_called()

    def test_min_idle_ensures_warm_runners(
        self, scaler: PoolScaler, mock_github: MagicMock, mock_docker: MagicMock, pool: PoolConfig
    ) -> None:
        """min_idle should ensure spare runners even with no queued jobs."""
        pool.min_idle = 3
        mock_github.get_queued_count.return_value = 0
        mock_github.list_runners.return_value = []
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
        mock_github.list_runners.assert_called_once_with(pool)


class TestPersonalAccountScaling:
    def test_defers_repository_scans_until_poll_interval(
        self,
        mock_github: MagicMock,
        mock_docker: MagicMock,
        personal_pool: PoolConfig,
    ) -> None:
        personal_pool.github_poll_interval = 60
        clock = MagicMock(side_effect=[100.0, 120.0, 161.0])
        mock_github.list_repositories.return_value = ["alpha"]
        mock_github.list_runners.return_value = []
        mock_docker.running_containers.return_value = []

        scaler = PoolScaler(mock_github, mock_docker, clock=clock)
        scaler.tick(personal_pool)
        scaler.tick(personal_pool)
        scaler.tick(personal_pool)

        assert mock_github.list_runners.call_count == 2

    def test_rate_limit_pauses_later_scans(
        self,
        mock_github: MagicMock,
        mock_docker: MagicMock,
        personal_pool: PoolConfig,
    ) -> None:
        personal_pool.github_poll_interval = 0
        clock = MagicMock(side_effect=[100.0, 101.0])
        mock_github.list_repositories.return_value = ["alpha"]
        mock_github.list_runners.side_effect = GitHubRateLimitError(
            time.time() + 120,
            "rate limit test",
        )

        scaler = PoolScaler(mock_github, mock_docker, clock=clock)
        scaler.tick(personal_pool)
        scaler.tick(personal_pool)

        mock_github.list_runners.assert_called_once()

    def test_checks_repositories_in_parallel(
        self,
        mock_github: MagicMock,
        mock_docker: MagicMock,
        personal_pool: PoolConfig,
    ) -> None:
        personal_pool.repo_check_workers = 2
        mock_github.list_repositories.return_value = ["alpha", "beta"]
        mock_github.list_runners.return_value = []
        mock_docker.running_containers.return_value = []
        inspection_barrier = Barrier(2)
        inspected: list[str] = []

        def queued_count(pool: PoolConfig) -> int:
            inspection_barrier.wait(timeout=2)
            inspected.append(pool.repo)
            return 0

        mock_github.get_queued_count.side_effect = queued_count

        PoolScaler(mock_github, mock_docker).tick(personal_pool)

        assert sorted(inspected) == ["alpha", "beta"]

    def test_skips_scaling_when_any_runner_snapshot_fails(
        self,
        mock_github: MagicMock,
        mock_docker: MagicMock,
        personal_pool: PoolConfig,
    ) -> None:
        mock_github.list_repositories.return_value = ["alpha", "beta"]
        mock_github.list_runners.side_effect = lambda pool: None if pool.repo == "alpha" else []
        mock_docker.running_containers.return_value = []
        mock_github.get_queued_count.return_value = 3

        PoolScaler(mock_github, mock_docker).tick(personal_pool)

        mock_docker.spawn_runner.assert_not_called()

    def test_runs_removal_and_provisioning_in_parallel(
        self,
        mock_github: MagicMock,
        mock_docker: MagicMock,
        personal_pool: PoolConfig,
    ) -> None:
        personal_pool.runner_operation_workers = 2
        mock_github.list_repositories.return_value = ["alpha", "beta"]
        mock_docker.running_containers.return_value = []
        mock_github.get_queued_count.side_effect = lambda pool: 1 if pool.repo == "beta" else 0
        mock_github.list_runners.side_effect = lambda pool: (
            [
                RunnerInfo(
                    id=7,
                    name="gh-runner-personal-pool-alpha-old",
                    status="offline",
                    busy=False,
                )
            ]
            if pool.repo == "alpha"
            else []
        )
        operation_barrier = Barrier(2)
        completed: list[str] = []

        def deregister_runner(pool: PoolConfig, runner_id: int) -> bool:
            operation_barrier.wait(timeout=2)
            completed.append("deregister")
            return True

        def spawn_runner(pool: PoolConfig) -> bool:
            operation_barrier.wait(timeout=2)
            completed.append("spawn")
            return True

        mock_github.deregister_runner.side_effect = deregister_runner
        mock_docker.spawn_runner.side_effect = spawn_runner

        PoolScaler(mock_github, mock_docker).tick(personal_pool)

        assert sorted(completed) == ["deregister", "spawn"]

    def test_reuses_repository_discovery_until_ttl_expires(
        self,
        mock_github: MagicMock,
        mock_docker: MagicMock,
        personal_pool: PoolConfig,
    ) -> None:
        personal_pool.github_poll_interval = 0
        clock = MagicMock(side_effect=[100.0, 200.0])
        scaler = PoolScaler(mock_github, mock_docker, clock=clock)
        mock_github.list_repositories.return_value = ["alpha"]
        mock_github.get_queued_count.return_value = 0

        scaler.tick(personal_pool)
        scaler.tick(personal_pool)

        mock_github.list_repositories.assert_called_once_with(personal_pool)

    def test_refreshes_repository_discovery_after_ttl(
        self,
        mock_github: MagicMock,
        mock_docker: MagicMock,
        personal_pool: PoolConfig,
    ) -> None:
        personal_pool.github_poll_interval = 0
        personal_pool.repo_discovery_ttl = 600
        clock = MagicMock(side_effect=[100.0, 701.0])
        scaler = PoolScaler(mock_github, mock_docker, clock=clock)
        mock_github.list_repositories.side_effect = [["alpha"], ["beta"]]
        mock_github.get_queued_count.return_value = 0

        scaler.tick(personal_pool)
        mock_github.get_queued_count.reset_mock()
        scaler.tick(personal_pool)

        assert mock_github.list_repositories.call_count == 2
        assert mock_github.get_queued_count.call_args.args[0].repo == "beta"

    def test_uses_stale_repositories_when_refresh_fails(
        self,
        mock_github: MagicMock,
        mock_docker: MagicMock,
        personal_pool: PoolConfig,
    ) -> None:
        personal_pool.github_poll_interval = 0
        personal_pool.repo_discovery_ttl = 600
        clock = MagicMock(side_effect=[100.0, 701.0])
        scaler = PoolScaler(mock_github, mock_docker, clock=clock)
        mock_github.list_repositories.side_effect = [["alpha"], None]
        mock_github.get_queued_count.return_value = 0

        scaler.tick(personal_pool)
        mock_github.get_queued_count.reset_mock()
        scaler.tick(personal_pool)

        assert mock_github.list_repositories.call_count == 2
        assert mock_github.get_queued_count.call_args.args[0].repo == "alpha"

    def test_allocates_global_capacity_fairly_between_repositories(
        self,
        scaler: PoolScaler,
        mock_github: MagicMock,
        mock_docker: MagicMock,
        personal_pool: PoolConfig,
    ) -> None:
        personal_pool.max_runners = 3
        mock_github.list_repositories.return_value = ["alpha", "beta"]
        mock_docker.running_containers.side_effect = lambda prefix: (
            ["gh-runner-personal-pool-alpha-running"] if prefix.endswith("alpha") else []
        )
        mock_github.get_queued_count.side_effect = lambda pool: {
            "alpha": 2,
            "beta": 2,
        }[pool.repo]
        mock_github.list_runners.side_effect = lambda pool: {
            "alpha": _online_runners(busy=1),
            "beta": [],
        }[pool.repo]

        scaler.tick(personal_pool)

        spawned_repos = sorted(
            call.args[0].repo for call in mock_docker.spawn_runner.call_args_list
        )
        assert spawned_repos == ["alpha", "beta"]
        assert mock_github.list_runners.call_count == 2

    def test_does_not_exceed_account_pool_capacity(
        self,
        scaler: PoolScaler,
        mock_github: MagicMock,
        mock_docker: MagicMock,
        personal_pool: PoolConfig,
    ) -> None:
        personal_pool.max_runners = 1
        mock_github.list_repositories.return_value = ["alpha", "beta"]
        mock_docker.running_containers.side_effect = lambda prefix: (
            ["gh-runner-personal-pool-alpha-running"] if prefix.endswith("alpha") else []
        )
        mock_github.get_queued_count.side_effect = lambda pool: 3 if pool.repo == "beta" else 0
        mock_github.list_runners.return_value = []

        scaler.tick(personal_pool)

        mock_docker.spawn_runner.assert_not_called()

    def test_newly_discovered_repository_uses_repository_registration_scope(
        self,
        scaler: PoolScaler,
        mock_github: MagicMock,
        mock_docker: MagicMock,
        personal_pool: PoolConfig,
    ) -> None:
        mock_github.list_repositories.return_value = ["new-project"]
        mock_docker.running_containers.return_value = []
        mock_github.get_queued_count.return_value = 1
        mock_github.list_runners.return_value = []

        scaler.tick(personal_pool)

        spawned_pool = mock_docker.spawn_runner.call_args.args[0]
        assert spawned_pool.repo == "new-project"
        assert spawned_pool.api_runners_path == "/repos/test-user/new-project/actions/runners"


class TestGlobalRunnerCap:
    """max_total_runners caps spawning across all pools combined."""

    def test_caps_single_pool_spawn(
        self, mock_github: MagicMock, mock_docker: MagicMock, pool: PoolConfig
    ) -> None:
        """Pool wants 3 more runners but only 1 global slot remains."""
        mock_github.get_queued_count.return_value = 3
        mock_github.list_runners.return_value = _online_runners(busy=1)
        mock_docker.running_containers.side_effect = lambda prefix: (
            ["r1", "r2", "r3"] if prefix == "gh-runner" else ["c1"]
        )

        PoolScaler(mock_github, mock_docker, max_total_runners=4).tick(pool)

        # desired = min(5, max(1, 1+3)) = 4, pool has 1 → wants 3; global 3/4 → 1 slot
        assert mock_docker.spawn_runner.call_count == 1

    def test_no_spawn_when_global_cap_reached(
        self, mock_github: MagicMock, mock_docker: MagicMock, pool: PoolConfig
    ) -> None:
        mock_github.get_queued_count.return_value = 5
        mock_github.list_runners.return_value = _online_runners(busy=1)
        mock_docker.running_containers.side_effect = lambda prefix: (
            ["r1", "r2"] if prefix == "gh-runner" else ["c1"]
        )

        PoolScaler(mock_github, mock_docker, max_total_runners=2).tick(pool)

        mock_docker.spawn_runner.assert_not_called()

    def test_zero_cap_is_unlimited(
        self, mock_github: MagicMock, mock_docker: MagicMock, pool: PoolConfig
    ) -> None:
        mock_github.get_queued_count.return_value = 3
        mock_github.list_runners.return_value = _online_runners(busy=1)
        mock_docker.running_containers.return_value = ["c1"]

        PoolScaler(mock_github, mock_docker).tick(pool)

        assert mock_docker.spawn_runner.call_count == 3

    def test_caps_personal_pool_capacity(
        self, mock_github: MagicMock, mock_docker: MagicMock, personal_pool: PoolConfig
    ) -> None:
        """Personal capacity (4) shrinks to the global headroom (1)."""
        mock_github.list_repositories.return_value = ["alpha", "beta"]
        mock_github.list_runners.return_value = []
        mock_github.get_queued_count.return_value = 2
        mock_docker.running_containers.side_effect = lambda prefix: (
            ["r1", "r2", "r3"] if prefix == "gh-runner" else []
        )

        PoolScaler(mock_github, mock_docker, max_total_runners=4).tick(personal_pool)

        assert mock_docker.spawn_runner.call_count == 1


class TestPauseAndDrain:
    """Operator control flags set from the dashboard."""

    def test_paused_pool_does_no_work_at_all(
        self, scaler: PoolScaler, mock_github: MagicMock, mock_docker: MagicMock, pool: PoolConfig
    ) -> None:
        mock_github.get_queued_count.return_value = 5

        scaler.tick(pool, PoolState(paused=True))

        mock_docker.spawn_runner.assert_not_called()
        mock_github.list_runners.assert_not_called()

    def test_draining_pool_never_spawns(
        self, scaler: PoolScaler, mock_github: MagicMock, mock_docker: MagicMock, pool: PoolConfig
    ) -> None:
        mock_github.get_queued_count.return_value = 5
        mock_github.list_runners.return_value = _online_runners(busy=1)

        scaler.tick(pool, PoolState(draining=True))

        mock_docker.spawn_runner.assert_not_called()

    def test_draining_pool_still_cleans_up(
        self, scaler: PoolScaler, mock_docker: MagicMock, pool: PoolConfig
    ) -> None:
        scaler.tick(pool, PoolState(draining=True))

        mock_docker.cleanup_exited.assert_called_once()
        mock_docker.cleanup_stuck.assert_called_once()

    def test_draining_personal_pool_never_spawns(
        self,
        mock_github: MagicMock,
        mock_docker: MagicMock,
        personal_pool: PoolConfig,
    ) -> None:
        mock_github.list_repositories.return_value = ["alpha", "beta"]
        mock_github.list_runners.return_value = []
        mock_github.get_queued_count.return_value = 3
        mock_docker.running_containers.return_value = []

        PoolScaler(mock_github, mock_docker).tick(personal_pool, PoolState(draining=True))

        mock_docker.spawn_runner.assert_not_called()

    def test_default_state_scales_normally(
        self, scaler: PoolScaler, mock_github: MagicMock, mock_docker: MagicMock, pool: PoolConfig
    ) -> None:
        """No state argument must behave exactly as before pause/drain existed."""
        mock_github.get_queued_count.return_value = 2
        mock_github.list_runners.return_value = _online_runners(busy=1)
        mock_docker.running_containers.return_value = ["c1"]

        scaler.tick(pool)

        assert mock_docker.spawn_runner.call_count == 2


class TestStoreRecording:
    def test_records_snapshot_and_runner_status(
        self,
        mock_github: MagicMock,
        mock_docker: MagicMock,
        pool: PoolConfig,
        tmp_path: Path,
    ) -> None:
        store = SqliteStore(str(tmp_path / "rorch.db"))
        mock_github.get_queued_count.return_value = 2
        mock_github.list_runners.return_value = [
            RunnerInfo(id=1, name="gh-runner-test-pool-abc", status="online", busy=True)
        ]
        mock_docker.running_containers.return_value = ["gh-runner-test-pool-abc"]

        PoolScaler(mock_github, mock_docker, store=store).tick(pool)

        snapshot = store.latest_snapshots()["test-pool"]
        assert snapshot["queued"] == 2
        assert snapshot["busy"] == 1
        assert store.runner_status()["gh-runner-test-pool-abc"]["busy"] is True

    def test_records_deregistration(
        self,
        mock_github: MagicMock,
        mock_docker: MagicMock,
        pool: PoolConfig,
        tmp_path: Path,
    ) -> None:
        store = SqliteStore(str(tmp_path / "rorch.db"))
        mock_github.list_runners.return_value = [
            RunnerInfo(id=7, name="gh-runner-test-pool-dead", status="offline", busy=False)
        ]
        mock_docker.running_containers.return_value = []

        PoolScaler(mock_github, mock_docker, store=store).tick(pool)

        assert any(e["event"] == EVENT_DEREGISTER for e in store.recent_events())

    def test_store_failure_never_breaks_a_tick(
        self, mock_github: MagicMock, mock_docker: MagicMock, pool: PoolConfig
    ) -> None:
        broken = MagicMock()
        broken.record_tick.side_effect = RuntimeError("disk full")
        broken.replace_runner_status.side_effect = RuntimeError("disk full")
        mock_github.get_queued_count.return_value = 2
        mock_github.list_runners.return_value = _online_runners(busy=1)
        mock_docker.running_containers.return_value = ["c1"]

        PoolScaler(mock_github, mock_docker, store=broken).tick(pool)

        assert mock_docker.spawn_runner.call_count == 2
