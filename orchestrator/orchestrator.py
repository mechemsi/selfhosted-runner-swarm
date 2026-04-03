#!/usr/bin/env python3
"""
GitHub Actions Runner Orchestrator — Multi-repo / Multi-org
"""

import os
import sys
import time
import json
import threading
import uuid
import logging
import subprocess
import urllib.request
import urllib.error
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)


# ── Pool config ───────────────────────────────────────────────────────────────
@dataclass
class PoolConfig:
    name: str
    pat: str
    owner: str
    repo: str = ""
    max_runners: int = 3
    min_idle: int = 1
    runner_labels: str = "self-hosted,linux,x64,docker"
    runner_image: str = "gh-runner:latest"
    memory_limit: str = "2g"     # per runner container RAM cap
    cpu_limit: float = 0.0       # 0 = no limit (recommended), or set cores e.g. 2.0

    @property
    def is_org_level(self):
        return not self.repo

    @property
    def display(self):
        return f"{self.owner} [org]" if self.is_org_level else f"{self.owner}/{self.repo}"

    @property
    def container_prefix(self):
        safe = self.name.lower().replace(" ", "-").replace("/", "-")
        return f"gh-runner-{safe}"

    @property
    def api_runners_path(self):
        if self.repo:
            return f"/repos/{self.owner}/{self.repo}/actions/runners"
        return f"/orgs/{self.owner}/actions/runners"

    @property
    def registration_url(self):
        if self.repo:
            return f"https://github.com/{self.owner}/{self.repo}"
        return f"https://github.com/{self.owner}"


# ── Config loader ─────────────────────────────────────────────────────────────
def resolve_env(value: str) -> str:
    """Expand ${VAR} style references in config values."""
    if not value:
        return value
    if value.startswith("${") and value.endswith("}"):
        var_name = value[2:-1]
        return os.environ.get(var_name, "")
    return value

def load_config(path="config.yml") -> list[PoolConfig]:
    if os.path.exists(path):
        with open(path) as f:
            raw = yaml.safe_load(f)

        defaults = raw.get("defaults", {})
        # FIX: resolve env vars in PAT
        global_pat    = resolve_env(defaults.get("pat", "")) or os.environ.get("GITHUB_PAT", "")
        global_image  = defaults.get("runner_image",  "gh-runner:latest")
        global_labels = defaults.get("runner_labels", "self-hosted,linux,x64,docker")
        global_max    = defaults.get("max_runners", 3)
        global_min    = defaults.get("min_idle",    1)

        pools = []
        for p in raw.get("pools", []):
            pat = resolve_env(p.get("pat", "")) or global_pat
            pools.append(PoolConfig(
                name          = p["name"],
                pat           = pat,
                owner         = p["owner"],
                repo          = p.get("repo", ""),
                max_runners   = p.get("max_runners",   global_max),
                min_idle      = p.get("min_idle",      global_min),
                runner_labels = p.get("runner_labels", global_labels),
                runner_image  = p.get("runner_image",  global_image),
                memory_limit  = p.get("memory_limit",  defaults.get("memory_limit", "2g")),
                cpu_limit     = float(p.get("cpu_limit", defaults.get("cpu_limit", 1.5))),
            ))
        return pools

    log.warning("No config.yml found — using env vars")
    return [PoolConfig(
        name          = "default",
        pat           = os.environ.get("GITHUB_PAT",    ""),
        owner         = os.environ.get("GITHUB_OWNER",  ""),
        repo          = os.environ.get("GITHUB_REPO",   ""),
        max_runners   = int(os.environ.get("MAX_RUNNERS",  "3")),
        min_idle      = int(os.environ.get("MIN_IDLE",     "1")),
        runner_labels = os.environ.get("RUNNER_LABELS", "self-hosted,linux,x64,docker"),
        runner_image  = os.environ.get("RUNNER_IMAGE",  "gh-runner:latest"),
    )]


def validate_pools(pools: list[PoolConfig]):
    ok = True
    for p in pools:
        if not p.pat or p.pat.startswith("${"):
            log.error(f"Pool '{p.name}': pat is missing or unresolved (got: '{p.pat}')")
            ok = False
        if not p.owner:
            log.error(f"Pool '{p.name}': owner is required")
            ok = False
    if not ok:
        sys.exit(1)


# ── GitHub API ────────────────────────────────────────────────────────────────
def github_get(pat: str, path: str) -> Optional[dict | list]:
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {pat}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        log.error(f"GitHub API {e.code}: {path} — {body[:200]}")
        return None
    except Exception as e:
        log.error(f"GitHub request failed: {e}")
        return None

def github_delete(pat: str, path: str) -> bool:
    """DELETE request to GitHub API. Returns True on success."""
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("Authorization", f"Bearer {pat}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 204
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        log.error(f"GitHub DELETE {e.code}: {path} — {body[:200]}")
        return None
    except Exception as e:
        log.error(f"GitHub DELETE failed: {e}")
        return None

def get_queued_jobs_for_repo(pat: str, owner: str, repo: str) -> int:
    """
    Count jobs actually waiting for a runner in a repo.
    Checks both queued runs AND in_progress runs (jobs inside can still be queued).
    """
    total = 0

    # Fetch runs in queued or in_progress state
    for status in ("queued", "in_progress"):
        runs_data = github_get(pat,
            f"/repos/{owner}/{repo}/actions/runs?status={status}&per_page=50")
        if not runs_data:
            continue
        runs = runs_data.get("workflow_runs", [])

        for run in runs:
            run_id = run["id"]
            jobs_data = github_get(pat,
                f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=50")
            if not jobs_data:
                continue
            for job in jobs_data.get("jobs", []):
                if job.get("status") == "queued":
                    total += 1

    return total

def get_queued_count(pool: PoolConfig) -> int:
    """Count jobs actually queued waiting for a runner."""
    if pool.repo:
        return get_queued_jobs_for_repo(pool.pat, pool.owner, pool.repo)

    # Org-level: sum across all repos
    repos = github_get(pool.pat, f"/orgs/{pool.owner}/repos?per_page=100&type=all")
    if not repos:
        repos = github_get(pool.pat, f"/users/{pool.owner}/repos?per_page=100&type=owner")
    if not repos or not isinstance(repos, list):
        return 0

    total = 0
    for repo in repos:
        total += get_queued_jobs_for_repo(pool.pat, pool.owner, repo["name"])
    return total

def get_runner_stats(pool: PoolConfig) -> tuple[int, int]:
    """
    Returns (idle, busy) counts of online registered runners.
    - idle: online and not running a job
    - busy: online and currently running a job
    """
    data = github_get(pool.pat, pool.api_runners_path)
    if not data:
        return 0, 0
    runners = data.get("runners", [])
    online  = [r for r in runners if r.get("status") == "online"]
    busy    = sum(1 for r in online if r.get("busy") is True)
    idle    = sum(1 for r in online if r.get("busy") is False)
    return idle, busy

def _get_online_runner_names(pool: PoolConfig) -> set[str]:
    """Get names of all online runners for this pool."""
    data = github_get(pool.pat, pool.api_runners_path)
    if not data:
        return set()
    return {
        r["name"] for r in data.get("runners", [])
        if r.get("status") == "online"
    }


# ── Docker ────────────────────────────────────────────────────────────────────
def docker_run(args: list, capture=True):
    cmd = ["docker"] + args
    if capture:
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.stdout.strip(), r.returncode
    return subprocess.run(cmd).returncode

def running_containers(prefix: str) -> list[str]:
    out, _ = docker_run([
        "ps",
        "--filter", f"name=^{prefix}-",   # FIX: anchor to exact prefix
        "--format", "{{.Names}}"
    ])
    return [n for n in out.split("\n") if n] if out else []

def cleanup_exited(prefix: str):
    out, _ = docker_run([
        "ps", "-a",
        "--filter", f"name=^{prefix}-",
        "--filter", "status=exited",
        "--format", "{{.Names}}"
    ])
    if not out:
        return
    names = [n for n in out.split("\n") if n]
    if not names:
        return

    def rm(name):
        docker_run(["rm", name])
        log.info(f"  🗑  rm {name}")

    threads = [threading.Thread(target=rm, args=(n,), daemon=True) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

def cleanup_stuck_containers(prefix: str, online_names: set[str], timeout_minutes: int = 3):
    """
    Kill containers that have been running longer than timeout_minutes
    but never came online on GitHub. These are stuck/hung registrations.
    """
    out, _ = docker_run([
        "ps",
        "--filter", f"name=^{prefix}-",
        "--format", "{{.Names}}\t{{.RunningFor}}"
    ])
    if not out:
        return

    to_kill = []
    for line in out.split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        name, running_for = parts[0], parts[1]

        if name in online_names:
            continue

        minutes = parse_running_minutes(running_for)
        if minutes is None or minutes < timeout_minutes:
            continue

        log.warning(f"  ⚠️  Stuck: {name} (running {running_for}, never came online)")
        to_kill.append(name)

    if not to_kill:
        return

    log.info(f"  Killing {len(to_kill)} stuck container(s) in parallel")
    def kill(name):
        docker_run(["rm", "-f", name])
        log.info(f"  💀  Killed {name}")

    threads = [threading.Thread(target=kill, args=(n,), daemon=True) for n in to_kill]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

def parse_running_minutes(running_for: str) -> Optional[float]:
    """Parse Docker's human-readable running time into minutes."""
    try:
        parts = running_for.lower().split()
        if len(parts) < 2:
            return None
        value = float(parts[0])
        unit  = parts[1]
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

def deregister_offline_runners(pool: PoolConfig):
    """Remove offline runner registrations from GitHub that have no running container."""
    data = github_get(pool.pat, pool.api_runners_path)
    if not data:
        return

    runners = data.get("runners", [])
    offline = [r for r in runners if r.get("status") == "offline"]

    if not offline:
        return

    # Get names of our currently running containers
    running = set(running_containers(pool.container_prefix))

    for runner in offline:
        name = runner.get("name", "")
        rid  = runner.get("id")

        # Only remove runners that belong to this pool (match prefix)
        # and have no running container (container already gone)
        if not name.startswith(pool.container_prefix):
            continue
        if name in running:
            continue  # container still running, might just be slow to register

        log.info(f"  🧹  Deregistering offline runner: {name} (id={rid})")
        result = github_delete(pool.pat,
            f"{pool.api_runners_path}/{rid}")
        if result is not None:
            log.info(f"  ✓  Deregistered {name}")
        else:
            log.warning(f"  ✗  Failed to deregister {name}")

def spawn_runner(pool: PoolConfig) -> bool:
    uid  = uuid.uuid4().hex[:8]
    name = f"{pool.container_prefix}-{uid}"
    log.info(f"  ▶  Spawning {name}")

    args = [
        "run", "-d",
        "--name", name,
        "--restart", "no",
        "--network", "host",
        # ── Resource limits ───────────────────────────────────────────────────
        "--memory",        pool.memory_limit,   # hard RAM cap per runner
        "--memory-swap",   pool.memory_limit,   # disable swap
        "--pids-limit",    "512",               # prevent fork bombs
        # ─────────────────────────────────────────────────────────────────────
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", "/opt/hostedtoolcache:/opt/hostedtoolcache",
        "-e", f"GITHUB_PAT={pool.pat}",
        "-e", f"GITHUB_OWNER={pool.owner}",
        "-e", f"GITHUB_REPO={pool.repo}",
        "-e", f"RUNNER_NAME={name}",
        "-e", f"RUNNER_LABELS={pool.runner_labels}",
        pool.runner_image
    ]

    # Only add CPU limit if explicitly set (0 = no limit)
    if pool.cpu_limit > 0:
        args = args[:5] + ["--cpus", str(pool.cpu_limit)] + args[5:]

    code = docker_run(args, capture=False)

    cpu_info = f" cpus={pool.cpu_limit}" if pool.cpu_limit > 0 else " cpus=unlimited"

    if code == 0:
        log.info(f"  ✓  Started {name}  (mem={pool.memory_limit}{cpu_info})")
        return True
    log.error(f"  ✗  Failed to start {name}")
    return False


# ── Pool tick ─────────────────────────────────────────────────────────────────
def tick(pool: PoolConfig):
    cleanup_exited(pool.container_prefix)
    deregister_offline_runners(pool)

    running        = running_containers(pool.container_prefix)
    n_running      = len(running)
    n_queued       = get_queued_count(pool)
    n_idle, n_busy = get_runner_stats(pool)
    n_online       = n_idle + n_busy

    # Kill containers that are running but never came online after timeout
    online_names = set(running) & _get_online_runner_names(pool)
    cleanup_stuck_containers(pool.container_prefix, online_names, timeout_minutes=3)

    # Recount after stuck cleanup
    running   = running_containers(pool.container_prefix)
    n_running = len(running)
    n_queued       = get_queued_count(pool)
    n_idle, n_busy = get_runner_stats(pool)
    n_online       = n_idle + n_busy

    log.info(
        f"[{pool.name}] {pool.display} | "
        f"containers={n_running}  online={n_online} (idle={n_idle} busy={n_busy})  queued={n_queued}"
    )

    # desired = busy runners (occupied) + queued jobs (need a runner)
    # this tells us exactly how many runners we need in total
    # idle runners already cover some queued jobs so factor them in
    desired  = min(pool.max_runners, max(pool.min_idle, n_busy + n_queued))
    to_spawn = max(0, desired - n_running)

    if to_spawn > 0:
        log.info(
            f"[{pool.name}] online={n_online} queued={n_queued} "
            f"containers={n_running}/{pool.max_runners} → spawning {to_spawn} in parallel"
        )
        # Spawn all at once in parallel — no more sequential waiting
        threads = [
            threading.Thread(target=spawn_runner, args=(pool,), daemon=True)
            for _ in range(to_spawn)
        ]
        for t in threads:
            t.start()
            time.sleep(0.3)   # tiny stagger to avoid name collisions
        for t in threads:
            t.join(timeout=60)
    else:
        log.info(
            f"[{pool.name}] ✓ OK  online={n_online}  queued={n_queued}  "
            f"containers={n_running}/{pool.max_runners}"
        )


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    poll = int(os.environ.get("POLL_INTERVAL", "15"))
    pools = load_config()
    validate_pools(pools)

    log.info("=" * 60)
    log.info("GitHub Runner Orchestrator  —  multi-pool")
    for p in pools:
        scope = "org-level" if p.is_org_level else "repo-level"
        log.info(f"  [{p.name}]  {p.display}  max={p.max_runners}  min_idle={p.min_idle}  ({scope})")
        # log PAT prefix only for debugging, never full token
        log.info(f"    PAT: {p.pat[:20]}..." if len(p.pat) > 20 else f"    PAT: {'SET' if p.pat else 'MISSING'}")
    log.info(f"Poll interval: {poll}s")
    log.info("=" * 60)

    while True:
        for pool in pools:
            try:
                tick(pool)
            except Exception as e:
                log.error(f"[{pool.name}] Unhandled error: {e}", exc_info=True)
        time.sleep(poll)


if __name__ == "__main__":
    main()