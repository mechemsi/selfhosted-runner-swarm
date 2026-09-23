# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Token scopes, brute-force guarded authorization, and idempotent control actions."""

import hmac
import logging
import os
import secrets
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import jsonify, make_response, request

from rorch.server.deps import Deps, _client, _deps
from rorch.store import Store

log = logging.getLogger(__name__)


AUTH_COOKIE = "rorch_token"
LOOPBACK_BINDS = {"127.0.0.1", "localhost", "::1"}

# Key under which a generated token is persisted, so it survives a restart
# instead of forcing the operator to re-read the logs after every deploy.
TOKEN_KEY = "api_token"


def _supplied_token() -> str:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer ") :]
    return request.args.get("token") or request.cookies.get(AUTH_COOKIE) or ""


def _scope(deps: Deps, supplied: str) -> str | None:
    """'write', 'read', or None. No configured token at all means open access."""
    if not deps.token and not deps.readonly_token:
        return "write"
    if deps.token and hmac.compare_digest(supplied, deps.token):
        return "write"
    if deps.readonly_token and hmac.compare_digest(supplied, deps.readonly_token):
        return "read"
    return None


def _authorize(write: bool) -> Any | None:
    """None when the request may proceed, else the error response to return."""
    deps = _deps()
    client = _client()

    locked = deps.guard.locked_until(client)
    if locked:
        retry_after = max(1, int(locked - time.time()))
        log.warning("Rejecting %s — too many bad tokens, locked for %ds", client, retry_after)
        response = jsonify(error="too many failed attempts")
        response.headers["Retry-After"] = str(retry_after)
        return response, 429

    scope = _scope(deps, _supplied_token())
    if scope is None:
        deps.guard.record_failure(client)
        return jsonify(error="unauthorized"), 401

    deps.guard.record_success(client)
    if write and scope != "write":
        return jsonify(error="read-only token: this action requires the control token"), 403
    return None


def require_auth(view: Callable[..., Any]) -> Callable[..., Any]:
    """Any valid token. Bearer header, cookie, or ?token=."""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        denied = _authorize(write=False)
        return denied if denied is not None else view(*args, **kwargs)

    return wrapper


def require_write(view: Callable[..., Any]) -> Callable[..., Any]:
    """Control actions and config changes — the read-only token is refused."""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        denied = _authorize(write=True)
        return denied if denied is not None else view(*args, **kwargs)

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


def resolve_token(store: Store | None = None) -> str:
    """Token from the environment, generating and persisting one if needed.

    A generated token used to be regenerated on every start, so an operator had
    to dig it back out of the logs after each deploy and no bookmark survived.
    Persisting it in the store keeps the dashboard URL stable; setting
    RORCH_API_TOKEN still overrides it.
    """
    token = os.environ.get("RORCH_API_TOKEN", "").strip()
    if token:
        return token
    if os.environ.get("RORCH_API_HOST", "127.0.0.1") in LOOPBACK_BINDS:
        return ""

    if store is not None:
        existing = store.global_overrides().get(TOKEN_KEY, "").strip()
        if existing:
            log.info("Using the stored dashboard token (set RORCH_API_TOKEN to override)")
            return existing

    generated = secrets.token_urlsafe(24)
    if store is not None:
        store.set_global(TOKEN_KEY, generated)
        log.warning(
            "RORCH_API_TOKEN unset — generated and stored, reused on restart: %s", generated
        )
    else:
        log.warning("RORCH_API_TOKEN unset — generated for this run: %s", generated)
    return generated


def resolve_readonly_token() -> str:
    """Optional token granting reads but no control actions."""
    return os.environ.get("RORCH_API_READONLY_TOKEN", "").strip()
