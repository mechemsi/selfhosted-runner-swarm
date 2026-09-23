# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Control endpoints: stop and protect containers, pause and scale pools, edit config."""

import json
import os
from typing import Any

from flask import Blueprint, jsonify

from rorch.config import PoolConfig, validation_errors
from rorch.resolver import (
    GLOBAL_KEYS,
    TUNABLE_FIELDS,
    coerce_pool_fields,
    unknown_fields,
)
from rorch.server.auth import idempotent, require_write
from rorch.server.deps import Deps, _actor, _deps, _is_runner_container, _json_body, _pool_by_name

bp = Blueprint("control", __name__)


@bp.post("/api/containers/<name>/stop")
@require_write
@idempotent
def stop_container(name: str) -> Any:
    deps = _deps()
    return _stop(deps, name, action="stop")


@bp.post("/api/containers/<name>/restart")
@require_write
@idempotent
def restart_container(name: str) -> Any:
    deps = _deps()
    # Runners are ephemeral: there is nothing to restart in place, so this
    # removes the container and lets the next tick provision a replacement.
    return _stop(deps, name, action="restart")


@bp.post("/api/containers/<name>/protect")
@require_write
def protect_container(name: str) -> Any:
    deps = _deps()
    if not _is_runner_container(name):
        return jsonify(error="not a runner container"), 400
    protected = bool(_json_body().get("protected", True))
    deps.store.set_protected(name, protected)
    deps.store.audit(_actor(), "protect" if protected else "unprotect", name)
    return jsonify(container=name, protected=protected)


@bp.post("/api/pools/<name>/state")
@require_write
def set_pool_state(name: str) -> Any:
    deps = _deps()
    if _pool_by_name(deps.resolver, name) is None:
        return jsonify(error=f"unknown pool '{name}'"), 404
    body = _json_body()
    current = deps.resolver.resolve().state_for(name)
    paused = bool(body.get("paused", current.paused))
    draining = bool(body.get("draining", current.draining))
    deps.store.set_pool_state(name, paused=paused, draining=draining)
    deps.store.audit(_actor(), "pool_state", name, f"paused={paused} draining={draining}")
    return jsonify(pool=name, paused=paused, draining=draining)


@bp.post("/api/pools/<name>/scale")
@require_write
@idempotent
def scale_pool(name: str) -> Any:
    deps = _deps()
    pool = _pool_by_name(deps.resolver, name)
    if pool is None:
        return jsonify(error=f"unknown pool '{name}'"), 404
    try:
        delta = int(_json_body().get("delta", 0))
    except (TypeError, ValueError):
        return jsonify(error="delta must be an integer"), 400
    if delta not in (-1, 1):
        return jsonify(error="delta must be -1 or 1"), 400
    if delta > 0:
        ok = deps.docker.spawn_runner(pool)
        deps.store.audit(_actor(), "scale_up", name, "manual +1")
        return (jsonify(pool=name, spawned=ok), 200 if ok else 502)
    victim = _idle_container(deps, pool)
    if victim is None:
        return jsonify(error="no idle runner to remove"), 409
    ok = deps.docker.stop_container(victim)
    deps.store.audit(_actor(), "scale_down", name, f"stopped {victim}")
    return jsonify(pool=name, stopped=victim, ok=ok)


@bp.post("/api/pause")
@require_write
def global_pause() -> Any:
    deps = _deps()
    paused = bool(_json_body().get("paused", True))
    deps.store.set_global("paused", "1" if paused else "0")
    deps.store.audit(_actor(), "global_pause", detail=str(paused))
    return jsonify(paused=paused)


@bp.patch("/api/config/pools/<name>")
@require_write
def patch_pool(name: str) -> Any:
    deps = _deps()
    body = _json_body()
    rejected = unknown_fields(body)
    if rejected:
        return jsonify(error=f"not editable: {', '.join(rejected)}"), 400
    base = next((p for p in deps.resolver.base_pools if p.name == name), None)
    existing = deps.store.pool_overrides().get(name)
    if base is None and (existing is None or existing["origin"] != "ui"):
        return jsonify(error=f"unknown pool '{name}'"), 404
    merged = dict(existing["data"]) if existing else {}
    try:
        merged.update(coerce_pool_fields(body))
    except (TypeError, ValueError) as error:
        return jsonify(error=f"invalid value: {error}"), 400
    origin = existing["origin"] if existing else "yaml"
    candidate = _candidate_pool(deps, name, merged, origin)
    if candidate is None:
        return jsonify(error="override does not produce a usable pool"), 400
    errors = validation_errors([candidate])
    if errors:
        return jsonify(error="; ".join(errors)), 400
    deps.store.set_pool_override(name, merged, origin=origin)
    deps.store.audit(_actor(), "config_patch", name, json.dumps(body))
    return jsonify(pool=name, overrides=merged)


@bp.delete("/api/config/pools/<name>/overrides")
@require_write
def reset_pool(name: str) -> Any:
    """Drop the override row so the pool reverts to its config.yml definition."""
    deps = _deps()
    if not any(p.name == name for p in deps.resolver.base_pools):
        return jsonify(error=f"'{name}' has no config.yml definition to revert to"), 404
    deps.store.delete_pool_override(name)
    deps.store.audit(_actor(), "config_reset", name)
    return jsonify(pool=name, reset=True)


@bp.post("/api/config/pools")
@require_write
def create_pool() -> Any:
    deps = _deps()
    body = _json_body()
    name = str(body.pop("name", "")).strip()
    if not name:
        return jsonify(error="name is required"), 400
    rejected = unknown_fields(body, allow_identity=True)
    if rejected:
        return jsonify(error=f"not accepted: {', '.join(rejected)}"), 400
    if any(p.name == name for p in deps.resolver.resolve().pools):
        return jsonify(error=f"pool '{name}' already exists"), 409
    data = {k: v for k, v in body.items() if k != "name"}
    candidate = _candidate_pool(deps, name, data, "ui")
    if candidate is None:
        return jsonify(error="pool definition is unusable (is its PAT env var set?)"), 400
    errors = validation_errors([candidate])
    if errors:
        return jsonify(error="; ".join(errors)), 400
    deps.store.set_pool_override(name, data, origin="ui")
    deps.store.audit(_actor(), "config_create_pool", name)
    return jsonify(pool=name, created=True), 201


@bp.delete("/api/config/pools/<name>")
@require_write
def remove_pool(name: str) -> Any:
    deps = _deps()
    overrides = deps.store.pool_overrides()
    is_ui = name in overrides and overrides[name]["origin"] == "ui"
    if is_ui:
        deps.store.delete_pool_override(name)
    elif any(p.name == name for p in deps.resolver.base_pools):
        # A YAML-defined pool can't be deleted from the database, only
        # switched off; removing the row would just resurrect it.
        data = overrides[name]["data"] if name in overrides else {}
        deps.store.set_pool_override(name, data, origin="yaml", disabled=True)
    else:
        return jsonify(error=f"unknown pool '{name}'"), 404
    deps.store.audit(_actor(), "config_remove_pool", name)
    return jsonify(pool=name, removed=True)


@bp.patch("/api/config/globals")
@require_write
def patch_globals() -> Any:
    deps = _deps()
    body = _json_body()
    rejected = sorted(k for k in body if k not in GLOBAL_KEYS)
    if rejected:
        return jsonify(error=f"not editable: {', '.join(rejected)}"), 400
    for key, value in body.items():
        if key == "paused":
            deps.store.set_global(key, "1" if value else "0")
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            return jsonify(error=f"{key} must be an integer"), 400
        if number < 0:
            return jsonify(error=f"{key} cannot be negative"), 400
        deps.store.set_global(key, str(number))
    deps.store.audit(_actor(), "config_globals", detail=json.dumps(body))
    effective = deps.resolver.resolve()
    return jsonify(
        max_total_runners=effective.max_total_runners,
        max_runner_lifetime=effective.max_runner_lifetime,
        paused=effective.paused,
    )


def _candidate_pool(deps: Deps, name: str, data: dict[str, Any], origin: str) -> PoolConfig | None:
    """The pool this override would produce, for validation before saving."""
    from dataclasses import replace

    if origin == "ui":
        pat = os.environ.get(str(data.get("pat_env", "GITHUB_PAT")), "")
        if not pat:
            return None
        fields = coerce_pool_fields({k: v for k, v in data.items() if k != "pat_env"})
        fields.pop("name", None)
        try:
            return PoolConfig(name=name, pat=pat, **fields)
        except (TypeError, ValueError):
            return None
    base = next((p for p in deps.resolver.base_pools if p.name == name), None)
    if base is None:
        return None
    clean = {k: v for k, v in coerce_pool_fields(data).items() if k in TUNABLE_FIELDS}
    try:
        return replace(base, **clean)
    except (TypeError, ValueError):
        return None


def _idle_container(deps: Deps, pool: PoolConfig) -> str | None:
    """Oldest idle runner in the pool, or None if every runner is busy."""
    status = deps.store.runner_status()
    candidates = [
        info
        for info in deps.docker.container_details(pool.container_prefix)
        if not status.get(info.name, {}).get("busy", False)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda info: info.minutes).name


def _stop(deps: Deps, name: str, action: str) -> Any:
    if not _is_runner_container(name):
        return jsonify(error="not a runner container"), 400
    body = _json_body()
    busy = deps.store.runner_status().get(name, {}).get("busy", False)
    if busy and not body.get("confirm"):
        # Killing a busy runner aborts a real CI job mid-flight.
        return (
            jsonify(
                error="runner is busy — resend with confirm=true to abort its job",
                busy=True,
            ),
            409,
        )
    ok = deps.docker.stop_container(name)
    deps.store.audit(
        _actor(),
        action,
        name,
        f"busy={busy} reason={body.get('reason', '')}",
    )
    return jsonify(container=name, action=action, ok=ok, was_busy=busy)
