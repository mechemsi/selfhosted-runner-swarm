# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Pool configuration loading and validation."""

import logging
import os
import sys
from dataclasses import dataclass, replace

import yaml

log = logging.getLogger(__name__)


@dataclass
class PoolConfig:
    """Configuration for a single runner pool."""

    name: str
    pat: str
    owner: str
    repo: str = ""
    scope: str = "organization"
    repo_discovery_ttl: int = 600
    repo_check_workers: int = 6
    runner_operation_workers: int = 4
    max_runners: int = 3
    min_idle: int = 1
    runner_labels: str = "self-hosted,linux,x64,docker"
    runner_image: str = "gh-runner:latest"
    memory_limit: str = "2g"
    cpu_limit: float = 0.0

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
        repo_check_workers=int(os.environ.get("REPO_CHECK_WORKERS", "6")),
        runner_operation_workers=int(os.environ.get("RUNNER_OPERATION_WORKERS", "4")),
        max_runners=int(os.environ.get("MAX_RUNNERS", "3")),
        min_idle=int(os.environ.get("MIN_IDLE", "0" if scope == "personal" else "1")),
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
        if p.scope not in {"organization", "personal", "repository"}:
            log.error("Pool '%s': unsupported scope '%s'", p.name, p.scope)
            ok = False
        if p.scope == "personal" and p.repo:
            log.error("Pool '%s': personal scope cannot also set repo", p.name)
            ok = False
        if p.scope == "repository" and not p.repo:
            log.error("Pool '%s': repository scope requires repo", p.name)
            ok = False
        if p.repo_discovery_ttl < 0:
            log.error("Pool '%s': repo_discovery_ttl cannot be negative", p.name)
            ok = False
        if not 1 <= p.repo_check_workers <= 32:
            log.error("Pool '%s': repo_check_workers must be between 1 and 32", p.name)
            ok = False
        if not 1 <= p.runner_operation_workers <= 16:
            log.error("Pool '%s': runner_operation_workers must be between 1 and 16", p.name)
            ok = False
    if not ok:
        sys.exit(1)
