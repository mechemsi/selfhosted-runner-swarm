# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""The Flask factory and the production WSGI server that serves it in-process.

The API reads the scaler's live state (store, resolver, docker client), so it
must run inside the orchestrator process: a pre-fork server such as gunicorn
would give each worker its own copy of that state. Waitress serves the app
from a thread pool in this process instead. It is pure Python with no
dependencies of its own, and it buffers each request in full before handing it
to a worker thread, so a slow client cannot pin one of the few workers.
"""

import logging
import threading
import time
from typing import Any

from flask import Flask, Response, g, request
from waitress.server import BaseWSGIServer, MultiSocketServer, create_server
from waitress.trigger import trigger
from waitress.wasyncore import close_all

from rorch.server import control_routes, read_routes
from rorch.server.auth import LOOPBACK_BINDS
from rorch.server.deps import Deps

log = logging.getLogger(__name__)

# Concurrent requests being handled. The dashboard polls /api/state while a
# control action may wait seconds on `docker rm` or `docker run`; eight leaves
# room for both without a poll ever queueing behind a slow action.
API_THREADS = 8
# Control bodies are a few hundred bytes. Anything near this is not a client of ours.
MAX_REQUEST_BODY_BYTES = 1024 * 1024
# How long stop() lets in-flight requests finish. Kept well inside Docker's
# default 10s stop timeout, which also has to cover the scaling loop.
STOP_TIMEOUT_SECONDS = 3


def create_app(deps: Deps) -> Flask:
    """Build the Flask app. Kept a factory so tests can drive it without a socket."""
    app = Flask(__name__)
    app.config["RORCH"] = deps
    app.register_blueprint(read_routes.bp)
    app.register_blueprint(control_routes.bp)
    app.before_request(_mark_start)
    app.after_request(_log_request)
    return app


def _mark_start() -> None:
    g.rorch_started = time.monotonic()


def _log_request(response: Response) -> Response:
    """One line per request, at a level that keeps routine polling out of the logs.

    The dashboard polls several endpoints every few seconds; at INFO that buried
    everything else. Successes are DEBUG, client errors (a bad token, an unknown
    pool) stay visible at INFO, and server errors are WARNING. Unhandled
    exceptions are additionally logged with their traceback by Flask.
    """
    status = response.status_code
    if status >= 500:
        level = logging.WARNING
    elif status >= 400:
        level = logging.INFO
    else:
        level = logging.DEBUG
    if log.isEnabledFor(level):
        started = g.get("rorch_started")
        elapsed_ms = (time.monotonic() - started) * 1000 if started is not None else 0.0
        log.log(
            level,
            "%s %s %s %d %.0fms",
            request.remote_addr or "-",
            request.method,
            request.path,
            status,
            elapsed_ms,
        )
    return response


class ApiServer:
    """The running API: a waitress server whose event loop owns one thread."""

    def __init__(self, app: Flask, host: str, port: int) -> None:
        # Our own socket map, so stop() can close every connection the server
        # holds, including idle keep-alive ones that would otherwise keep the
        # event loop (and with it the thread) alive.
        self._map: dict[int, Any] = {}
        self._server: BaseWSGIServer | MultiSocketServer = create_server(
            app,
            map=self._map,
            host=host,
            port=port,
            threads=API_THREADS,
            ident="rorch",
            max_request_body_size=MAX_REQUEST_BODY_BYTES,
        )
        # Every listening socket registers a trigger in the map; pulling one
        # runs a callback on the event-loop thread. "localhost" can bind two
        # sockets (IPv4 and IPv6), so take whichever comes first.
        self._trigger = next(d for d in self._map.values() if isinstance(d, trigger))
        # Read now: the listening socket is gone once the server is stopped.
        self.port = _bound_port(self._server)
        self.thread = threading.Thread(target=self._serve, name="rorch-api", daemon=True)
        self._stopped = False

    def start(self) -> None:
        self.thread.start()

    def _serve(self) -> None:
        try:
            self._server.run()
        except Exception:
            log.error("Dashboard server stopped", exc_info=True)

    def stop(self, timeout: int = STOP_TIMEOUT_SECONDS) -> bool:
        """Stop accepting, let in-flight requests finish, close every connection.

        Returns whether the server thread had exited by the deadline. Safe to
        call more than once.
        """
        if self._stopped:
            return not self.thread.is_alive()
        self._stopped = True
        deadline = time.monotonic() + timeout
        # Waits for requests already running; anything still queued is dropped.
        self._server.task_dispatcher.shutdown(cancel_pending=True, timeout=timeout)
        # The socket map belongs to the event-loop thread, so close it from
        # there: the trigger runs the callback inside the loop.
        self._trigger.pull_trigger(self._close_connections)
        # A short floor so the loop can run the close even if requests used the budget.
        self.thread.join(max(0.5, deadline - time.monotonic()))
        stopped = not self.thread.is_alive()
        if stopped:
            log.info("Dashboard server stopped")
        else:
            log.warning("Dashboard server did not stop within %.0fs", timeout)
        return stopped

    def _close_connections(self, _unused: None = None) -> None:
        # waitress calls trigger callbacks with no argument; its type stubs
        # declare one, hence the ignored optional parameter.
        close_all(self._map)


def _bound_port(server: BaseWSGIServer | MultiSocketServer) -> int:
    """The port actually bound; differs from the requested one when that was 0."""
    if isinstance(server, MultiSocketServer):
        return int(server.effective_listen[0][1])
    return int(server.getsockname()[1])


def start(deps: Deps, host: str, port: int) -> ApiServer | None:
    """Serve the API from a background thread. Returns None if it refuses to start."""
    if host not in LOOPBACK_BINDS and not deps.token:
        log.error(
            "Refusing to bind %s without RORCH_API_TOKEN — this port can start "
            "root containers on the host via the mounted Docker socket",
            host,
        )
        return None

    server = ApiServer(create_app(deps), host, port)
    server.start()
    log.info(
        "Dashboard listening on http://%s:%d%s",
        host,
        server.port,
        "" if deps.token else " (no token)",
    )
    return server
