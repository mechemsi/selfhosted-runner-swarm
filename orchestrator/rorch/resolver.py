# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Effective configuration: config.yml as the base, database rows as an overlay.

Resolution order is always `config.yml` (or env) → database override → effective
`PoolConfig`. With no store, or a store holding no overrides, the result is
identical to what `load_config()` returned before the database existed, so
deleting `rorch.db` reverts the orchestrator to pure-YAML behaviour.
"""

import dataclasses
import logging
import os
from dataclasses import dataclass, replace
from typing import Any

from rorch.config import PoolConfig, validation_errors
from rorch.store import PoolState, SqliteStore

log = logging.getLogger(__name__)

# Pool fields an operator may override at runtime. `name`, `owner`, `repo` and
# `scope` define a pool's identity (and `pat` is a secret), so they are only
# settable when creating a pool, never as an override on a YAML-defined one.
TUNABLE_FIELDS = frozenset(
    {
        "max_runners",
        "min_idle",
        "runner_labels",
        "runner_image",
        "memory_limit",
        "cpu_limit",
        "repo_discovery_ttl",
        "github_poll_interval",
        "repo_check_workers",
        "runner_operation_workers",
        "network_mode",
    }
)

# Identity fields a UI-created pool must carry in addition to the tunables.
IDENTITY_FIELDS = frozenset({"name", "owner", "repo", "scope"})

GLOBAL_KEYS = frozenset({"max_total_runners", "max_runner_lifetime", "paused"})

_NUMERIC_INT = frozenset(
    {
        "max_runners",
        "min_idle",
        "repo_discovery_ttl",
        "github_poll_interval",
        "repo_check_workers",
        "runner_operation_workers",
    }
)


@dataclass(frozen=True)
class EffectiveConfig:
    """One resolved view of the world, rebuilt every tick."""

    pools: list[PoolConfig]
    max_total_runners: int
    max_runner_lifetime: int
    paused: bool
    states: dict[str, PoolState]
    protected: frozenset[str]

    def state_for(self, pool_name: str) -> PoolState:
        return self.states.get(pool_name, PoolState())


def coerce_pool_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce JSON-ish override values into the types PoolConfig expects."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in _NUMERIC_INT:
            out[key] = int(value)
        elif key == "cpu_limit":
            out[key] = float(value)
        elif key in {"network_mode", "scope"}:
            out[key] = str(value).lower()
        else:
            out[key] = str(value)
    return out


def unknown_fields(data: dict[str, Any], allow_identity: bool = False) -> list[str]:
    """Reject anything not explicitly editable — notably `pat`."""
    allowed = TUNABLE_FIELDS | (IDENTITY_FIELDS | {"pat_env"} if allow_identity else frozenset())
    return sorted(key for key in data if key not in allowed)


def pool_to_dict(pool: PoolConfig) -> dict[str, Any]:
    """Serialise a pool for the API. The PAT is never included."""
    data = {f.name: getattr(pool, f.name) for f in dataclasses.fields(pool) if f.name != "pat"}
    data["display"] = pool.display
    data["container_prefix"] = pool.container_prefix
    return data


class ConfigResolver:
    """Merges the YAML base with database overrides on every call."""

    def __init__(
        self,
        base_pools: list[PoolConfig],
        base_max_total_runners: int,
        base_max_runner_lifetime: int,
        store: SqliteStore | None = None,
    ) -> None:
        self._base_pools = list(base_pools)
        self._base_max_total = base_max_total_runners
        self._base_max_lifetime = base_max_runner_lifetime
        self._store = store

    @property
    def base_pools(self) -> list[PoolConfig]:
        return list(self._base_pools)

    def resolve(self) -> EffectiveConfig:
        if self._store is None:
            return EffectiveConfig(
                pools=list(self._base_pools),
                max_total_runners=self._base_max_total,
                max_runner_lifetime=self._base_max_lifetime,
                paused=False,
                states={},
                protected=frozenset(),
            )

        overrides = self._store.pool_overrides()
        globals_ = self._store.global_overrides()

        pools: list[PoolConfig] = []
        for base in self._base_pools:
            entry = overrides.pop(base.name, None)
            if entry is None:
                pools.append(base)
                continue
            if entry["disabled"]:
                log.debug("Pool '%s' disabled by override", base.name)
                continue
            pools.append(self._apply(base, entry["data"]))

        # Rows with no YAML counterpart are pools created through the API.
        for name, entry in overrides.items():
            if entry["origin"] != "ui" or entry["disabled"]:
                continue
            created = self._build_ui_pool(name, entry["data"])
            if created is not None:
                pools.append(created)

        return EffectiveConfig(
            pools=pools,
            max_total_runners=_as_int(globals_, "max_total_runners", self._base_max_total),
            max_runner_lifetime=_as_int(globals_, "max_runner_lifetime", self._base_max_lifetime),
            paused=globals_.get("paused", "0") == "1",
            states=self._store.pool_states(),
            protected=self._store.protected_containers(),
        )

    @staticmethod
    def _apply(base: PoolConfig, data: dict[str, Any]) -> PoolConfig:
        clean = {k: v for k, v in coerce_pool_fields(data).items() if k in TUNABLE_FIELDS}
        if not clean:
            return base
        try:
            return replace(base, **clean)
        except (TypeError, ValueError):
            log.warning("Ignoring invalid override for pool '%s': %s", base.name, data)
            return base

    @staticmethod
    def _build_ui_pool(name: str, data: dict[str, Any]) -> PoolConfig | None:
        """Materialise an API-created pool. Its PAT comes from the environment."""
        pat_env = str(data.get("pat_env", "GITHUB_PAT"))
        pat = os.environ.get(pat_env, "")
        if not pat:
            log.error("Pool '%s': %s is not set in the environment — skipping", name, pat_env)
            return None
        fields = coerce_pool_fields(
            {k: v for k, v in data.items() if k in TUNABLE_FIELDS | IDENTITY_FIELDS}
        )
        fields.pop("name", None)
        try:
            pool = PoolConfig(name=name, pat=pat, **fields)
        except (TypeError, ValueError):
            log.error("Pool '%s': unusable definition %s — skipping", name, data)
            return None
        errors = validation_errors([pool])
        if errors:
            log.error("Pool '%s' is invalid, skipping: %s", name, "; ".join(errors))
            return None
        return pool


def _as_int(values: dict[str, str], key: str, fallback: int) -> int:
    raw = values.get(key)
    if raw is None:
        return fallback
    try:
        parsed = int(raw)
    except ValueError:
        log.warning("Global override '%s' is not an integer (%r) — using %d", key, raw, fallback)
        return fallback
    return parsed if parsed >= 0 else fallback
