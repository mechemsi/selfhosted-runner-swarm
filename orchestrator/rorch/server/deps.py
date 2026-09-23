# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""What every request handler reaches for: the wired dependencies and request helpers."""

import threading
import time
from collections.abc import Callable
from typing import Any

from flask import current_app, request

from rorch.config import PoolConfig
from rorch.docker_client import ORCHESTRATOR_CONTAINER
from rorch.protocols import ContainerManager
from rorch.resolver import (
    ConfigResolver,
)
from rorch.scaler import GLOBAL_CONTAINER_PREFIX
from rorch.store import Store

# Brute-force protection. The token is the only credential, so unlimited
# guessing against an exposed port is the whole attack.
MAX_TOKEN_FAILURES = 10
LOCKOUT_SECONDS = 300


class TokenGuard:
    """Locks out a client IP after repeated bad tokens.

    # ponytail: in-process counters, not Redis. One orchestrator, one process;
    # a restart clearing the counters is acceptable for a rate limiter whose
    # job is to make online guessing impractical, not to be a security ledger.
    """

    def __init__(
        self,
        max_failures: int = MAX_TOKEN_FAILURES,
        lockout_seconds: int = LOCKOUT_SECONDS,
    ) -> None:
        self._max = max_failures
        self._lockout = lockout_seconds
        self._lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}

    def locked_until(self, client: str) -> float:
        with self._lock:
            count, last = self._failures.get(client, (0, 0.0))
            if count < self._max:
                return 0.0
            until = last + self._lockout
            if until <= time.time():
                self._failures.pop(client, None)
                return 0.0
            return until

    def record_failure(self, client: str) -> None:
        with self._lock:
            count, _ = self._failures.get(client, (0, 0.0))
            self._failures[client] = (count + 1, time.time())

    def record_success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


class Deps:
    """Everything the HTTP layer is allowed to touch."""

    def __init__(
        self,
        store: Store,
        resolver: ConfigResolver,
        docker: ContainerManager,
        rate_limit_status: Callable[[], dict[str, Any]] | None = None,
        token: str = "",
        readonly_token: str = "",
    ) -> None:
        self.store = store
        self.resolver = resolver
        self.docker = docker
        self.rate_limit_status = rate_limit_status or (lambda: {})
        self.token = token
        # Grants every read endpoint but no control action, so a dashboard can
        # be handed out without also handing over container control.
        self.readonly_token = readonly_token
        self.guard = TokenGuard()


def _deps() -> Deps:
    return current_app.config["RORCH"]


def _actor() -> str:
    return request.headers.get("X-Actor", request.remote_addr or "unknown")


def _client() -> str:
    return request.remote_addr or "unknown"


def _json_body() -> dict[str, Any]:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _limit(default: int, param: str = "limit") -> int:
    try:
        return max(1, min(int(request.args.get(param, default)), 5000))
    except (TypeError, ValueError):
        return default


def _pool_by_name(resolver: ConfigResolver, name: str) -> PoolConfig | None:
    return next((p for p in resolver.resolve().pools if p.name == name), None)


def _is_runner_container(name: str) -> bool:
    """Refuse to operate on anything outside the runner namespace."""
    return (
        name.startswith(f"{GLOBAL_CONTAINER_PREFIX}-")
        and name != ORCHESTRATOR_CONTAINER
        and "/" not in name
        and ".." not in name
    )
