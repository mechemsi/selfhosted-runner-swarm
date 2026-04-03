"""Pool configuration loading and validation."""

import logging
import os
import sys
from dataclasses import dataclass

import yaml

log = logging.getLogger(__name__)


@dataclass
class PoolConfig:
    """Configuration for a single runner pool."""

    name: str
    pat: str
    owner: str
    repo: str = ""
    max_runners: int = 3
    min_idle: int = 1
    runner_labels: str = "self-hosted,linux,x64,docker"
    runner_image: str = "gh-runner:latest"
    memory_limit: str = "2g"
    cpu_limit: float = 0.0

    @property
    def is_org_level(self) -> bool:
        return not self.repo

    @property
    def display(self) -> str:
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


def _load_from_yaml(path: str) -> list[PoolConfig]:
    with open(path) as f:
        raw = yaml.safe_load(f)

    defaults = raw.get("defaults", {})
    global_pat = resolve_env(defaults.get("pat", "")) or os.environ.get("GITHUB_PAT", "")
    global_image = defaults.get("runner_image", "gh-runner:latest")
    global_labels = defaults.get("runner_labels", "self-hosted,linux,x64,docker")
    global_max = defaults.get("max_runners", 3)
    global_min = defaults.get("min_idle", 1)

    pools: list[PoolConfig] = []
    for p in raw.get("pools", []):
        pat = resolve_env(p.get("pat", "")) or global_pat
        pools.append(
            PoolConfig(
                name=p["name"],
                pat=pat,
                owner=p["owner"],
                repo=p.get("repo", ""),
                max_runners=p.get("max_runners", global_max),
                min_idle=p.get("min_idle", global_min),
                runner_labels=p.get("runner_labels", global_labels),
                runner_image=p.get("runner_image", global_image),
                memory_limit=p.get("memory_limit", defaults.get("memory_limit", "2g")),
                cpu_limit=float(p.get("cpu_limit", defaults.get("cpu_limit", 1.5))),
            )
        )
    return pools


def _load_from_env() -> PoolConfig:
    return PoolConfig(
        name="default",
        pat=os.environ.get("GITHUB_PAT", ""),
        owner=os.environ.get("GITHUB_OWNER", ""),
        repo=os.environ.get("GITHUB_REPO", ""),
        max_runners=int(os.environ.get("MAX_RUNNERS", "3")),
        min_idle=int(os.environ.get("MIN_IDLE", "1")),
        runner_labels=os.environ.get("RUNNER_LABELS", "self-hosted,linux,x64,docker"),
        runner_image=os.environ.get("RUNNER_IMAGE", "gh-runner:latest"),
    )


def validate_pools(pools: list[PoolConfig]) -> None:
    """Validate pool configurations. Exits on failure."""
    ok = True
    for p in pools:
        if not p.pat or p.pat.startswith("${"):
            log.error("Pool '%s': pat is missing or unresolved (got: '%s')", p.name, p.pat)
            ok = False
        if not p.owner:
            log.error("Pool '%s': owner is required", p.name)
            ok = False
    if not ok:
        sys.exit(1)
