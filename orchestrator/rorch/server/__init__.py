# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Authenticated HTTP surface: dashboard, JSON API, control actions, metrics.

Served in-process by waitress from background threads beside the scaling loop,
which keeps owning the process; see `rorch.server.app` for why in-process.

SECURITY: the orchestrator mounts /var/run/docker.sock, so anything that can
reach this port can start containers as root on the host. It binds to loopback
by default and refuses to bind anywhere else without a token.
"""

from rorch.server.app import ApiServer, create_app, start
from rorch.server.auth import resolve_readonly_token, resolve_token
from rorch.server.deps import MAX_TOKEN_FAILURES, Deps, TokenGuard

__all__ = [
    "MAX_TOKEN_FAILURES",
    "ApiServer",
    "Deps",
    "TokenGuard",
    "create_app",
    "resolve_readonly_token",
    "resolve_token",
    "start",
]
