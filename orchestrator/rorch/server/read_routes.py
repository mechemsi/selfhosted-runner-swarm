# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Read endpoints: the dashboard page, state, history, logs, metrics and config."""

import time
from pathlib import Path

import yaml
from flask import Blueprint, Response, jsonify, make_response, request

from rorch.resolver import (
    TUNABLE_FIELDS,
    pool_to_dict,
)
from rorch.server.auth import AUTH_COOKIE, require_auth
from rorch.server.deps import _deps, _is_runner_container, _limit
from rorch.server.payloads import _prometheus, _state_payload

DASHBOARD_HTML = Path(__file__).parent.parent / "dashboard.html"

bp = Blueprint("read", __name__)


@bp.get("/")
@require_auth
def dashboard() -> Response:
    deps = _deps()
    response = make_response(DASHBOARD_HTML.read_text(encoding="utf-8"))
    response.mimetype = "text/html"
    # A browser can't attach an Authorization header to a navigation, so a
    # ?token= visit converts it into a cookie for subsequent fetches.
    if deps.token and request.args.get("token"):
        response.set_cookie(
            AUTH_COOKIE, deps.token, httponly=True, samesite="Strict", max_age=86400
        )
    return response


@bp.get("/api/health")
def health() -> Response:
    return jsonify(status="ok", ts=time.time())


@bp.get("/api/state")
@require_auth
def state() -> Response:
    deps = _deps()
    return jsonify(_state_payload(deps))


@bp.get("/api/events")
@require_auth
def events() -> Response:
    deps = _deps()
    return jsonify(events=deps.store.recent_events(_limit(200)))


@bp.get("/api/audit")
@require_auth
def audit() -> Response:
    deps = _deps()
    return jsonify(audit=deps.store.recent_audit(_limit(100)))


@bp.get("/api/jobs")
@require_auth
def jobs() -> Response:
    """What each runner ran: workflow, job, outcome and duration."""
    deps = _deps()
    return jsonify(jobs=deps.store.recent_jobs(_limit(50)))


@bp.get("/api/history")
@require_auth
def history() -> Response:
    deps = _deps()
    hours = min(max(float(request.args.get("hours", 6)), 0.1), 720)
    return jsonify(
        snapshots=deps.store.snapshots_since(hours),
        event_counts=deps.store.event_counts_since(hours),
        hours=hours,
    )


@bp.get("/api/containers/<name>/logs")
@require_auth
def container_logs(name: str) -> Response:
    deps = _deps()
    if not _is_runner_container(name):
        return jsonify(error="not a runner container"), 400  # type: ignore[return-value]
    text = deps.docker.container_logs(name, tail=_limit(500, param="tail"))
    response = make_response(text)
    response.mimetype = "text/plain"
    return response


@bp.get("/metrics")
@require_auth
def metrics() -> Response:
    deps = _deps()
    response = make_response(_prometheus(deps))
    response.mimetype = "text/plain; version=0.0.4"
    return response


@bp.get("/api/config")
@require_auth
def get_config() -> Response:
    deps = _deps()
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


@bp.get("/api/config/export")
@require_auth
def export_config() -> Response:
    """Effective config as YAML, for copying back into config.yml."""
    deps = _deps()
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
