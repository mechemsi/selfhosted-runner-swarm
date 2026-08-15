# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Docker container management via CLI."""

import json
import logging
import os
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
from rorch.protocols import ContainerInfo
from rorch.store import (
    EVENT_AGED_KILL,
    EVENT_EXIT,
    EVENT_MANUAL_STOP,
    EVENT_SPAWN,
    EVENT_SPAWN_FAILED,
    EVENT_STUCK_KILL,
    Store,
)

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

# The runner user must be in the group owning the socket or `docker info` fails
# inside the runner and it exits before registering. The GID differs per host,
# so it is baked in at build time and stamped on the image to detect staleness.
DOCKER_SOCKET = "/var/run/docker.sock"
GID_LABEL = "rorch.docker_gid"


def _host_docker_gid() -> int:
    """GID owning the mounted Docker socket (the host's docker group)."""
    return os.stat(DOCKER_SOCKET).st_gid


def _pinned_runner_version(image: str) -> str:
    """Agent version a `gh-runner:2.328.0`-style tag asks for, else empty.

    Only dotted numeric tags count: `latest`, `dev` and custom images have no
    version to pass through to the build.
    """
    _, _, tag = image.partition(":")
    parts = tag.split(".")
    if len(parts) >= 2 and all(part.isdigit() for part in parts):
        return tag
    return ""


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

    def __init__(self, store: Store | None = None) -> None:
        self._build_lock = threading.Lock()
        self._build_retry_at: dict[str, float] = {}
        self._store = store

    def _record(self, event: str, container: str = "", pool: str = "", reason: str = "") -> None:
        """Record a lifecycle event when a store is attached; never fail the caller."""
        if self._store is None:
            return
        try:
            self._store.record_event(event, container=container, pool=pool, reason=reason)
        except Exception:
            log.debug("Could not record %s for %s", event, container, exc_info=True)

    @staticmethod
    def _pool_of(container: str) -> str:
        """Pool name embedded in `gh-runner-{pool}-{uid}`."""
        parts = container.split("-")
        return "-".join(parts[2:-1]) if len(parts) > 3 else ""

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

    def container_details(self, prefix: str) -> list[ContainerInfo]:
        """Running containers with the detail the dashboard renders."""
        out, _ = self._capture(
            [
                "ps",
                "--filter",
                f"name=^{prefix}-",
                "--format",
                "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.RunningFor}}",
            ]
        )
        if not out:
            return []
        details: list[ContainerInfo] = []
        for line in out.split("\n"):
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            name, image, status, running_for = parts
            details.append(
                ContainerInfo(
                    name=name,
                    image=image,
                    status=status,
                    running_for=running_for,
                    minutes=_parse_running_minutes(running_for) or 0.0,
                )
            )
        return details

    def stop_container(self, name: str) -> bool:
        """Remove one runner container. Already gone counts as success."""
        _, code = self._capture(["rm", "-f", "-v", name])
        if code == 0:
            log.info("  ⏹  Stopped %s", name)
            self._record(EVENT_MANUAL_STOP, container=name, pool=self._pool_of(name))
            return True
        # `docker rm` on a container that no longer exists is the desired end
        # state, so report success rather than making callers special-case it.
        if name not in self.running_containers(name.rsplit("-", 1)[0]):
            log.info("  ⏹  %s already gone", name)
            return True
        log.warning("  ✗  Could not stop %s", name)
        return False

    def container_logs(self, name: str, tail: int = 200) -> str:
        """Tail of a runner's combined output — the runner reports fatals on stderr."""
        result = subprocess.run(
            ["docker", "logs", "--tail", str(max(1, min(tail, 5000))), name],
            capture_output=True,
            text=True,
        )
        return result.stdout + result.stderr

    @staticmethod
    def _last_logs(name: str, lines: int = 5) -> str:
        """Both streams of a container's tail — the runner reports fatals on stderr."""
        r = subprocess.run(
            ["docker", "logs", "--tail", str(lines), name], capture_output=True, text=True
        )
        return " | ".join(x.strip() for x in (r.stdout + r.stderr).splitlines() if x.strip())

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
                "{{.Names}}\t{{.Status}}",
            ]
        )
        if not out:
            return

        rows = [line.split("\t") for line in out.split("\n") if line]
        if not rows:
            return

        def rm(row: list[str]) -> None:
            name = row[0]
            status = row[1] if len(row) > 1 else ""
            # A runner that died before registering is otherwise removed without
            # a trace, and the pool just respawns it forever.
            if status and not status.startswith("Exited (0)"):
                log.warning("  ⚠  %s %s — %s", name, status, self._last_logs(name))
            self._capture(["rm", "-v", name])
            log.info("  🗑  rm %s", name)
            self._record(EVENT_EXIT, container=name, pool=self._pool_of(name), reason=status)

        self._run_parallel(rm, rows, timeout=15)

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
            self._record(
                EVENT_STUCK_KILL,
                container=name,
                pool=self._pool_of(name),
                reason=f"never came online within {timeout_minutes}m",
            )

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
            self._record(
                EVENT_AGED_KILL,
                container=name,
                pool=self._pool_of(name),
                reason=f"exceeded {max_minutes}m lifetime ceiling",
            )

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

    def _image_state(self, image: str, gid: int) -> str | None:
        """None if the image is usable, else why it has to be (re)built."""
        out, code = self._capture(
            ["image", "inspect", "-f", f'{{{{index .Config.Labels "{GID_LABEL}"}}}}', image]
        )
        if code != 0:
            return "missing"
        if out.strip() != str(gid):
            return f"built for docker GID {out.strip() or 'unknown'}, host socket is {gid}"
        return None

    def ensure_image(self, image: str) -> bool:
        """Check the image is present and usable, building it from the mounted context if not.

        Two ways a runner dies before it ever registers: the image is gone (spawn
        tries to pull a locally-built tag from a registry it was never pushed to)
        or it was built for the wrong docker GID (the runner can't read the
        socket, entrypoint aborts, cleanup removes the evidence).
        # ponytail: one build context for every pool, so a pool with a custom
        # runner_image just pauses instead of building the wrong Dockerfile.
        """
        with self._build_lock:
            gid = _host_docker_gid()
            reason = self._image_state(image, gid)
            if reason is None:
                return True

            if not Path(RUNNER_BUILD_CONTEXT, "Dockerfile").exists():
                log.error(
                    "✗ Image %s %s and no build context at %s — spawns paused (build it "
                    "on the host: docker build --build-arg DOCKER_GID=%d -t %s ./runner-image)",
                    image,
                    reason,
                    RUNNER_BUILD_CONTEXT,
                    gid,
                    image,
                )
                return False

            retry_at = self._build_retry_at.get(image, 0.0)
            if time.monotonic() < retry_at:
                log.error(
                    "✗ Image %s %s, last build failed — spawns paused, retry in %.0fs",
                    image,
                    reason,
                    retry_at - time.monotonic(),
                )
                return False

            log.warning(
                "Image %s %s — building from %s with DOCKER_GID=%d (takes a few minutes)",
                image,
                reason,
                RUNNER_BUILD_CONTEXT,
                gid,
            )
            build = [
                "build",
                "--build-arg",
                f"DOCKER_GID={gid}",
                "--label",
                f"{GID_LABEL}={gid}",
                "-t",
                image,
                RUNNER_BUILD_CONTEXT,
            ]
            # A pool pinned to gh-runner:2.328.0 must get that agent, not the
            # Dockerfile default under a misleading tag.
            pinned = _pinned_runner_version(image)
            if pinned:
                build[1:1] = ["--build-arg", f"RUNNER_VERSION={pinned}"]
                log.info("Building %s with pinned runner agent %s", image, pinned)
            if self._exec(build) != 0:
                self._build_retry_at[image] = time.monotonic() + BUILD_RETRY_SECONDS
                log.error(
                    "✗ Build of %s failed — spawns paused for %ds", image, BUILD_RETRY_SECONDS
                )
                return False

            log.info("✓ Built %s (docker GID %d)", image, gid)
            return True

    def spawn_runner(self, pool: PoolConfig) -> bool:
        """Start a new ephemeral runner container."""
        if not self.ensure_image(pool.runner_image):
            self._record(
                EVENT_SPAWN_FAILED,
                pool=pool.name,
                reason=f"image {pool.runner_image} unusable",
            )
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
            pool.network_mode,
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
            self._record(EVENT_SPAWN, container=name, pool=pool.name, reason=pool.display)
            return True
        log.error("  ✗  Failed to start %s", name)
        self._record(
            EVENT_SPAWN_FAILED, container=name, pool=pool.name, reason=f"docker run exit {code}"
        )
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
    def _run_parallel[T](fn: Callable[[T], None], items: list[T], timeout: int = 15) -> None:
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
