# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Authenticated HTTP surface: dashboard, JSON API, control actions, metrics.

Runs in a daemon thread beside the scaling loop, which keeps owning the process.

SECURITY: the orchestrator mounts /var/run/docker.sock, so anything that can
reach this port can start containers as root on the host. It binds to loopback
by default and refuses to bind anywhere else without a token.
"""

import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

import yaml
from flask import Flask, Response, current_app, jsonify, make_response, request

from rorch.config import PoolConfig, validation_errors
from rorch.docker_client import ORCHESTRATOR_CONTAINER
from rorch.protocols import ContainerManager
from rorch.resolver import (
    GLOBAL_KEYS,
    TUNABLE_FIELDS,
    ConfigResolver,
    coerce_pool_fields,
    pool_to_dict,
    unknown_fields,
)
from rorch.scaler import GLOBAL_CONTAINER_PREFIX
from rorch.store import SqliteStore

log = logging.getLogger(__name__)

DASHBOARD_HTML = Path(__file__).with_name("dashboard.html")
AUTH_COOKIE = "rorch_token"
LOOPBACK_BINDS = {"127.0.0.1", "localhost", "::1"}


class Deps:
    """Everything the HTTP layer is allowed to touch."""

    def __init__(
        self,
        store: SqliteStore,
        resolver: ConfigResolver,
        docker: ContainerManager,
        rate_limit_status: Callable[[], dict[str, Any]] | None = None,
        token: str = "",
    ) -> None:
        self.store = store
        self.resolver = resolver
        self.docker = docker
        self.rate_limit_status = rate_limit_status or (lambda: {})
        self.token = token


def _deps() -> Deps:
    return current_app.config["RORCH"]


def _actor() -> str:
    return request.headers.get("X-Actor", request.remote_addr or "unknown")


def require_auth(view: Callable[..., Any]) -> Callable[..., Any]:
    """Bearer header, cookie, or ?token=. No configured token means open access."""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        expected = _deps().token
        if not expected:
            return view(*args, **kwargs)
        header = request.headers.get("Authorization", "")
        supplied = (
            header[len("Bearer ") :]
            if header.startswith("Bearer ")
            else request.args.get("token") or request.cookies.get(AUTH_COOKIE) or ""
        )
        if not hmac.compare_digest(supplied, expected):
            return jsonify(error="unauthorized"), 401
        return view(*args, **kwargs)

    return wrapper


def idempotent(view: Callable[..., Any]) -> Callable[..., Any]:
    """Replay the first response when a caller retries with the same key.

    External controllers retry on timeouts; without this a retried spawn would
    provision a second runner for the same intent.
    """

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        key = request.headers.get("Idempotency-Key", "")
        if not key:
            return view(*args, **kwargs)
        store = _deps().store
        scoped = f"{request.method}:{request.path}:{key}"
        cached = store.idempotent_response(scoped)
        if cached is not None:
            response = make_response(cached)
            response.mimetype = "application/json"
            response.headers["Idempotency-Replayed"] = "true"
            return response
        result = view(*args, **kwargs)
        body, status = result if isinstance(result, tuple) else (result, 200)
        if 200 <= status < 300:
            store.remember_response(scoped, body.get_data(as_text=True))
        return result

    return wrapper


def _json_body() -> dict[str, Any]:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _pool_by_name(resolver: ConfigResolver, name: str) -> PoolConfig | None:
    return next((p for p in resolver.resolve().pools if p.name == name), None)


def create_app(deps: Deps) -> Flask:
    """Build the Flask app. Kept a factory so tests can drive it without a socket."""
    app = Flask(__name__)
    app.config["RORCH"] = deps

    # ── dashboard ──────────────────────────────────────────────────────────

    @app.get("/")
    @require_auth
    def dashboard() -> Response:
        response = make_response(DASHBOARD_HTML.read_text(encoding="utf-8"))
        response.mimetype = "text/html"
        # A browser can't attach an Authorization header to a navigation, so a
        # ?token= visit converts it into a cookie for subsequent fetches.
        if deps.token and request.args.get("token"):
            response.set_cookie(
                AUTH_COOKIE, deps.token, httponly=True, samesite="Strict", max_age=86400
            )
        return response

    @app.get("/api/health")
    def health() -> Response:
        return jsonify(status="ok", ts=time.time())

    # ── read ───────────────────────────────────────────────────────────────

    @app.get("/api/state")
    @require_auth
    def state() -> Response:
        return jsonify(_state_payload(deps))

    @app.get("/api/events")
    @require_auth
    def events() -> Response:
        return jsonify(events=deps.store.recent_events(_limit(200)))

    @app.get("/api/audit")
    @require_auth
    def audit() -> Response:
        return jsonify(audit=deps.store.recent_audit(_limit(100)))

    @app.get("/api/jobs")
    @require_auth
    def jobs() -> Response:
        """What each runner ran: workflow, job, outcome and duration."""
        return jsonify(jobs=deps.store.recent_jobs(_limit(50)))

    @app.get("/api/history")
    @require_auth
    def history() -> Response:
        hours = min(max(float(request.args.get("hours", 6)), 0.1), 720)
        return jsonify(
            snapshots=deps.store.snapshots_since(hours),
            event_counts=deps.store.event_counts_since(hours),
            hours=hours,
        )

    @app.get("/api/containers/<name>/logs")
    @require_auth
    def container_logs(name: str) -> Response:
        if not _is_runner_container(name):
            return jsonify(error="not a runner container"), 400  # type: ignore[return-value]
        text = deps.docker.container_logs(name, tail=_limit(500, param="tail"))
        response = make_response(text)
        response.mimetype = "text/plain"
        return response

    @app.get("/metrics")
    @require_auth
    def metrics() -> Response:
        response = make_response(_prometheus(deps))
        response.mimetype = "text/plain; version=0.0.4"
        return response

    # ── container control ──────────────────────────────────────────────────

    @app.post("/api/containers/<name>/stop")
    @require_auth
    @idempotent
    def stop_container(name: str) -> Any:
        return _stop(deps, name, action="stop")

    @app.post("/api/containers/<name>/restart")
    @require_auth
    @idempotent
    def restart_container(name: str) -> Any:
        # Runners are ephemeral: there is nothing to restart in place, so this
        # removes the container and lets the next tick provision a replacement.
        return _stop(deps, name, action="restart")

    @app.post("/api/containers/<name>/protect")
    @require_auth
    def protect_container(name: str) -> Any:
        if not _is_runner_container(name):
            return jsonify(error="not a runner container"), 400
        protected = bool(_json_body().get("protected", True))
        deps.store.set_protected(name, protected)
        deps.store.audit(_actor(), "protect" if protected else "unprotect", name)
        return jsonify(container=name, protected=protected)

    # ── pool control ───────────────────────────────────────────────────────

    @app.post("/api/pools/<name>/state")
    @require_auth
    def set_pool_state(name: str) -> Any:
        if _pool_by_name(deps.resolver, name) is None:
            return jsonify(error=f"unknown pool '{name}'"), 404
        body = _json_body()
        current = deps.resolver.resolve().state_for(name)
        paused = bool(body.get("paused", current.paused))
        draining = bool(body.get("draining", current.draining))
        deps.store.set_pool_state(name, paused=paused, draining=draining)
        deps.store.audit(_actor(), "pool_state", name, f"paused={paused} draining={draining}")
        return jsonify(pool=name, paused=paused, draining=draining)

    @app.post("/api/pools/<name>/scale")
    @require_auth
    @idempotent
    def scale_pool(name: str) -> Any:
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

    @app.post("/api/pause")
    @require_auth
    def global_pause() -> Any:
        paused = bool(_json_body().get("paused", True))
        deps.store.set_global("paused", "1" if paused else "0")
        deps.store.audit(_actor(), "global_pause", detail=str(paused))
        return jsonify(paused=paused)

    # ── config ─────────────────────────────────────────────────────────────

    @app.get("/api/config")
    @require_auth
    def get_config() -> Response:
        effective = deps.resolver.resolve()
        return jsonify(
            pools=[pool_to_dict(p) for p in effective.pools],
            base_pools=[pool_to_dict(p) for p in deps.resolver.base_pools],
            overrides=deps.store.pool_overrides(),
            globals={
                "max_total_runners": effective.max_total_runners,
                "max_runner_lifetime": effective.max_runner_lifetime,
                "paused": effective.paused,
            },
            editable_fields=sorted(TUNABLE_FIELDS),
        )

    @app.patch("/api/config/pools/<name>")
    @require_auth
    def patch_pool(name: str) -> Any:
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

    @app.delete("/api/config/pools/<name>/overrides")
    @require_auth
    def reset_pool(name: str) -> Any:
        """Drop the override row so the pool reverts to its config.yml definition."""
        if not any(p.name == name for p in deps.resolver.base_pools):
            return jsonify(error=f"'{name}' has no config.yml definition to revert to"), 404
        deps.store.delete_pool_override(name)
        deps.store.audit(_actor(), "config_reset", name)
        return jsonify(pool=name, reset=True)

    @app.post("/api/config/pools")
    @require_auth
    def create_pool() -> Any:
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

    @app.delete("/api/config/pools/<name>")
    @require_auth
    def remove_pool(name: str) -> Any:
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

    @app.patch("/api/config/globals")
    @require_auth
    def patch_globals() -> Any:
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

    @app.get("/api/config/export")
    @require_auth
    def export_config() -> Response:
        """Effective config as YAML, for copying back into config.yml."""
        effective = deps.resolver.resolve()
        document = {
            "max_total_runners": effective.max_total_runners,
            "max_runner_lifetime": effective.max_runner_lifetime,
            "pools": [
                {
                    k: v
                    for k, v in pool_to_dict(pool).items()
                    if k not in {"display", "container_prefix"}
                }
                | {"pat": "${GITHUB_PAT}"}
                for pool in effective.pools
            ],
        }
        response = make_response(yaml.safe_dump(document, sort_keys=False))
        response.mimetype = "text/yaml"
        return response

    return app


# ── helpers ────────────────────────────────────────────────────────────────


def _limit(default: int, param: str = "limit") -> int:
    try:
        return max(1, min(int(request.args.get(param, default)), 5000))
    except (TypeError, ValueError):
        return default


def _is_runner_container(name: str) -> bool:
    """Refuse to operate on anything outside the runner namespace."""
    return (
        name.startswith(f"{GLOBAL_CONTAINER_PREFIX}-")
        and name != ORCHESTRATOR_CONTAINER
        and "/" not in name
        and ".." not in name
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


def start(deps: Deps, host: str, port: int) -> threading.Thread | None:
    """Serve the API in a daemon thread. Returns None if it refuses to start."""
    if host not in LOOPBACK_BINDS and not deps.token:
        log.error(
            "Refusing to bind %s without RORCH_API_TOKEN — this port can start "
            "root containers on the host via the mounted Docker socket",
            host,
        )
        return None

    app = create_app(deps)

    def serve() -> None:
        try:
            # ponytail: Flask's built-in server. One operator on a loopback
            # port, not public traffic — put a real WSGI server in front only
            # if this ever gets exposed beyond the host.
            app.run(host=host, port=port, threaded=True, use_reloader=False)
        except Exception:
            log.error("Dashboard server stopped", exc_info=True)

    thread = threading.Thread(target=serve, name="rorch-api", daemon=True)
    thread.start()
    log.info(
        "Dashboard listening on http://%s:%d%s", host, port, "" if deps.token else " (no token)"
    )
    return thread


def resolve_token() -> str:
    """Token from the environment, generating one when bound off-loopback."""
    token = os.environ.get("RORCH_API_TOKEN", "").strip()
    if token:
        return token
    if os.environ.get("RORCH_API_HOST", "127.0.0.1") not in LOOPBACK_BINDS:
        generated = secrets.token_urlsafe(24)
        log.warning("RORCH_API_TOKEN unset — generated for this run: %s", generated)
        return generated
    return ""
