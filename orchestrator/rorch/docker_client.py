# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Docker container management via CLI."""

import json
import logging
import subprocess
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from rorch.config import PoolConfig

log = logging.getLogger(__name__)


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

    def spawn_runner(self, pool: PoolConfig) -> bool:
        """Start a new ephemeral runner container."""
        uid = uuid.uuid4().hex[:8]
        name = f"{pool.container_prefix}-{uid}"
        log.info("  ▶  Spawning %s", name)

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

    def prune_images(self, until: str = "5h") -> None:
        """Remove dangling images older than the given threshold."""
        out, code = self._capture(["image", "prune", "-f", "--filter", f"until={until}"])
        if code == 0 and out:
            log.info("🧹 Image prune: %s", out)

    def prune_volumes(self, max_age_hours: float = 5.0) -> None:
        """Remove dangling volumes older than max_age_hours."""
        out, _ = self._capture(["volume", "ls", "--filter", "dangling=true", "-q"])
        if not out:
            return

        vol_ids = [v for v in out.split("\n") if v]
        if not vol_ids:
            return

        now = datetime.now(tz=timezone.utc)
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

        log.info("🧹 Removing %d dangling volume(s) older than %.0fh", len(to_remove), max_age_hours)

        def rm_vol(vid: str) -> None:
            _, code = self._capture(["volume", "rm", vid])
            if code == 0:
                log.info("🧹 Removed volume %s", vid)

        self._run_parallel(rm_vol, to_remove, timeout=15)

    @staticmethod
    def _run_parallel(fn: Callable[[str], None], items: list[str], timeout: int = 15) -> None:
        """Run a function on each item in parallel threads."""
        threads = [threading.Thread(target=fn, args=(item,), daemon=True) for item in items]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=timeout)
