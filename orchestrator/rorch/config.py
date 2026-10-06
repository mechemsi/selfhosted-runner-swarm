# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Pool configuration loading and validation."""

import logging
import os
import re
import sys
from dataclasses import dataclass, replace

import yaml

log = logging.getLogger(__name__)

# work_tmpfs_size "auto" when memory_limit is not a plain size.
AUTO_WORK_TMPFS_FALLBACK = "4g"

# GitHub's default job timeout-minutes.
DEFAULT_CI_CONTAINER_MAX_AGE = 360


# The runner image published by this repo's release workflow. `{version}` is the
# orchestrator's own version (rorch.version.expand_image), so deploying or rolling
# back the orchestrator moves the runners with it. A bare local name such as
# gh-runner:latest (scripts/build-runner.sh) is built on the host instead.
DEFAULT_RUNNER_IMAGE = "ghcr.io/mechemsi/rorch-runner:{version}"


@dataclass
class PoolConfig:
    """Configuration for a single runner pool."""

    name: str
    pat: str
    owner: str
    repo: str = ""
    scope: str = "organization"
    repo_discovery_ttl: int = 600
    github_poll_interval: int = 60
    # Minutes a runner container may run without GitHub listing it online or
    # busy before it counts as stuck and is killed. A container running a job
    # (Runner.Worker alive inside it) is never killed, whatever GitHub says.
    stuck_timeout: int = 8
    repo_check_workers: int = 6
    runner_operation_workers: int = 4
    max_runners: int = 3
    min_idle: int = 1
    runner_labels: str = "self-hosted,linux,x64,docker"
    runner_image: str = DEFAULT_RUNNER_IMAGE
    memory_limit: str = "2g"
    cpu_limit: float = 0.0
    # Size of the tmpfs mounted over the runner work dir (checkouts, node_modules,
    # build output). Keeps that churn off the host disk. tmpfs pages count toward
    # memory_limit, so leave room for the job's processes. "auto" = half of
    # memory_limit. "" or "0" = no tmpfs, the work dir lives in the container's
    # writable layer as before.
    work_tmpfs_size: str = "auto"
    # "host" shares the host network namespace: a job's `services:` containers
    # bind host ports and collide with anything already listening there (e.g. a
    # host Postgres on 5432). "bridge" isolates the runner so service containers
    # get their own namespace, at the cost of jobs no longer reaching host
    # services over localhost. Default stays "host" — the historical behaviour.
    network_mode: str = "host"
    # Discovery pools (personal/org scope) skip public repositories by default.
    # A fork PR on a public repo carries its own workflow file and picks its own
    # `runs-on` labels, so registering a self-hosted runner there offers any
    # contributor code execution on the host that owns the Docker socket.
    # Opt in per pool only if you accept that.
    include_public_repos: bool = False
    # Comma-separated repository names or glob patterns a discovery pool must
    # never provision for, e.g. "legacy-*,scratch". Applied after the public
    # filter, so both can be used together.
    exclude_repos: str = ""
    # Host path of a script the runner runs before each job's first step
    # (ACTIONS_RUNNER_HOOK_JOB_STARTED); a non-zero exit fails the job. Mounted
    # read-only, so keep it outside any directory a job can write. config.yml
    # only: it is not a dashboard tunable.
    job_started_hook: str = ""

    @property
    def label_set(self) -> frozenset[str]:
        """Labels the runner registers with: the configured ones plus the
        defaults config.sh always adds. GitHub compares labels case-insensitively."""
        configured = {label.strip().lower() for label in self.runner_labels.split(",")}
        return frozenset((configured | {"self-hosted", "linux", "x64"}) - {""})

    def serves_labels(self, labels: list[str]) -> bool:
        """Whether a job asking for `labels` can run on this pool's runners."""
        return {label.lower() for label in labels} <= self.label_set

    @property
    def excluded_repo_patterns(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.exclude_repos.split(",") if p.strip())

    def excludes_repo(self, name: str) -> bool:
        """Whether `name` matches any exclude pattern (glob or exact)."""
        from fnmatch import fnmatch

        return any(fnmatch(name, pattern) for pattern in self.excluded_repo_patterns)

    @property
    def work_tmpfs_enabled(self) -> bool:
        # Docker reads size=0 as "unbounded", so every zero spelling means off.
        size = self.work_tmpfs_size.strip()
        return bool(size) and parse_size(size) != 0

    @property
    def effective_work_tmpfs_size(self) -> str:
        """The `size=` for the work dir tmpfs, or "" when it is disabled."""
        size = self.work_tmpfs_size.strip()
        if not self.work_tmpfs_enabled:
            return ""
        if size.lower() != "auto":
            return size
        memory = parse_size(self.memory_limit)
        if not memory:
            return AUTO_WORK_TMPFS_FALLBACK
        return f"{memory // 2 // 1024**2}m"

    @property
    def is_org_level(self) -> bool:
        return not self.repo and not self.is_personal_level

    @property
    def is_personal_level(self) -> bool:
        return not self.repo and self.scope == "personal"

    @property
    def display(self) -> str:
        if self.is_personal_level:
            return f"{self.owner} [personal: all repos]"
        return f"{self.owner} [org]" if self.is_org_level else f"{self.owner}/{self.repo}"

    @property
    def container_prefix(self) -> str:
        safe = self.name.lower().replace(" ", "-").replace("/", "-")
        return f"gh-runner-{safe}"

    @property
    def api_runners_path(self) -> str:
        if self.repo:
            return f"/repos/{self.owner}/{self.repo}/actions/runners"
        return f"/orgs/{self.owner}/actions/runners"

    @property
    def registration_url(self) -> str:
        if self.repo:
            return f"https://github.com/{self.owner}/{self.repo}"
        return f"https://github.com/{self.owner}"

    def for_repository(self, repo: str) -> "PoolConfig":
        """Create a repository-scoped runtime pool derived from this pool."""
        return replace(
            self,
            name=f"{self.name}-{repo}",
            repo=repo,
            scope="repository",
            min_idle=0,
        )


_SIZE_RE = re.compile(r"^(\d+)([kmg]?)b?$", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def parse_size(value: str) -> int | None:
    """Bytes in a Docker-style size such as `4g` or `512m`; None if malformed."""
    match = _SIZE_RE.match(value.strip())
    if not match:
        return None
    return int(match.group(1)) * _SIZE_UNITS[match.group(2).lower()]


def _as_csv(value: object) -> str:
    """Accept either a YAML list or a comma-separated string."""
    if isinstance(value, list):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


def _as_size(value: object) -> str:
    """YAML reads `0` as an int and `~` as None; both mean "no tmpfs"."""
    return "" if value is None else str(value).strip()


def resolve_env(value: str) -> str:
    """Expand ${VAR} style references in config values."""
    if not value:
        return value
    if value.startswith("${") and value.endswith("}"):
        var_name = value[2:-1]
        return os.environ.get(var_name, "")
    return value


def load_config(path: str = "config.yml") -> list[PoolConfig]:
    """Load pool configurations from YAML file or environment variables."""
    if os.path.exists(path):
        return _load_from_yaml(path)

    log.warning("No config.yml found — using env vars")
    return [_load_from_env()]


def load_max_total_runners(path: str = "config.yml") -> int:
    """Load the global runner ceiling across all pools (0 = unlimited)."""
    if os.path.exists(path):
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        value = int(raw.get("max_total_runners", 0))
    else:
        value = int(os.environ.get("MAX_TOTAL_RUNNERS", "0"))
    if value < 0:
        log.error("max_total_runners cannot be negative (got %d)", value)
        sys.exit(1)
    return value


def load_max_runner_lifetime(path: str = "config.yml") -> int:
    """Load the hard wall-clock ceiling (minutes) for a runner container (0 = disabled).

    Backstop that reaps leaked runners the ephemeral-exit and stuck reapers miss:
    over-provisioned idle runners that never got a job, and jobs that hung after
    coming online. Set above your longest expected job or it will abort real work.
    """
    if os.path.exists(path):
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        value = int(raw.get("max_runner_lifetime", 0))
    else:
        value = int(os.environ.get("MAX_RUNNER_LIFETIME", "0"))
    if value < 0:
        log.error("max_runner_lifetime cannot be negative (got %d)", value)
        sys.exit(1)
    return value


def load_ci_container_max_age(path: str = "config.yml") -> int:
    """Minutes after which a leftover CI job container is removed (0 = disabled).

    Containers a job starts through the host Docker socket (`services:`, or a
    plain `docker run` in a step) outlive the job when it is cancelled, because
    the runner never reaches its cleanup step. The default matches GitHub's
    default job timeout, so no job that still owns the container can be running.
    """
    if os.path.exists(path):
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        value = int(raw.get("ci_container_max_age", DEFAULT_CI_CONTAINER_MAX_AGE))
    else:
        value = int(os.environ.get("CI_CONTAINER_MAX_AGE", str(DEFAULT_CI_CONTAINER_MAX_AGE)))
    if value < 0:
        log.error("ci_container_max_age cannot be negative (got %d)", value)
        sys.exit(1)
    return value


def load_ci_container_patterns(path: str = "config.yml") -> tuple[str, ...]:
    """Extra container name globs (or `label=KEY`) that mark a container as CI-owned.

    Only needed for containers a workflow starts itself with `docker run`; the
    runner's own `services:` containers are recognised without configuration.
    """
    if os.path.exists(path):
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        value = _as_csv(raw.get("ci_container_patterns", ""))
    else:
        value = os.environ.get("CI_CONTAINER_PATTERNS", "")
    return tuple(p.strip() for p in value.split(",") if p.strip())


def _load_from_yaml(path: str) -> list[PoolConfig]:
    with open(path) as f:
        raw = yaml.safe_load(f)

    defaults = raw.get("defaults", {})
    global_pat = resolve_env(defaults.get("pat", "")) or os.environ.get("GITHUB_PAT", "")
    global_image = defaults.get("runner_image", DEFAULT_RUNNER_IMAGE)
    global_labels = defaults.get("runner_labels", "self-hosted,linux,x64,docker")
    global_max = defaults.get("max_runners", 3)
    global_min = defaults.get("min_idle", 1)

    pools: list[PoolConfig] = []
    for p in raw.get("pools", []):
        pat = resolve_env(p.get("pat", "")) or global_pat
        scope = str(p.get("scope", "organization")).lower()
        pools.append(
            PoolConfig(
                name=p["name"],
                pat=pat,
                owner=p["owner"],
                repo=p.get("repo", ""),
                scope=scope,
                repo_discovery_ttl=int(
                    p.get("repo_discovery_ttl", defaults.get("repo_discovery_ttl", 600))
                ),
                github_poll_interval=int(
                    p.get("github_poll_interval", defaults.get("github_poll_interval", 60))
                ),
                stuck_timeout=int(p.get("stuck_timeout", defaults.get("stuck_timeout", 8))),
                repo_check_workers=int(
                    p.get("repo_check_workers", defaults.get("repo_check_workers", 6))
                ),
                runner_operation_workers=int(
                    p.get(
                        "runner_operation_workers",
                        defaults.get("runner_operation_workers", 4),
                    )
                ),
                max_runners=p.get("max_runners", global_max),
                min_idle=p.get("min_idle", 0 if scope == "personal" else global_min),
                runner_labels=p.get("runner_labels", global_labels),
                runner_image=p.get("runner_image", global_image),
                memory_limit=p.get("memory_limit", defaults.get("memory_limit", "2g")),
                cpu_limit=float(p.get("cpu_limit", defaults.get("cpu_limit", 1.5))),
                work_tmpfs_size=_as_size(
                    p.get("work_tmpfs_size", defaults.get("work_tmpfs_size", "auto"))
                ),
                network_mode=str(
                    p.get("network_mode", defaults.get("network_mode", "host"))
                ).lower(),
                include_public_repos=bool(
                    p.get("include_public_repos", defaults.get("include_public_repos", False))
                ),
                exclude_repos=_as_csv(p.get("exclude_repos", defaults.get("exclude_repos", ""))),
                job_started_hook=str(p.get("job_started_hook", "")).strip(),
            )
        )
    return pools


def _load_from_env() -> PoolConfig:
    scope = os.environ.get("GITHUB_SCOPE", "organization").lower()
    return PoolConfig(
        name="default",
        pat=os.environ.get("GITHUB_PAT", ""),
        owner=os.environ.get("GITHUB_OWNER", ""),
        repo=os.environ.get("GITHUB_REPO", ""),
        scope=scope,
        repo_discovery_ttl=int(os.environ.get("REPO_DISCOVERY_TTL", "600")),
        github_poll_interval=int(os.environ.get("GITHUB_POLL_INTERVAL", "60")),
        stuck_timeout=int(os.environ.get("STUCK_TIMEOUT", "8")),
        repo_check_workers=int(os.environ.get("REPO_CHECK_WORKERS", "6")),
        runner_operation_workers=int(os.environ.get("RUNNER_OPERATION_WORKERS", "4")),
        max_runners=int(os.environ.get("MAX_RUNNERS", "3")),
        min_idle=int(os.environ.get("MIN_IDLE", "0" if scope == "personal" else "1")),
        runner_labels=os.environ.get("RUNNER_LABELS", "self-hosted,linux,x64,docker"),
        runner_image=os.environ.get("RUNNER_IMAGE", DEFAULT_RUNNER_IMAGE),
        network_mode=os.environ.get("RUNNER_NETWORK_MODE", "host").lower(),
        work_tmpfs_size=os.environ.get("RUNNER_WORK_TMPFS_SIZE", "auto").strip(),
        include_public_repos=os.environ.get("INCLUDE_PUBLIC_REPOS", "").lower()
        in {"1", "true", "yes"},
        exclude_repos=os.environ.get("EXCLUDE_REPOS", ""),
    )


def validation_errors(pools: list[PoolConfig]) -> list[str]:
    """Return one message per invalid pool setting; empty means the config is usable.

    Split out of `validate_pools` so the HTTP API can reject a bad config change
    with a 400 instead of killing the orchestrator process.
    """
    errors: list[str] = []
    for p in pools:
        if not p.pat or p.pat.startswith("${"):
            errors.append(f"Pool '{p.name}': pat is missing or unresolved (got: '{p.pat}')")
        if not p.owner:
            errors.append(f"Pool '{p.name}': owner is required")
        if p.scope not in {"organization", "personal", "repository"}:
            errors.append(f"Pool '{p.name}': unsupported scope '{p.scope}'")
        if p.scope == "personal" and p.repo:
            errors.append(f"Pool '{p.name}': personal scope cannot also set repo")
        if p.scope == "repository" and not p.repo:
            errors.append(f"Pool '{p.name}': repository scope requires repo")
        if p.repo_discovery_ttl < 0:
            errors.append(f"Pool '{p.name}': repo_discovery_ttl cannot be negative")
        if p.github_poll_interval != 0 and not 15 <= p.github_poll_interval <= 3600:
            errors.append(f"Pool '{p.name}': github_poll_interval must be 0 or between 15 and 3600")
        if not 1 <= p.stuck_timeout <= 1440:
            errors.append(f"Pool '{p.name}': stuck_timeout must be between 1 and 1440 minutes")
        if not 1 <= p.repo_check_workers <= 32:
            errors.append(f"Pool '{p.name}': repo_check_workers must be between 1 and 32")
        if not 1 <= p.runner_operation_workers <= 16:
            errors.append(f"Pool '{p.name}': runner_operation_workers must be between 1 and 16")
        if p.max_runners < 0:
            errors.append(f"Pool '{p.name}': max_runners cannot be negative")
        if p.min_idle < 0:
            errors.append(f"Pool '{p.name}': min_idle cannot be negative")
        if p.network_mode not in {"host", "bridge"}:
            errors.append(f"Pool '{p.name}': network_mode must be 'host' or 'bridge'")
        if p.work_tmpfs_enabled and parse_size(p.effective_work_tmpfs_size) is None:
            errors.append(
                f"Pool '{p.name}': work_tmpfs_size must be 'auto', a size like '4g' or "
                f"'512m', or '0' to disable (got: '{p.work_tmpfs_size}')"
            )
        if p.job_started_hook and not p.job_started_hook.startswith("/"):
            errors.append(f"Pool '{p.name}': job_started_hook must be an absolute host path")
    return errors


def validation_warnings(pools: list[PoolConfig]) -> list[str]:
    """Configurations that work but are almost certainly a mistake.

    Deliberately separate from `validation_errors`: these must never stop a
    running deployment from starting, only make the problem visible.
    """
    warnings: list[str] = []
    seen: set[str] = set()
    for p in pools:
        if p.name in seen:
            warnings.append(
                f"Pool '{p.name}' is defined more than once. Both copies share the container "
                f"prefix '{p.container_prefix}', so they scale the same runners, double the "
                f"GitHub API calls for {p.display}, and share one row of dashboard state."
            )
        seen.add(p.name)
        for other in pools:
            if other.name != p.name and other.container_prefix.startswith(f"{p.container_prefix}-"):
                warnings.append(
                    f"Pool '{p.name}' matches containers by the prefix '{p.container_prefix}-', "
                    f"which also matches pool '{other.name}'. It would count and reap that "
                    f"pool's runners as its own; rename one of them."
                )
        tmpfs = parse_size(p.effective_work_tmpfs_size) if p.work_tmpfs_enabled else None
        memory = parse_size(p.memory_limit)
        if tmpfs and memory and tmpfs >= memory:
            warnings.append(
                f"Pool '{p.name}': work_tmpfs_size {p.effective_work_tmpfs_size} is not below "
                f"memory_limit {p.memory_limit}. tmpfs pages count toward the memory limit, "
                f"so a job that fills its workspace gets OOM-killed instead of a disk-full error."
            )
    return warnings


def validate_pools(pools: list[PoolConfig]) -> None:
    """Validate pool configurations. Exits on failure."""
    for message in validation_warnings(pools):
        log.warning("%s", message)
    errors = validation_errors(pools)
    for message in errors:
        log.error("%s", message)
    if errors:
        sys.exit(1)
