# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Tests for the dashboard API: auth, control actions, config editing, metrics."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from flask.testing import FlaskClient

from rorch.config import PoolConfig
from rorch.protocols import ContainerInfo
from rorch.resolver import ConfigResolver
from rorch.server import Deps, create_app, start
from rorch.store import SqliteStore


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(str(tmp_path / "rorch.db"))


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
def deps(pool: PoolConfig, store: SqliteStore, docker: MagicMock) -> Deps:
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

    def test_refuses_non_loopback_bind_without_token(self, deps: Deps) -> None:
        deps.token = ""
        assert start(deps, host="0.0.0.0", port=8080) is None


class TestState:
    def test_state_reports_pools_and_containers(
        self, client: FlaskClient, store: SqliteStore, pool: PoolConfig
    ) -> None:
        store.record_tick(pool.name, pool.display, 2, 2, 1, 1, 4, 0.3)
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
        self, client: FlaskClient, store: SqliteStore, docker: MagicMock
    ) -> None:
        store.replace_runner_status("test-pool", [("gh-runner-test-pool-abc123", "online", True)])
        response = client.post("/api/containers/gh-runner-test-pool-abc123/stop", json={})

        assert response.status_code == 409
        docker.stop_container.assert_not_called()

    def test_confirmed_stop_of_busy_runner_is_audited(
        self, client: FlaskClient, store: SqliteStore, docker: MagicMock
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

    def test_protect_toggles_the_flag(self, client: FlaskClient, store: SqliteStore) -> None:
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
    def test_pause_and_resume(self, client: FlaskClient, store: SqliteStore) -> None:
        client.post("/api/pools/test-pool/state", json={"paused": True})
        assert store.pool_states()["test-pool"].paused is True

        client.post("/api/pools/test-pool/state", json={"paused": False})
        assert store.pool_states()["test-pool"].paused is False

    def test_drain_preserves_pause_flag(self, client: FlaskClient, store: SqliteStore) -> None:
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
        self, client: FlaskClient, store: SqliteStore, docker: MagicMock
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
        self, client: FlaskClient, store: SqliteStore, pool: PoolConfig
    ) -> None:
        store.record_tick(pool.name, pool.display, 2, 2, 1, 1, 3, 0.3)
        body = client.get("/metrics").get_data(as_text=True)

        assert 'rorch_pool_containers{pool="test-pool"} 2' in body
        assert 'rorch_pool_queued{pool="test-pool"} 3' in body
        assert "rorch_max_total_runners 10" in body
        assert body.endswith("\n")

    def test_metrics_requires_auth(self, deps: Deps) -> None:
        deps.token = "s3cret"
        assert create_app(deps).test_client().get("/metrics").status_code == 401
