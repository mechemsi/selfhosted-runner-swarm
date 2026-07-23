# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Docker container management via CLI."""

import json
import logging
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from rorch.config import PoolConfig

log = logging.getLogger(__name__)

MAX_DOCKER_CLEANUP_WORKERS = 4

# The orchestrator's own container shares the gh-runner- prefix, so the global
# sweeps below would otherwise match (and the aged reaper would kill) it.
ORCHESTRATOR_CONTAINER = "gh-runner-orchestrator"

# runner-image/ mounted into the orchestrator by docker-compose. Present → a
# missing runner image is rebuilt instead of hanging the pool.
RUNNER_BUILD_CONTEXT = "/app/runner-image"
BUILD_RETRY_SECONDS = 300

# Applied to the runner image so `image prune -a` can't delete it while no
# runner happens to be running (that is how it goes missing in the first place).
KEEP_LABEL = "rorch.keep=true"


def _parse_running_minutes(running_for: str) -> float | None:
    """Parse Docker's human-readable running time into minutes."""
    try:
        parts = running_for.lower().split()
        if len(parts) < 2:
            return None
        value = float(parts[0])
        unit = parts[1]
        if "second" in unit:
            return value / 60
        if "minute" in unit:
            return value
        if "hour" in unit:
            return value * 60
        if "day" in unit:
            return value * 60 * 24
        return None
    except Exception:
        return None


class DockerClient:
    """Manages runner container lifecycle via Docker CLI."""

    def __init__(self) -> None:
        self._build_lock = threading.Lock()
        self._build_retry_at: dict[str, float] = {}

    @staticmethod
    def _capture(args: list[str]) -> tuple[str, int]:
        """Run docker command and capture output."""
        cmd = ["docker", *args]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.stdout.strip(), r.returncode

    @staticmethod
    def _exec(args: list[str]) -> int:
        """Run docker command without capturing output."""
        cmd = ["docker", *args]
        return subprocess.run(cmd).returncode

    def running_containers(self, prefix: str) -> list[str]:
        """List names of running containers matching the prefix."""
        out, _ = self._capture(
            [
                "ps",
                "--filter",
                f"name=^{prefix}-",
                "--format",
                "{{.Names}}",
            ]
        )
        return [n for n in out.split("\n") if n] if out else []

    def cleanup_exited(self, prefix: str) -> None:
        """Remove exited containers matching the prefix."""
        out, _ = self._capture(
            [
                "ps",
                "-a",
                "--filter",
                f"name=^{prefix}-",
                "--filter",
                "status=exited",
                "--format",
                "{{.Names}}",
            ]
        )
        if not out:
            return

        names = [n for n in out.split("\n") if n]
        if not names:
            return

        def rm(name: str) -> None:
            self._capture(["rm", "-v", name])
            log.info("  🗑  rm %s", name)

        self._run_parallel(rm, names, timeout=15)

    def cleanup_stuck(self, prefix: str, online_names: set[str], timeout_minutes: int = 3) -> None:
        """Kill containers that never came online within the timeout."""
        out, _ = self._capture(
            [
                "ps",
                "--filter",
                f"name=^{prefix}-",
                "--format",
                "{{.Names}}\t{{.RunningFor}}",
            ]
        )
        if not out:
            return

        to_kill: list[str] = []
        for line in out.split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) != 2:
                continue
            name, running_for = parts

            if name in online_names:
                continue

            minutes = _parse_running_minutes(running_for)
            if minutes is None or minutes < timeout_minutes:
                continue

            log.warning("  ⚠️  Stuck: %s (running %s, never came online)", name, running_for)
            to_kill.append(name)

        if not to_kill:
            return

        log.info("  Killing %d stuck container(s) in parallel", len(to_kill))

        def kill(name: str) -> None:
            self._capture(["rm", "-f", "-v", name])
            log.info("  💀  Killed %s", name)

        self._run_parallel(kill, to_kill, timeout=15)

    def cleanup_aged(
        self, prefix: str, max_minutes: int, exclude: frozenset[str] = frozenset()
    ) -> None:
        """Kill runner containers older than max_minutes, regardless of state.

        Backstop for leaks the ephemeral-exit and stuck reapers miss: idle runners
        over-provisioned during a burst that never got a job (so they never hit the
        --once exit), and jobs that hung after coming online.
        # ponytail: hard wall-clock ceiling, not idle-time. Set it above your
        # longest job or it aborts real work; per-pool ceiling if one pool needs
        # longer. Coarser fix than true desired-vs-running scale-down, but catches
        # every leak class with one number.
        """
        if max_minutes <= 0:
            return
        out, _ = self._capture(
            [
                "ps",
                "--filter",
                f"name=^{prefix}-",
                "--format",
                "{{.Names}}\t{{.RunningFor}}",
            ]
        )
        if not out:
            return

        to_kill: list[str] = []
        for line in out.split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) != 2:
                continue
            name, running_for = parts
            if name in exclude:
                continue
            minutes = _parse_running_minutes(running_for)
            if minutes is None or minutes < max_minutes:
                continue
            log.warning(
                "  ⏰ Aged: %s (alive %s, exceeds %dm ceiling)", name, running_for, max_minutes
            )
            to_kill.append(name)

        if not to_kill:
            return

        log.info("  Killing %d aged container(s) in parallel", len(to_kill))

        def kill(name: str) -> None:
            self._capture(["rm", "-f", "-v", name])
            log.info("  💀  Killed aged %s", name)

        self._run_parallel(kill, to_kill, timeout=15)

    # Persistent cache mounts shared across ephemeral runners in a pool.
    # Each entry maps a host path (under /opt/runner-cache/<pool>/) to
    # a container path.  Concurrent runners may read/write simultaneously
    # but the tools below all use lock files or atomic writes.
    _CACHE_MOUNTS: ClassVar[list[tuple[str, str]]] = [
        ("cache", "/home/runner/.cache"),  # pip, yarn, go-build, …
        ("npm", "/home/runner/.npm"),  # npm
        ("composer", "/home/runner/.composer"),  # composer (PHP)
        ("nuget", "/home/runner/.nuget"),  # dotnet
    ]

    def ensure_image(self, image: str) -> bool:
        """Check the image exists locally, building it from the mounted context if not.

        Without this `docker run` tries to pull a locally-built image from a
        registry it was never pushed to: every spawn fails slowly, forever.
        # ponytail: one build context for every pool, so a pool with a custom
        # runner_image just pauses instead of building the wrong Dockerfile.
        """
        with self._build_lock:
            if self._capture(["image", "inspect", image])[1] == 0:
                return True

            if not Path(RUNNER_BUILD_CONTEXT, "Dockerfile").exists():
                log.error(
                    "✗ Image %s missing and no build context at %s — spawns paused "
                    "(build it on the host: docker build -t %s ./runner-image)",
                    image,
                    RUNNER_BUILD_CONTEXT,
                    image,
                )
                return False

            retry_at = self._build_retry_at.get(image, 0.0)
            if time.monotonic() < retry_at:
                log.error(
                    "✗ Image %s missing, last build failed — spawns paused, retry in %.0fs",
                    image,
                    retry_at - time.monotonic(),
                )
                return False

            log.warning(
                "Image %s missing — building from %s (takes a few minutes)",
                image,
                RUNNER_BUILD_CONTEXT,
            )
            if self._exec(["build", "-t", image, RUNNER_BUILD_CONTEXT]) != 0:
                self._build_retry_at[image] = time.monotonic() + BUILD_RETRY_SECONDS
                log.error(
                    "✗ Build of %s failed — spawns paused for %ds", image, BUILD_RETRY_SECONDS
                )
                return False

            log.info("✓ Built %s", image)
            return True

    def spawn_runner(self, pool: PoolConfig) -> bool:
        """Start a new ephemeral runner container."""
        if not self.ensure_image(pool.runner_image):
            return False

        uid = uuid.uuid4().hex[:8]
        name = f"{pool.container_prefix}-{uid}"
        log.info("  ▶  Spawning %s", name)

        cache_base = f"/opt/runner-cache/{pool.container_prefix}"

        args = [
            "run",
            "-d",
            "--name",
            name,
            "--restart",
            "no",
            "--network",
            "host",
            "--memory",
            pool.memory_limit,
            "--memory-swap",
            pool.memory_limit,
            "--pids-limit",
            "512",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-v",
            "/opt/hostedtoolcache:/opt/hostedtoolcache",
        ]

        for host_dir, container_dir in self._CACHE_MOUNTS:
            args.extend(["-v", f"{cache_base}/{host_dir}:{container_dir}"])

        args.extend(
            [
                "-e",
                f"GITHUB_PAT={pool.pat}",
                "-e",
                f"GITHUB_OWNER={pool.owner}",
                "-e",
                f"GITHUB_REPO={pool.repo}",
                "-e",
                f"RUNNER_NAME={name}",
                "-e",
                f"RUNNER_LABELS={pool.runner_labels}",
            ]
        )

        if pool.cpu_limit > 0:
            args.extend(["--cpus", str(pool.cpu_limit)])

        args.append(pool.runner_image)
        code = self._exec(args)

        cpu_info = f" cpus={pool.cpu_limit}" if pool.cpu_limit > 0 else " cpus=unlimited"
        if code == 0:
            log.info("  ✓  Started %s  (mem=%s%s)", name, pool.memory_limit, cpu_info)
            return True
        log.error("  ✗  Failed to start %s", name)
        return False

    def prune_images(self, until: str = "24h", all_unused: bool = True) -> None:
        """Remove unused images older than the given threshold.

        With all_unused=True (default), removes both dangling images and tagged
        images not referenced by any container. The until filter protects images
        created more recently than the threshold.
        """
        args = ["image", "prune"]
        if all_unused:
            args.append("-a")
        args.extend(["-f", "--filter", f"until={until}", "--filter", f"label!={KEEP_LABEL}"])
        out, code = self._capture(args)
        if code == 0 and out:
            log.info("🧹 Image prune: %s", out)

    def prune_build_cache(self, until: str = "24h") -> None:
        """Remove build cache entries older than the given threshold."""
        out, code = self._capture(["builder", "prune", "-f", "--filter", f"until={until}"])
        if code == 0 and out:
            log.info("🧹 Build cache prune: %s", out)

    def prune_volumes(self, max_age_hours: float = 5.0) -> None:
        """Remove dangling volumes older than max_age_hours."""
        out, _ = self._capture(["volume", "ls", "--filter", "dangling=true", "-q"])
        if not out:
            return

        vol_ids = [v for v in out.split("\n") if v]
        if not vol_ids:
            return

        now = datetime.now(tz=UTC)
        to_remove: list[str] = []
        for vid in vol_ids:
            inspect_out, code = self._capture(["volume", "inspect", vid])
            if code != 0:
                continue
            try:
                info = json.loads(inspect_out)
                created = info[0]["CreatedAt"] if isinstance(info, list) else info["CreatedAt"]
                # Docker returns e.g. "2026-04-04T06:12:34+00:00" or "2026-04-04T06:12:34Z"
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_hours = (now - created_dt).total_seconds() / 3600
                if age_hours >= max_age_hours:
                    to_remove.append(vid)
            except Exception:
                continue

        if not to_remove:
            return

        log.info(
            "🧹 Removing %d dangling volume(s) older than %.0fh", len(to_remove), max_age_hours
        )

        def rm_vol(vid: str) -> None:
            _, code = self._capture(["volume", "rm", vid])
            if code == 0:
                log.info("🧹 Removed volume %s", vid)

        self._run_parallel(rm_vol, to_remove, timeout=15)

    @staticmethod
    def _run_parallel(fn: Callable[[str], None], items: list[str], timeout: int = 15) -> None:
        """Run cleanup operations through a bounded worker pool."""
        if not items:
            return

        executor = ThreadPoolExecutor(
            max_workers=min(MAX_DOCKER_CLEANUP_WORKERS, len(items)),
            thread_name_prefix="docker-cleanup",
        )
        futures = [executor.submit(fn, item) for item in items]
        completed, pending = wait(futures, timeout=timeout)
        for future in completed:
            try:
                future.result()
            except Exception:
                log.error("Docker cleanup operation failed", exc_info=True)
        if pending:
            log.warning("%d Docker cleanup operation(s) exceeded %ds", len(pending), timeout)
        executor.shutdown(wait=False, cancel_futures=True)
