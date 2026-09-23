# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Characterization tests for the daemon loop in rorch.__main__.

main() runs forever, so each test lets exactly one tick happen: time.sleep raises _StopLoopError.
Everything with I/O (GitHub, Docker, the store, the dashboard server) is a MagicMock;
config, pools and the resolved EffectiveConfig are the real types.
"""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

import rorch.__main__ as daemon
from rorch.config import PoolConfig
from rorch.docker_client import ORCHESTRATOR_CONTAINER
from rorch.resolver import EffectiveConfig
from rorch.scaler import GLOBAL_CONTAINER_PREFIX
from rorch.store import PoolState


class _StopLoopError(Exception):
    """Raised by the patched sleep to end main() after one tick."""


class Wired:
    """The mocks main() was wired with, plus the resolved config it will see."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, pools: list[PoolConfig]) -> None:
        self.pools = pools
        self.effective = EffectiveConfig(
            pools=pools,
            max_total_runners=7,
            max_runner_lifetime=60,
            paused=False,
            states={pools[0].name: PoolState(draining=True)},
            protected=frozenset({"gh-runner-keep-me"}),
        )
        self.store = MagicMock(name="store")
        self.github = MagicMock(name="github")
        self.github.is_public_repo.return_value = False
        self.docker = MagicMock(name="docker")
        self.scaler = MagicMock(name="scaler")
        self.resolver = MagicMock(name="resolver")
        self.resolver.resolve.side_effect = lambda: self.effective
        self.server_start = MagicMock(name="server.start")
        self.sleeps: list[float] = []

        for name, value in {
            "load_config": lambda: pools,
            "validate_pools": lambda _pools: None,
            "load_max_total_runners": lambda: 7,
            "load_max_runner_lifetime": lambda: 60,
            "open_store": lambda _url: self.store,
            "ConfigResolver": lambda *_args: self.resolver,
            "GitHubClient": lambda **_kwargs: self.github,
            "DockerClient": lambda **_kwargs: self.docker,
            "PoolScaler": lambda *_args, **_kwargs: self.scaler,
        }.items():
            monkeypatch.setattr(daemon, name, value)
        monkeypatch.setattr(daemon.server, "start", self.server_start)
        monkeypatch.setattr(daemon.server, "resolve_token", lambda _store: "token")
        monkeypatch.setattr(daemon.server, "resolve_readonly_token", lambda: "readonly")
        monkeypatch.setattr(daemon.time, "sleep", self._sleep)

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        raise _StopLoopError

    def run_one_tick(self) -> None:
        with pytest.raises(_StopLoopError):
            daemon.main()


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, pool: PoolConfig, org_pool: PoolConfig) -> Wired:
    # POLL_INTERVAL=900 makes prune_every 1, so housekeeping runs on the first tick.
    monkeypatch.setenv("POLL_INTERVAL", "900")
    monkeypatch.delenv("RORCH_DB", raising=False)
    monkeypatch.delenv("HISTORY_RETENTION_DAYS", raising=False)
    return Wired(monkeypatch, [pool, org_pool])


def test_ticks_every_pool_with_its_state_then_sleeps(wired: Wired) -> None:
    wired.run_one_tick()

    ticked = [(call.args[0].name, call.args[1]) for call in wired.scaler.tick.call_args_list]
    assert ticked == [
        (wired.pools[0].name, PoolState(draining=True)),
        (wired.pools[1].name, PoolState()),
    ]
    assert wired.sleeps == [900]


def test_scaler_gets_the_effective_global_cap(wired: Wired) -> None:
    wired.run_one_tick()

    assert wired.scaler.max_total_runners == 7


def test_paused_skips_pool_ticks_but_still_cleans_up(wired: Wired) -> None:
    wired.effective = replace(wired.effective, paused=True)

    wired.run_one_tick()

    wired.scaler.tick.assert_not_called()
    wired.docker.cleanup_aged.assert_called_once()


def test_one_failing_pool_does_not_stop_the_others(wired: Wired) -> None:
    wired.scaler.tick.side_effect = [RuntimeError("boom"), None]

    wired.run_one_tick()

    assert wired.scaler.tick.call_count == 2


def test_aged_cleanup_spares_the_orchestrator_and_protected_containers(wired: Wired) -> None:
    wired.run_one_tick()

    wired.docker.cleanup_aged.assert_called_once_with(
        GLOBAL_CONTAINER_PREFIX,
        60,
        exclude=frozenset({ORCHESTRATOR_CONTAINER, "gh-runner-keep-me"}),
    )


def test_failed_aged_cleanup_does_not_stop_the_loop(wired: Wired) -> None:
    wired.docker.cleanup_aged.side_effect = RuntimeError("docker down")

    wired.run_one_tick()

    assert wired.sleeps == [900]


def test_housekeeping_prunes_docker_and_history(wired: Wired) -> None:
    wired.run_one_tick()

    wired.docker.prune_images.assert_called_once_with()
    wired.docker.prune_build_cache.assert_called_once_with()
    wired.docker.prune_volumes.assert_called_once_with()
    wired.store.prune.assert_called_once_with(14)
    wired.store.prune_idempotency.assert_called_once_with()


def test_failed_docker_prune_still_prunes_history(wired: Wired) -> None:
    wired.docker.prune_images.side_effect = RuntimeError("docker down")

    wired.run_one_tick()

    wired.docker.prune_volumes.assert_not_called()
    wired.store.prune.assert_called_once_with(14)


def test_housekeeping_waits_for_its_interval(wired: Wired, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLL_INTERVAL", "15")

    wired.run_one_tick()

    wired.docker.prune_images.assert_not_called()
    wired.store.prune.assert_not_called()


def test_builds_each_runner_image_once_before_the_first_tick(wired: Wired) -> None:
    wired.run_one_tick()

    built = sorted(call.args[0] for call in wired.docker.ensure_image.call_args_list)
    assert built == sorted({p.runner_image for p in wired.pools})


def test_starts_the_dashboard_when_a_store_exists(wired: Wired) -> None:
    wired.run_one_tick()

    wired.server_start.assert_called_once()
    deps = wired.server_start.call_args.args[0]
    assert deps.store is wired.store
    assert (deps.token, deps.readonly_token) == ("token", "readonly")


def test_without_a_store_no_dashboard_and_no_history_prune(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RORCH_DB", "off")

    wired.run_one_tick()

    wired.server_start.assert_not_called()
    wired.store.prune.assert_not_called()
    wired.docker.prune_images.assert_called_once_with()


def test_failed_history_prune_does_not_stop_the_loop(wired: Wired) -> None:
    wired.store.prune.side_effect = RuntimeError("db gone")

    wired.run_one_tick()

    assert wired.sleeps == [900]
