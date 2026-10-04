# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Independent tick lanes, so one slow pool never delays another.

The daemon used to tick every pool in turn and then sleep, so a pool whose
GitHub scan took 40s pushed every other pool's next decision back by 40s. Now
each pool (and the global cleanup) is a *lane*: the main loop submits a lane's
work when it wakes, and never waits for it. A lane runs at most once at a time,
so a pool still busy from its last tick is skipped rather than ticked twice
concurrently, and the scaler never sees the same pool from two threads.
"""

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

log = logging.getLogger(__name__)

# A lane busy for this long is logged as stalled each time it is skipped.
# Python threads cannot be killed, so a hung lane holds its worker until the
# blocking call returns; the warning is what makes that visible.
STALL_WARNING_SECONDS = 600.0


class TickLanes:
    """A bounded worker pool that runs at most one job per lane at a time."""

    def __init__(
        self,
        max_workers: int,
        name: str = "tick",
        clock: Callable[[], float] = time.monotonic,
        stall_after: float = STALL_WARNING_SECONDS,
    ) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=name)
        self._clock = clock
        self._stall_after = stall_after
        self._lock = threading.Lock()
        self._running: dict[str, tuple[Future[None], float]] = {}

    def submit(self, lane: str, job: Callable[[], None]) -> bool:
        """Start `job` on `lane` unless that lane's previous job is unfinished.

        Returns whether it was started. Exceptions from `job` are logged and
        swallowed: one lane failing must not affect any other.
        """
        with self._lock:
            current = self._running.get(lane)
            if current is not None and not current[0].done():
                busy_for = self._clock() - current[1]
                if busy_for >= self._stall_after:
                    log.warning(
                        "[%s] Still running after %.0fs; skipping this round", lane, busy_for
                    )
                else:
                    log.debug("[%s] Previous tick still running; skipping this round", lane)
                return False
            future = self._executor.submit(self._run, lane, job)
            self._running[lane] = (future, self._clock())
            return True

    def busy_lanes(self) -> list[str]:
        with self._lock:
            return sorted(lane for lane, (future, _) in self._running.items() if not future.done())

    def forget_except(self, lanes: set[str]) -> None:
        """Drop finished bookkeeping for lanes that no longer exist (a removed pool)."""
        with self._lock:
            for lane in [name for name in self._running if name not in lanes]:
                if self._running[lane][0].done():
                    del self._running[lane]

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)

    @staticmethod
    def _run(lane: str, job: Callable[[], None]) -> None:
        try:
            job()
        except Exception:
            log.error("[%s] Unhandled error", lane, exc_info=True)
