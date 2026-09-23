# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""The Flask factory and the daemon thread that serves it."""

import logging
import threading

from flask import Flask

from rorch.server import control_routes, read_routes
from rorch.server.auth import LOOPBACK_BINDS
from rorch.server.deps import Deps

log = logging.getLogger(__name__)


def create_app(deps: Deps) -> Flask:
    """Build the Flask app. Kept a factory so tests can drive it without a socket."""
    app = Flask(__name__)
    app.config["RORCH"] = deps
    app.register_blueprint(read_routes.bp)
    app.register_blueprint(control_routes.bp)
    return app


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
