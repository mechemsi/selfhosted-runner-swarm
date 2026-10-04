# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Tests for the dashboard API: auth, control actions, config editing, metrics."""

import http.client
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from flask.testing import FlaskClient

from rorch.config import PoolConfig
from rorch.protocols import ContainerInfo
from rorch.resolver import ConfigResolver
from rorch.server import (
    MAX_TOKEN_FAILURES,
    ApiServer,
    Deps,
    TokenGuard,
    create_app,
    payloads,
    read_routes,
    resolve_token,
    start,
)
from rorch.store import Store, TickSnapshot


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(str(tmp_path / "rorch.db"))


@pytest.fixture
def docker() -> MagicMock:
    mock = MagicMock()
    mock.container_details.return_value = [
        ContainerInfo(
            name="gh-runner-test-pool-abc123",
            image="gh-runner:latest",
            status="Up 4 minutes",
            running_for="4 minutes ago",
            minutes=4.0,
        )
    ]
    mock.stop_container.return_value = True
    mock.spawn_runner.return_value = True
    mock.container_logs.return_value = "runner log line"
    return mock


@pytest.fixture
def deps(pool: PoolConfig, store: Store, docker: MagicMock) -> Deps:
    resolver = ConfigResolver([pool], 10, 0, store)
    return Deps(store=store, resolver=resolver, docker=docker, token="")


@pytest.fixture
def client(deps: Deps) -> FlaskClient:
    app = create_app(deps)
    app.config.update(TESTING=True)
    return app.test_client()


def _json(response: Any) -> dict[str, Any]:
    return response.get_json()


class TestAuth:
    def test_rejects_missing_token(self, deps: Deps) -> None:
        deps.token = "s3cret"
        client = create_app(deps).test_client()
        assert client.get("/api/state").status_code == 401

    def test_accepts_bearer_token(self, deps: Deps) -> None:
        deps.token = "s3cret"
        client = create_app(deps).test_client()
        response = client.get("/api/state", headers={"Authorization": "Bearer s3cret"})
        assert response.status_code == 200

    def test_accepts_query_token_and_sets_cookie(self, deps: Deps) -> None:
        deps.token = "s3cret"
        client = create_app(deps).test_client()
        response = client.get("/?token=s3cret")
        assert response.status_code == 200
        assert "rorch_token" in response.headers.get("Set-Cookie", "")

    def test_wrong_token_rejected(self, deps: Deps) -> None:
        deps.token = "s3cret"
        client = create_app(deps).test_client()
        assert client.get("/api/state?token=nope").status_code == 401

    def test_health_is_open(self, deps: Deps) -> None:
        deps.token = "s3cret"
        client = create_app(deps).test_client()
        assert client.get("/api/health").status_code == 200

    def test_health_reports_version(self, deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(read_routes, "get_version", lambda: "1.2.3")
        body = create_app(deps).test_client().get("/api/health").get_json()
        assert body["status"] == "ok"
        assert body["version"] == "1.2.3"
        assert isinstance(body["ts"], float)

    def test_state_reports_version(self, deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(payloads, "get_version", lambda: "1.2.3")
        deps.token = ""
        body = create_app(deps).test_client().get("/api/state").get_json()
        assert body["version"] == "1.2.3"

    def test_refuses_non_loopback_bind_without_token(self, deps: Deps) -> None:
        deps.token = ""
        assert start(deps, host="0.0.0.0", port=8080) is None


class TestState:
    def test_state_reports_pools_and_containers(
        self, client: FlaskClient, store: Store, pool: PoolConfig
    ) -> None:
        store.record_tick(
            TickSnapshot(
                pool=pool.name,
                display=pool.display,
                containers=2,
                online=2,
                idle=1,
                busy=1,
                queued=4,
                duration=0.3,
            )
        )
        body = _json(client.get("/api/state"))

        assert body["pools"][0]["config"]["name"] == pool.name
        assert body["pools"][0]["queued"] == 4
        assert body["containers"][0]["name"] == "gh-runner-test-pool-abc123"
        assert body["globals"]["max_total_runners"] == 10

    def test_state_never_leaks_the_pat(self, client: FlaskClient, pool: PoolConfig) -> None:
        assert pool.pat not in client.get("/api/state").get_data(as_text=True)

    def test_config_never_leaks_the_pat(self, client: FlaskClient, pool: PoolConfig) -> None:
        assert pool.pat not in client.get("/api/config").get_data(as_text=True)

    def test_export_never_leaks_the_pat(self, client: FlaskClient, pool: PoolConfig) -> None:
        exported = client.get("/api/config/export").get_data(as_text=True)
        assert pool.pat not in exported
        assert "${GITHUB_PAT}" in exported


class TestContainerControl:
    def test_stop_idle_runner(self, client: FlaskClient, docker: MagicMock) -> None:
        response = client.post("/api/containers/gh-runner-test-pool-abc123/stop", json={})
        assert response.status_code == 200
        docker.stop_container.assert_called_once_with("gh-runner-test-pool-abc123")

    def test_stop_busy_runner_requires_confirmation(
        self, client: FlaskClient, store: Store, docker: MagicMock
    ) -> None:
        store.replace_runner_status("test-pool", [("gh-runner-test-pool-abc123", "online", True)])
        response = client.post("/api/containers/gh-runner-test-pool-abc123/stop", json={})

        assert response.status_code == 409
        docker.stop_container.assert_not_called()

    def test_confirmed_stop_of_busy_runner_is_audited(
        self, client: FlaskClient, store: Store, docker: MagicMock
    ) -> None:
        store.replace_runner_status("test-pool", [("gh-runner-test-pool-abc123", "online", True)])
        response = client.post(
            "/api/containers/gh-runner-test-pool-abc123/stop",
            json={"confirm": True, "reason": "wedged"},
        )

        assert response.status_code == 200
        docker.stop_container.assert_called_once()
        assert "wedged" in store.recent_audit()[0]["detail"]

    def test_refuses_containers_outside_the_runner_namespace(
        self, client: FlaskClient, docker: MagicMock
    ) -> None:
        for name in ("postgres", "gh-runner-orchestrator"):
            assert client.post(f"/api/containers/{name}/stop", json={}).status_code == 400
        docker.stop_container.assert_not_called()

    def test_protect_toggles_the_flag(self, client: FlaskClient, store: Store) -> None:
        client.post("/api/containers/gh-runner-test-pool-abc123/protect", json={"protected": True})
        assert "gh-runner-test-pool-abc123" in store.protected_containers()

    def test_logs_are_returned_as_text(self, client: FlaskClient) -> None:
        response = client.get("/api/containers/gh-runner-test-pool-abc123/logs")
        assert response.status_code == 200
        assert "runner log line" in response.get_data(as_text=True)


class TestIdempotency:
    def test_repeated_key_does_not_spawn_twice(
        self, client: FlaskClient, docker: MagicMock
    ) -> None:
        headers = {"Idempotency-Key": "retry-1"}
        first = client.post("/api/pools/test-pool/scale", json={"delta": 1}, headers=headers)
        second = client.post("/api/pools/test-pool/scale", json={"delta": 1}, headers=headers)

        assert first.status_code == 200
        assert second.headers.get("Idempotency-Replayed") == "true"
        docker.spawn_runner.assert_called_once()

    def test_different_keys_both_execute(self, client: FlaskClient, docker: MagicMock) -> None:
        client.post(
            "/api/pools/test-pool/scale", json={"delta": 1}, headers={"Idempotency-Key": "a"}
        )
        client.post(
            "/api/pools/test-pool/scale", json={"delta": 1}, headers={"Idempotency-Key": "b"}
        )
        assert docker.spawn_runner.call_count == 2


class TestPoolControl:
    def test_pause_and_resume(self, client: FlaskClient, store: Store) -> None:
        client.post("/api/pools/test-pool/state", json={"paused": True})
        assert store.pool_states()["test-pool"].paused is True

        client.post("/api/pools/test-pool/state", json={"paused": False})
        assert store.pool_states()["test-pool"].paused is False

    def test_drain_preserves_pause_flag(self, client: FlaskClient, store: Store) -> None:
        client.post("/api/pools/test-pool/state", json={"paused": True})
        client.post("/api/pools/test-pool/state", json={"draining": True})
        state = store.pool_states()["test-pool"]

        assert state.paused is True
        assert state.draining is True

    def test_unknown_pool_is_404(self, client: FlaskClient) -> None:
        assert client.post("/api/pools/nope/state", json={"paused": True}).status_code == 404

    def test_scale_down_stops_an_idle_runner(self, client: FlaskClient, docker: MagicMock) -> None:
        response = client.post("/api/pools/test-pool/scale", json={"delta": -1})
        assert response.status_code == 200
        docker.stop_container.assert_called_once_with("gh-runner-test-pool-abc123")

    def test_scale_down_refuses_when_every_runner_is_busy(
        self, client: FlaskClient, store: Store, docker: MagicMock
    ) -> None:
        store.replace_runner_status("test-pool", [("gh-runner-test-pool-abc123", "online", True)])
        response = client.post("/api/pools/test-pool/scale", json={"delta": -1})

        assert response.status_code == 409
        docker.stop_container.assert_not_called()

    def test_scale_rejects_arbitrary_delta(self, client: FlaskClient) -> None:
        assert client.post("/api/pools/test-pool/scale", json={"delta": 50}).status_code == 400

    def test_global_pause(self, client: FlaskClient, deps: Deps) -> None:
        client.post("/api/pause", json={"paused": True})
        assert deps.resolver.resolve().paused is True


class TestConfigEditing:
    def test_patch_applies_override(self, client: FlaskClient, deps: Deps) -> None:
        response = client.patch("/api/config/pools/test-pool", json={"max_runners": 9})
        assert response.status_code == 200
        assert deps.resolver.resolve().pools[0].max_runners == 9

    def test_patch_rejects_invalid_value(self, client: FlaskClient, deps: Deps) -> None:
        response = client.patch("/api/config/pools/test-pool", json={"repo_check_workers": 99})
        assert response.status_code == 400
        # The tick is never reached with a bad value.
        assert deps.resolver.resolve().pools[0].repo_check_workers == 6

    def test_patch_rejects_secret_fields(self, client: FlaskClient) -> None:
        response = client.patch("/api/config/pools/test-pool", json={"pat": "ghp_evil"})
        assert response.status_code == 400
        assert "not editable" in _json(response)["error"]

    def test_reset_reverts_to_yaml(self, client: FlaskClient, deps: Deps, pool: PoolConfig) -> None:
        client.patch("/api/config/pools/test-pool", json={"max_runners": 9})
        client.delete("/api/config/pools/test-pool/overrides")
        assert deps.resolver.resolve().pools[0].max_runners == pool.max_runners

    def test_patch_unknown_pool_is_404(self, client: FlaskClient) -> None:
        assert client.patch("/api/config/pools/nope", json={"max_runners": 1}).status_code == 404

    def test_create_pool_from_api(
        self, client: FlaskClient, deps: Deps, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTHER_PAT", "ghp_other_token_123456")
        response = client.post(
            "/api/config/pools",
            json={"name": "extra", "owner": "acme", "repo": "widgets", "pat_env": "OTHER_PAT"},
        )
        assert response.status_code == 201
        assert [p.name for p in deps.resolver.resolve().pools] == ["test-pool", "extra"]

    def test_create_pool_rejects_duplicate_name(self, client: FlaskClient) -> None:
        response = client.post("/api/config/pools", json={"name": "test-pool", "owner": "acme"})
        assert response.status_code == 409

    def test_remove_yaml_pool_disables_it(self, client: FlaskClient, deps: Deps) -> None:
        response = client.delete("/api/config/pools/test-pool")
        assert response.status_code == 200
        assert deps.resolver.resolve().pools == []

    def test_globals_patch(self, client: FlaskClient, deps: Deps) -> None:
        client.patch("/api/config/globals", json={"max_total_runners": 4})
        assert deps.resolver.resolve().max_total_runners == 4

    def test_globals_reject_negative(self, client: FlaskClient) -> None:
        response = client.patch("/api/config/globals", json={"max_total_runners": -1})
        assert response.status_code == 400


class TestMetrics:
    def test_prometheus_exposition(
        self, client: FlaskClient, store: Store, pool: PoolConfig
    ) -> None:
        store.record_tick(
            TickSnapshot(
                pool=pool.name,
                display=pool.display,
                containers=2,
                online=2,
                idle=1,
                busy=1,
                queued=3,
                duration=0.3,
            )
        )
        body = client.get("/metrics").get_data(as_text=True)

        assert 'rorch_pool_containers{pool="test-pool"} 2' in body
        assert 'rorch_pool_queued{pool="test-pool"} 3' in body
        assert "rorch_max_total_runners 10" in body
        assert body.endswith("\n")

    def test_metrics_requires_auth(self, deps: Deps) -> None:
        deps.token = "s3cret"
        assert create_app(deps).test_client().get("/metrics").status_code == 401


class TestDashboardEscaping:
    """The dashboard renders GitHub-supplied text into innerHTML.

    Workflow and job names are chosen by anyone who can open a PR on a watched
    repository, and the page carries the auth cookie for an API that starts
    root-privileged containers — so an unescaped value here is a privilege
    escalation, not a cosmetic bug.
    """

    _SOURCE = Path(__file__).resolve().parents[1] / "rorch" / "dashboard.html"
    # Fields that arrive from GitHub, Docker or the store rather than the code.
    _UNTRUSTED = (
        "workflow",
        "job_name",
        "repo",
        "runner",
        "conclusion",
        "reason",
        "image",
        "status",
        "container",
        "event",
        "display",
        "name",
    )

    def test_every_untrusted_field_is_escaped(self) -> None:
        source = self._SOURCE.read_text(encoding="utf-8")
        bare = [
            f"${{{obj}.{field}"
            for obj in ("j", "c", "e", "r", "p")
            for field in self._UNTRUSTED
            if f"${{{obj}.{field}" in source
        ]
        assert bare == [], f"unescaped interpolation in dashboard.html: {bare}"

    def test_escape_helper_covers_the_dangerous_characters(self) -> None:
        source = self._SOURCE.read_text(encoding="utf-8")
        for char in ("&", "<", ">", '"', "'"):
            assert f"'{char}'" in source or f'"{char}"' in source, char
        assert "const esc =" in source

    def test_job_links_are_restricted_to_https(self) -> None:
        """A javascript: or data: href would execute on click."""
        source = self._SOURCE.read_text(encoding="utf-8")
        assert "safeUrl" in source
        assert 'href="${esc(href)}"' in source


class TestScopedTokens:
    """A read-only token must see everything and change nothing."""

    @pytest.fixture
    def scoped(self, deps: Deps) -> FlaskClient:
        deps.token = "control-token"
        deps.readonly_token = "view-token"
        return create_app(deps).test_client()

    def _read(self, client: FlaskClient, token: str) -> int:
        return client.get("/api/state", headers={"Authorization": f"Bearer {token}"}).status_code

    def test_readonly_token_can_read(self, scoped: FlaskClient) -> None:
        assert self._read(scoped, "view-token") == 200

    def test_control_token_can_read(self, scoped: FlaskClient) -> None:
        assert self._read(scoped, "control-token") == 200

    def test_readonly_token_cannot_stop_a_runner(
        self, scoped: FlaskClient, docker: MagicMock
    ) -> None:
        response = scoped.post(
            "/api/containers/gh-runner-test-pool-abc123/stop",
            json={},
            headers={"Authorization": "Bearer view-token"},
        )
        assert response.status_code == 403
        docker.stop_container.assert_not_called()

    def test_readonly_token_cannot_edit_config(self, scoped: FlaskClient, deps: Deps) -> None:
        response = scoped.patch(
            "/api/config/pools/test-pool",
            json={"max_runners": 99},
            headers={"Authorization": "Bearer view-token"},
        )
        assert response.status_code == 403
        assert deps.resolver.resolve().pools[0].max_runners == 5

    def test_readonly_token_cannot_pause(self, scoped: FlaskClient, deps: Deps) -> None:
        response = scoped.post(
            "/api/pause", json={"paused": True}, headers={"Authorization": "Bearer view-token"}
        )
        assert response.status_code == 403
        assert deps.resolver.resolve().paused is False

    def test_control_token_still_works_for_writes(
        self, scoped: FlaskClient, docker: MagicMock
    ) -> None:
        response = scoped.post(
            "/api/containers/gh-runner-test-pool-abc123/stop",
            json={},
            headers={"Authorization": "Bearer control-token"},
        )
        assert response.status_code == 200
        docker.stop_container.assert_called_once()


class TestBruteForceGuard:
    def test_locks_out_after_repeated_bad_tokens(self, deps: Deps) -> None:
        deps.token = "s3cret"
        client = create_app(deps).test_client()

        for _ in range(MAX_TOKEN_FAILURES):
            assert client.get("/api/state?token=wrong").status_code == 401

        locked = client.get("/api/state?token=wrong")
        assert locked.status_code == 429
        assert int(locked.headers["Retry-After"]) > 0

    def test_lockout_also_blocks_the_correct_token(self, deps: Deps) -> None:
        """Otherwise an attacker's guessing would not slow them down at all."""
        deps.token = "s3cret"
        client = create_app(deps).test_client()
        for _ in range(MAX_TOKEN_FAILURES):
            client.get("/api/state?token=wrong")

        assert client.get("/api/state?token=s3cret").status_code == 429

    def test_success_resets_the_counter(self, deps: Deps) -> None:
        deps.token = "s3cret"
        client = create_app(deps).test_client()

        for _ in range(MAX_TOKEN_FAILURES - 1):
            client.get("/api/state?token=wrong")
        assert client.get("/api/state?token=s3cret").status_code == 200

        # Counter cleared, so the budget starts over rather than tripping now.
        assert client.get("/api/state?token=wrong").status_code == 401

    def test_lockout_expires(self, deps: Deps) -> None:
        deps.token = "s3cret"
        deps.guard = TokenGuard(max_failures=1, lockout_seconds=0)
        client = create_app(deps).test_client()

        assert client.get("/api/state?token=wrong").status_code == 401
        assert client.get("/api/state?token=s3cret").status_code == 200


class TestTokenPersistence:
    def test_generated_token_is_reused_after_restart(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("RORCH_API_TOKEN", raising=False)
        monkeypatch.setenv("RORCH_API_HOST", "0.0.0.0")

        first = resolve_token(store)
        second = resolve_token(store)

        assert first and first == second

    def test_env_token_overrides_the_stored_one(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RORCH_API_HOST", "0.0.0.0")
        stored = resolve_token(store)
        monkeypatch.setenv("RORCH_API_TOKEN", "from-env")

        assert resolve_token(store) == "from-env" != stored

    def test_loopback_bind_needs_no_token(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("RORCH_API_TOKEN", raising=False)
        monkeypatch.setenv("RORCH_API_HOST", "127.0.0.1")

        assert resolve_token(store) == ""

    def test_stored_token_is_never_exposed_by_the_api(
        self, deps: Deps, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("RORCH_API_TOKEN", raising=False)
        monkeypatch.setenv("RORCH_API_HOST", "0.0.0.0")
        token = resolve_token(store)
        deps.token = token
        client = create_app(deps).test_client()
        headers = {"Authorization": f"Bearer {token}"}

        for path in ("/api/state", "/api/config", "/api/config/export", "/metrics"):
            assert token not in client.get(path, headers=headers).get_data(as_text=True), path


def _get(url: str, timeout: float = 5.0) -> tuple[int, dict[str, str], dict[str, Any]]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, dict(response.headers), json.loads(response.read())


def _post(url: str, body: dict[str, Any], headers: dict[str, str]) -> int:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


@pytest.fixture
def served(deps: Deps) -> Iterator[tuple[ApiServer, str]]:
    """The real production server on an ephemeral loopback port."""
    server = start(deps, host="127.0.0.1", port=0)
    assert server is not None
    yield server, f"http://127.0.0.1:{server.port}"
    server.stop()


class TestProductionServer:
    def test_serves_health_through_waitress(self, served: tuple[ApiServer, str]) -> None:
        _, base = served
        status, headers, body = _get(f"{base}/api/health")

        assert status == 200
        assert body["status"] == "ok"
        # Not Werkzeug's development server.
        assert headers["Server"] == "rorch"

    def test_handles_requests_concurrently(
        self, served: tuple[ApiServer, str], docker: MagicMock
    ) -> None:
        _, base = served
        parties = 4
        barrier = threading.Barrier(parties, timeout=5)
        listing = docker.container_details.return_value

        def slow_listing(_prefix: str) -> list[ContainerInfo]:
            # Every request blocks here until all of them have arrived, so this
            # only passes if the server runs `parties` handlers at once.
            barrier.wait()
            return listing

        docker.container_details.side_effect = slow_listing
        with ThreadPoolExecutor(max_workers=parties) as pool:
            results = list(pool.map(lambda _: _get(f"{base}/api/state"), range(parties)))

        assert [status for status, _, _ in results] == [200] * parties
        assert all(body["containers"] for _, _, body in results)

    def test_stop_closes_idle_keep_alive_connections(self, served: tuple[ApiServer, str]) -> None:
        server, _ = served
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        connection.request("GET", "/api/health")
        assert connection.getresponse().read()  # leaves the connection open

        started = time.monotonic()
        assert server.stop() is True
        assert time.monotonic() - started < 3
        assert not server.thread.is_alive()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", server.port), timeout=1).close()
        connection.close()

    def test_stop_lets_an_in_flight_request_finish(
        self, served: tuple[ApiServer, str], docker: MagicMock
    ) -> None:
        server, base = served
        entered = threading.Event()

        def slow_logs(_name: str, tail: int = 200) -> str:
            entered.set()
            time.sleep(0.3)
            return "done"

        docker.container_logs.side_effect = slow_logs
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                urllib.request.urlopen, f"{base}/api/containers/gh-runner-test-pool-abc/logs"
            )
            assert entered.wait(5)
            assert server.stop() is True
            assert pending.result(timeout=5).read() == b"done"

    def test_concurrent_retry_with_the_same_key_spawns_once(
        self, served: tuple[ApiServer, str], docker: MagicMock
    ) -> None:
        _, base = served
        entered, release = threading.Event(), threading.Event()

        def slow_spawn(_pool: PoolConfig) -> bool:
            entered.set()
            assert release.wait(5)
            return True

        docker.spawn_runner.side_effect = slow_spawn
        url = f"{base}/api/pools/test-pool/scale"
        headers = {"Idempotency-Key": "timeout-retry"}
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(_post, url, {"delta": 1}, headers)
            assert entered.wait(5)
            # A client that timed out and retried while the first is still running.
            assert _post(url, {"delta": 1}, headers) == 409
            release.set()
            assert first.result(timeout=5) == 200

        assert _post(url, {"delta": 1}, headers) == 200  # replayed now
        docker.spawn_runner.assert_called_once()

    def test_concurrent_config_edits_both_persist(
        self, served: tuple[ApiServer, str], store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, base = served
        read_overrides = store.pool_overrides

        def slow_read() -> dict[str, dict[str, Any]]:
            # Widen the read-then-write window so unserialised edits would lose one.
            rows = read_overrides()
            time.sleep(0.05)
            return rows

        monkeypatch.setattr(store, "pool_overrides", slow_read)
        url = f"{base}/api/config/pools/test-pool"

        def patch(body: dict[str, Any]) -> int:
            request = urllib.request.Request(
                url,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(patch, [{"max_runners": 4}, {"min_idle": 0}]))

        assert statuses == [200, 200]
        assert read_overrides()["test-pool"]["data"] == {"max_runners": 4, "min_idle": 0}


class TestRequestLogging:
    def test_successful_polls_stay_out_of_info_logs(
        self, client: FlaskClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="rorch.server.app"):
            client.get("/api/state")

        [record] = [r for r in caplog.records if r.name == "rorch.server.app"]
        assert record.levelno == logging.DEBUG
        assert "GET /api/state 200" in record.getMessage()

    def test_client_errors_are_visible_at_info(
        self, deps: Deps, caplog: pytest.LogCaptureFixture
    ) -> None:
        deps.token = "s3cret"
        with caplog.at_level(logging.INFO, logger="rorch.server.app"):
            create_app(deps).test_client().get("/api/state")

        assert any(
            r.levelno == logging.INFO and "GET /api/state 401" in r.getMessage()
            for r in caplog.records
        )

    def test_server_errors_are_warnings(
        self, deps: Deps, docker: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        docker.container_details.side_effect = RuntimeError("docker down")
        client = create_app(deps).test_client()  # not TESTING: errors become 500s
        with caplog.at_level(logging.INFO, logger="rorch.server.app"):
            assert client.get("/api/state").status_code == 500

        assert any(
            r.levelno == logging.WARNING and "GET /api/state 500" in r.getMessage()
            for r in caplog.records
        )
