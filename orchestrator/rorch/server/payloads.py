# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""JSON and Prometheus views of the store's history, for /api/state and /metrics."""

import time
from typing import Any

from rorch.docker_client import ORCHESTRATOR_CONTAINER
from rorch.resolver import (
    pool_to_dict,
)
from rorch.scaler import GLOBAL_CONTAINER_PREFIX
from rorch.server.deps import Deps

COUNTERS = ("containers", "online", "idle", "busy", "queued")


def group_snapshots(
    pool_names: list[str], snapshots: dict[str, dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Attach each derived repository snapshot to the pool that produced it.

    A personal-scope pool inspects one derived pool per repository
    (`PoolConfig.for_repository` names them `{pool}-{repo}`), so all the real
    numbers land under names that never match the configured pool. Without this
    the dashboard shows a personal pool as permanently idle while dozens of
    repositories are actually being scaled.
    """
    # Longest first, so `personal-web` claims its own children before `personal`.
    ordered = sorted(pool_names, key=len, reverse=True)
    children: dict[str, list[dict[str, Any]]] = {name: [] for name in pool_names}
    for key, snapshot in snapshots.items():
        if key in children:
            continue
        parent = next((name for name in ordered if key.startswith(f"{name}-")), None)
        if parent is not None:
            children[parent].append(snapshot)
    for rows in children.values():
        rows.sort(key=lambda row: row.get("pool", ""))
    return children


def _rollup(own: dict[str, Any], children: list[dict[str, Any]]) -> dict[str, Any]:
    """One pool's numbers: its own tick, or the sum of its repository ticks."""
    if own:
        totals = {key: own.get(key, 0) for key in COUNTERS}
        return {
            **totals,
            # Repositories are inspected in parallel, so the slowest one is the
            # tick cost — summing would overstate it.
            "duration": own.get("duration", 0.0),
            "last_tick": own.get("ts", 0.0),
        }
    return {
        **{key: sum(row.get(key, 0) for row in children) for key in COUNTERS},
        "duration": max((row.get("duration", 0.0) for row in children), default=0.0),
        "last_tick": max((row.get("ts", 0.0) for row in children), default=0.0),
    }


def _state_payload(deps: Deps) -> dict[str, Any]:
    effective = deps.resolver.resolve()
    snapshots = deps.store.latest_snapshots()
    status = deps.store.runner_status()
    containers = deps.docker.container_details(GLOBAL_CONTAINER_PREFIX)
    protected = effective.protected
    children = group_snapshots([p.name for p in effective.pools], snapshots)

    pools = []
    for pool in effective.pools:
        kids = children[pool.name]
        state = effective.state_for(pool.name)
        pools.append(
            {
                "config": pool_to_dict(pool),
                "paused": state.paused,
                "draining": state.draining,
                **_rollup(snapshots.get(pool.name, {}), kids),
                # Only repositories doing something: a personal account can
                # carry dozens of quiet repos that would bury the signal.
                "repos": [
                    {key: row.get(key, 0) for key in COUNTERS} | {"name": row["pool"]}
                    for row in kids
                    if any(row.get(key, 0) for key in COUNTERS)
                ],
                "repo_count": len(kids),
            }
        )

    running_jobs = deps.store.jobs_by_runner()
    return {
        "pools": pools,
        "containers": [
            {
                "name": info.name,
                "image": info.image,
                "status": info.status,
                "running_for": info.running_for,
                "minutes": round(info.minutes, 1),
                "protected": info.name in protected,
                "github": status.get(info.name, {}),
                # What this runner is executing right now, if anything.
                "job": running_jobs.get(info.name),
            }
            for info in containers
            if info.name != ORCHESTRATOR_CONTAINER
        ],
        "jobs": deps.store.recent_jobs(25),
        "globals": {
            "max_total_runners": effective.max_total_runners,
            "max_runner_lifetime": effective.max_runner_lifetime,
            "paused": effective.paused,
            "total_containers": sum(
                1 for info in containers if info.name != ORCHESTRATOR_CONTAINER
            ),
        },
        "rate_limit": deps.rate_limit_status(),
        "events": deps.store.recent_events(25),
        "ts": time.time(),
    }


def _prometheus(deps: Deps) -> str:
    """Minimal text-format exposition — no client library needed."""
    effective = deps.resolver.resolve()
    snapshots = deps.store.latest_snapshots()
    # Emit a series per snapshot key, not per configured pool: a personal pool
    # records one snapshot per repository, and iterating configured pools would
    # report every one of them as zero.
    keys = sorted(snapshots) or [pool.name for pool in effective.pools]
    lines: list[str] = []
    for metric, help_text in (
        ("containers", "Runner containers currently running."),
        ("online", "Runners GitHub reports online."),
        ("idle", "Online runners not executing a job."),
        ("busy", "Online runners executing a job."),
        ("queued", "Queued jobs waiting for a runner."),
    ):
        lines.append(f"# HELP rorch_pool_{metric} {help_text}")
        lines.append(f"# TYPE rorch_pool_{metric} gauge")
        for key in keys:
            value = snapshots.get(key, {}).get(metric, 0)
            lines.append(f'rorch_pool_{metric}{{pool="{key}"}} {value}')

    counts = deps.store.event_counts_since(24)
    lines.append("# HELP rorch_events_24h Runner lifecycle events in the last 24 hours.")
    lines.append("# TYPE rorch_events_24h gauge")
    for event, count in sorted(counts.items()):
        lines.append(f'rorch_events_24h{{event="{event}"}} {count}')

    lines.append("# HELP rorch_max_total_runners Global ceiling (0 = unlimited).")
    lines.append("# TYPE rorch_max_total_runners gauge")
    lines.append(f"rorch_max_total_runners {effective.max_total_runners}")
    lines.append("# HELP rorch_paused Whether all provisioning is paused.")
    lines.append("# TYPE rorch_paused gauge")
    lines.append(f"rorch_paused {int(effective.paused)}")

    rate = deps.rate_limit_status()
    if rate.get("remaining") is not None:
        lines.append("# HELP rorch_github_rate_remaining Last seen GitHub API budget.")
        lines.append("# TYPE rorch_github_rate_remaining gauge")
        lines.append(f"rorch_github_rate_remaining {rate['remaining']}")
    return "\n".join(lines) + "\n"
