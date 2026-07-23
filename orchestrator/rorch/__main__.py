# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Entry point for the orchestrator: python -m rorch"""

import logging
import os
import time

from rorch.config import (
    load_config,
    load_max_runner_lifetime,
    load_max_total_runners,
    validate_pools,
)
from rorch.docker_client import ORCHESTRATOR_CONTAINER, DockerClient
from rorch.github_client import GitHubClient
from rorch.scaler import GLOBAL_CONTAINER_PREFIX, PoolScaler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def main() -> None:
    poll = int(os.environ.get("POLL_INTERVAL", "15"))
    pools = load_config()
    validate_pools(pools)
    max_total_runners = load_max_total_runners()
    max_runner_lifetime = load_max_runner_lifetime()

    rate_limit_reserve = max(0, int(os.environ.get("GITHUB_RATE_LIMIT_RESERVE", "100")))
    github = GitHubClient(rate_limit_reserve=rate_limit_reserve)
    docker = DockerClient()
    scaler = PoolScaler(github, docker, max_total_runners=max_total_runners)

    log.info("=" * 60)
    log.info("GitHub Runner Orchestrator  —  multi-pool")
    for p in pools:
        if p.is_personal_level:
            scope = "personal all-repositories"
        else:
            scope = "org-level" if p.is_org_level else "repo-level"
        log.info(
            "  [%s]  %s  max=%d  min_idle=%d  (%s)",
            p.name,
            p.display,
            p.max_runners,
            p.min_idle,
            scope,
        )
        if len(p.pat) > 20:
            log.info("    PAT: %s...", p.pat[:20])
        else:
            log.info("    PAT: %s", "SET" if p.pat else "MISSING")
    prune_every = max(1, 900 // poll)  # ~every 15 minutes
    log.info("Poll interval: %ds  (image prune every %d ticks)", poll, prune_every)
    log.info("GitHub API reserve: %d requests", rate_limit_reserve)
    log.info(
        "Global runner cap: %s",
        max_total_runners if max_total_runners else "unlimited",
    )
    log.info(
        "Max runner lifetime: %s",
        f"{max_runner_lifetime}m" if max_runner_lifetime else "disabled",
    )
    log.info("=" * 60)

    # Build ahead of demand: doing it on the first spawn stalls a queued job.
    for image in {p.runner_image for p in pools}:
        docker.ensure_image(image)

    tick_count = 0
    while True:
        for pool in pools:
            try:
                scaler.tick(pool)
            except Exception:
                log.error("[%s] Unhandled error", pool.name, exc_info=True)

        try:
            docker.cleanup_aged(
                GLOBAL_CONTAINER_PREFIX,
                max_runner_lifetime,
                exclude=frozenset({ORCHESTRATOR_CONTAINER}),
            )
        except Exception:
            log.error("Aged-runner cleanup failed", exc_info=True)

        tick_count += 1
        if tick_count % prune_every == 0:
            try:
                docker.prune_images()
                docker.prune_build_cache()
                docker.prune_volumes()
            except Exception:
                log.error("Docker prune failed", exc_info=True)

        time.sleep(poll)


if __name__ == "__main__":
    main()
