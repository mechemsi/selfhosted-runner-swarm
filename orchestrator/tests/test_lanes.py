# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Tests for independent pool tick lanes."""

import threading
import time
from collections.abc import Iterator
from dataclasses import replace

import pytest

import rorch.__main__ as daemon
from rorch.config import PoolConfig
from rorch.lanes import TickLanes
from rorch.resolver import EffectiveConfig
from rorch.store import PoolState

WAIT = 5.0


@pytest.fixture
def lanes() -> Iterator[TickLanes]:
    lanes = TickLanes(max_workers=4)
    yield lanes
    lanes.shutdown(wait=True)


class FakeScaler:
    """Records ticks; pools named in `blocked` wait until `release` is set."""

    def __init__(self, blocked: set[str]) -> None:
        self.blocked = blocked
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.ticked: list[str] = []
        self.done: dict[str, threading.Event] = {}
        self.active: dict[str, int] = {}
        self.overlapped = False
        self.max_total_runners = 0

    def finished(self, name: str) -> threading.Event:
        with self.lock:
            return self.done.setdefault(name, threading.Event())

    def tick(self, pool: PoolConfig, state: PoolState | None = None) -> None:
        with self.lock:
            self.active[pool.name] = self.active.get(pool.name, 0) + 1
            self.overlapped |= self.active[pool.name] > 1
            self.ticked.append(pool.name)
        try:
            if pool.name in self.blocked:
                assert self.release.wait(WAIT)
            if pool.name == "broken":
                raise RuntimeError("bad PAT")
        finally:
            with self.lock:
                self.active[pool.name] -= 1
            self.finished(pool.name).set()


def _effective(*pools: PoolConfig) -> EffectiveConfig:
    return EffectiveConfig(
        pools=list(pools),
        max_total_runners=0,
        max_runner_lifetime=0,
        paused=False,
        states={},
        protected=frozenset(),
    )


def _pools(base: PoolConfig, *names: str) -> list[PoolConfig]:
    return [replace(base, name=name) for name in names]


def test_slow_pool_does_not_delay_a_fast_pool(lanes: TickLanes, pool: PoolConfig) -> None:
    slow, fast = _pools(pool, "slow", "fast")
    scaler = FakeScaler(blocked={"slow"})

    daemon._submit_pool_ticks(scaler, _effective(slow, fast), lanes)  # type: ignore[arg-type]

    # The fast pool finishes while the slow one is still mid-tick.
    assert scaler.finished("fast").wait(WAIT)
    assert not scaler.finished("slow").is_set()
    # Next loop: the fast pool ticks again, the slow pool is not re-entered.
    scaler.finished("fast").clear()
    daemon._submit_pool_ticks(scaler, _effective(slow, fast), lanes)  # type: ignore[arg-type]
    assert scaler.finished("fast").wait(WAIT)
    assert scaler.ticked.count("slow") == 1
    assert scaler.ticked.count("fast") == 2
    scaler.release.set()


def test_same_pool_never_ticks_twice_concurrently(lanes: TickLanes, pool: PoolConfig) -> None:
    scaler = FakeScaler(blocked={"test-pool"})
    effective = _effective(pool)

    for _ in range(5):
        daemon._submit_pool_ticks(scaler, effective, lanes)  # type: ignore[arg-type]
    assert lanes.busy_lanes() == ["test-pool"]
    scaler.release.set()
    assert scaler.finished("test-pool").wait(WAIT)
    lanes.shutdown(wait=True)

    assert scaler.ticked == ["test-pool"]
    assert scaler.overlapped is False


def _wait_idle(lanes: TickLanes) -> None:
    deadline = time.monotonic() + WAIT
    while lanes.busy_lanes() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert lanes.busy_lanes() == []


def test_lane_runs_again_once_its_previous_tick_finished(lanes: TickLanes) -> None:
    ran: list[int] = []

    assert lanes.submit("pool", lambda: ran.append(1))
    _wait_idle(lanes)
    assert lanes.submit("pool", lambda: ran.append(2))
    _wait_idle(lanes)

    assert ran == [1, 2]


def test_one_raising_pool_does_not_stop_the_others(
    lanes: TickLanes, pool: PoolConfig, caplog: pytest.LogCaptureFixture
) -> None:
    broken, healthy = _pools(pool, "broken", "healthy")
    scaler = FakeScaler(blocked=set())

    daemon._submit_pool_ticks(scaler, _effective(broken, healthy), lanes)  # type: ignore[arg-type]
    lanes.shutdown(wait=True)

    assert sorted(scaler.ticked) == ["broken", "healthy"]
    assert "[broken] Unhandled error" in caplog.text


def test_a_failed_lane_can_run_again(lanes: TickLanes) -> None:
    calls: list[str] = []

    def boom() -> None:
        calls.append("boom")
        raise RuntimeError("boom")

    lanes.submit("pool", boom)
    _wait_idle(lanes)
    lanes.submit("pool", lambda: calls.append("ok"))
    _wait_idle(lanes)

    assert calls == ["boom", "ok"]


def test_stalled_lane_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    now = [0.0]
    lanes = TickLanes(max_workers=1, clock=lambda: now[0], stall_after=600)
    release = threading.Event()
    lanes.submit("personal", lambda: release.wait(WAIT) and None)

    now[0] = 30.0
    assert lanes.submit("personal", lambda: None) is False
    assert "Still running" not in caplog.text

    now[0] = 601.0
    assert lanes.submit("personal", lambda: None) is False
    assert "[personal] Still running after 601s" in caplog.text
    release.set()
    lanes.shutdown(wait=True)


def test_removed_pools_are_forgotten_once_finished(lanes: TickLanes) -> None:
    lanes.submit("gone", lambda: None)
    lanes.shutdown(wait=True)

    lanes.forget_except({"kept"})

    assert lanes._running == {}
