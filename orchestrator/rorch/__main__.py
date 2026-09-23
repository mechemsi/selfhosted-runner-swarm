# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Entry point for the orchestrator: python -m rorch"""

import logging
import os
import time
from dataclasses import dataclass

from rorch import server
from rorch.config import (
    PoolConfig,
    load_config,
    load_max_runner_lifetime,
    load_max_total_runners,
    validate_pools,
)
from rorch.docker_client import ORCHESTRATOR_CONTAINER, DockerClient
from rorch.github_client import GitHubClient
from rorch.resolver import ConfigResolver, EffectiveConfig
from rorch.scaler import GLOBAL_CONTAINER_PREFIX, PoolScaler
from rorch.store import Store, open_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/app/data/rorch.db"


@dataclass(frozen=True)
class Runtime:
    """What main() wires together once at startup and every tick reuses."""

    pools: list[PoolConfig]
    poll: int
    prune_every: int
    retention_days: int
    rate_limit_reserve: int
    max_total_runners: int
    max_runner_lifetime: int
    store: Store | None
    resolver: ConfigResolver
    github: GitHubClient
    docker: DockerClient
    scaler: PoolScaler


def main() -> None:
    runtime = _build_runtime()
    _log_banner(runtime)
    _start_dashboard(runtime)

    # Build ahead of demand: doing it on the first spawn stalls a queued job.
    for image in {p.runner_image for p in runtime.pools}:
        runtime.docker.ensure_image(image)

    tick_count = 0
    while True:
        _tick(runtime)
        tick_count += 1
        if tick_count % runtime.prune_every == 0:
            _housekeeping(runtime)
        time.sleep(runtime.poll)


def _build_runtime() -> Runtime:
    poll = int(os.environ.get("POLL_INTERVAL", "15"))
    pools = load_config()
    validate_pools(pools)
    max_total_runners = load_max_total_runners()
    max_runner_lifetime = load_max_runner_lifetime()
    retention_days = int(os.environ.get("HISTORY_RETENTION_DAYS", "14"))

    store = None if os.environ.get("RORCH_DB", "").lower() == "off" else open_store(_db_url())
    resolver = ConfigResolver(pools, max_total_runners, max_runner_lifetime, store)

    rate_limit_reserve = max(0, int(os.environ.get("GITHUB_RATE_LIMIT_RESERVE", "100")))
    github = GitHubClient(rate_limit_reserve=rate_limit_reserve)
    docker = DockerClient(store=store)
    return Runtime(
        pools=pools,
        poll=poll,
        prune_every=max(1, 900 // poll),  # ~every 15 minutes
        retention_days=retention_days,
        rate_limit_reserve=rate_limit_reserve,
        max_total_runners=max_total_runners,
        max_runner_lifetime=max_runner_lifetime,
        store=store,
        resolver=resolver,
        github=github,
        docker=docker,
        scaler=PoolScaler(github, docker, max_total_runners=max_total_runners, store=store),
    )


def _log_banner(runtime: Runtime) -> None:
    log.info("=" * 60)
    log.info("GitHub Runner Orchestrator  —  multi-pool")
    for pool in runtime.pools:
        _log_pool(pool)
    log.info("Poll interval: %ds  (image prune every %d ticks)", runtime.poll, runtime.prune_every)
    log.info("GitHub API reserve: %d requests", runtime.rate_limit_reserve)
    log.info(
        "Global runner cap: %s",
        runtime.max_total_runners if runtime.max_total_runners else "unlimited",
    )
    log.info(
        "Max runner lifetime: %s",
        f"{runtime.max_runner_lifetime}m" if runtime.max_runner_lifetime else "disabled",
    )
    log.info("State store: %s", "enabled" if runtime.store else "disabled (config.yml only)")
    _warn_public_repo_pools(runtime.pools, runtime.github)
    log.info("=" * 60)


def _log_pool(pool: PoolConfig) -> None:
    log.info(
        "  [%s]  %s  max=%d  min_idle=%d  (%s)",
        pool.name,
        pool.display,
        pool.max_runners,
        pool.min_idle,
        _pool_scope(pool),
    )
    # Never any part of the token: 20 characters was half of a 40-character ghp_ PAT, in plain logs.
    log.info("    PAT: %s", "SET" if pool.pat else "MISSING")


def _pool_scope(pool: PoolConfig) -> str:
    if pool.is_personal_level:
        return "personal all-repositories"
    return "org-level" if pool.is_org_level else "repo-level"


def _start_dashboard(runtime: Runtime) -> None:
    if runtime.store is None:
        return
    server.start(
        server.Deps(
            store=runtime.store,
            resolver=runtime.resolver,
            docker=runtime.docker,
            rate_limit_status=runtime.github.rate_limit_status,
            token=server.resolve_token(runtime.store),
            readonly_token=server.resolve_readonly_token(),
        ),
        host=os.environ.get("RORCH_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("RORCH_API_PORT", "8080")),
    )


def _tick(runtime: Runtime) -> None:
    effective = runtime.resolver.resolve()
    runtime.scaler.max_total_runners = effective.max_total_runners

    if effective.paused:
        log.info("All provisioning paused — skipping tick")
    else:
        _tick_pools(runtime.scaler, effective)

    try:
        runtime.docker.cleanup_aged(
            GLOBAL_CONTAINER_PREFIX,
            effective.max_runner_lifetime,
            exclude=frozenset({ORCHESTRATOR_CONTAINER}) | effective.protected,
        )
    except Exception:
        log.error("Aged-runner cleanup failed", exc_info=True)


def _tick_pools(scaler: PoolScaler, effective: EffectiveConfig) -> None:
    for pool in effective.pools:
        # One pool's failure (a bad PAT, a GitHub outage for that owner) must not starve the rest.
        try:
            scaler.tick(pool, effective.state_for(pool.name))
        except Exception:
            log.error("[%s] Unhandled error", pool.name, exc_info=True)


def _housekeeping(runtime: Runtime) -> None:
    try:
        runtime.docker.prune_images()
        runtime.docker.prune_build_cache()
        runtime.docker.prune_volumes()
    except Exception:
        log.error("Docker prune failed", exc_info=True)
    if runtime.store is None:
        return
    try:
        runtime.store.prune(runtime.retention_days)
        runtime.store.prune_idempotency()
    except Exception:
        log.error("History prune failed", exc_info=True)


def _warn_public_repo_pools(pools: list, github: GitHubClient) -> None:
    """Flag repo-level pools aimed at public repositories.

    `include_public_repos` only filters pools that *discover* repositories. A
    pool naming a repo explicitly bypasses it, which is exactly how a public
    repo keeps its self-hosted runners after the discovery filter was added.

    A warning rather than a refusal: this must never stop a running deployment
    from starting.
    """
    public = [p for p in pools if p.repo and github.is_public_repo(p) is True]
    if not public:
        return
    for pool in public:
        log.warning(
            "Pool '%s' targets the PUBLIC repository %s. A fork PR there picks its own "
            "runs-on labels, so anyone who can open a PR can run code on this host — "
            "which owns the Docker socket and every PAT in this config. Remove the pool "
            "unless you accept that.",
            pool.name,
            pool.display,
        )


def _db_url() -> str:
    """Where state lives: a MariaDB URL, or a SQLite file path.

    RORCH_DB_URL wins (e.g. mysql://rorch:pw@mariadb:3306/rorch). Otherwise the
    historical SQLite path is used, so an existing install keeps working
    untouched after an upgrade.
    """
    url = os.environ.get("RORCH_DB_URL", "").strip()
    if url:
        return url
    configured = os.environ.get("RORCH_DB_PATH")
    if configured:
        return configured
    return DEFAULT_DB_PATH if os.path.isdir("/app") else "rorch.db"


if __name__ == "__main__":
    main()
