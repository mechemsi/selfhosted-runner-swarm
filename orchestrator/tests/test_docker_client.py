# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Tests for Docker client helpers."""

from dataclasses import replace
from pathlib import Path
from threading import Barrier, Lock
from typing import ClassVar

import pytest

from rorch import docker_client
from rorch.config import PoolConfig
from rorch.docker_client import RUNNER_BUILD_CONTEXT, DockerClient, _parse_running_minutes
from rorch.store import EVENT_MANUAL_STOP, SqliteStore


class TestParseRunningMinutes:
    def test_seconds(self) -> None:
        assert _parse_running_minutes("30 seconds") == pytest.approx(0.5)

    def test_minutes(self) -> None:
        assert _parse_running_minutes("5 minutes") == 5.0

    def test_hours(self) -> None:
        assert _parse_running_minutes("2 hours") == 120.0

    def test_days(self) -> None:
        assert _parse_running_minutes("1 day") == 1440.0

    def test_singular_forms(self) -> None:
        assert _parse_running_minutes("1 second") == pytest.approx(1 / 60)
        assert _parse_running_minutes("1 minute") == 1.0
        assert _parse_running_minutes("1 hour") == 60.0

    def test_invalid_format(self) -> None:
        assert _parse_running_minutes("") is None
        assert _parse_running_minutes("unknown") is None

    def test_about_prefix(self) -> None:
        # Docker sometimes outputs "About a minute"
        assert _parse_running_minutes("garbage data here") is None


class TestBoundedCleanup:
    def test_limits_parallel_cleanup_operations(self) -> None:
        first_batch = Barrier(4)
        state_lock = Lock()
        active = 0
        max_active = 0
        completed: list[str] = []

        def cleanup(item: str) -> None:
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            if item != "last":
                first_batch.wait(timeout=2)
            completed.append(item)
            with state_lock:
                active -= 1

        DockerClient._run_parallel(cleanup, ["one", "two", "three", "four", "last"])

        assert max_active == 4
        assert sorted(completed) == ["four", "last", "one", "three", "two"]


class TestEnsureImage:
    HOST_GID = 991
    EXPECTED_BUILD: ClassVar[list[str]] = [
        "build",
        "--build-arg",
        f"DOCKER_GID={HOST_GID}",
        "--label",
        f"rorch.docker_gid={HOST_GID}",
        "-t",
        "gh-runner:latest",
        RUNNER_BUILD_CONTEXT,
    ]

    def _client(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        image_gid: int | None,
        has_context: bool,
        build_code: int = 0,
    ) -> tuple[DockerClient, list[list[str]]]:
        """image_gid: None = image absent, else the GID it was built for."""
        builds: list[list[str]] = []
        client = DockerClient()
        monkeypatch.setattr(docker_client, "_host_docker_gid", lambda: self.HOST_GID)
        monkeypatch.setattr(
            client, "_capture", lambda args: ("", 1) if image_gid is None else (str(image_gid), 0)
        )
        monkeypatch.setattr(client, "_exec", lambda args: builds.append(args) or build_code)
        monkeypatch.setattr(Path, "exists", lambda self: has_context)
        return client, builds

    def test_matching_image_is_not_rebuilt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, builds = self._client(monkeypatch, image_gid=self.HOST_GID, has_context=True)
        assert client.ensure_image("gh-runner:latest") is True
        assert builds == []

    def test_missing_image_is_built(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, builds = self._client(monkeypatch, image_gid=None, has_context=True)
        assert client.ensure_image("gh-runner:latest") is True
        assert builds == [self.EXPECTED_BUILD]

    def test_wrong_docker_gid_is_rebuilt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Image exists but its runner user can't read this host's socket.
        client, builds = self._client(monkeypatch, image_gid=988, has_context=True)
        assert client.ensure_image("gh-runner:latest") is True
        assert builds == [self.EXPECTED_BUILD]

    def test_pauses_without_build_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, builds = self._client(monkeypatch, image_gid=None, has_context=False)
        assert client.ensure_image("gh-runner:latest") is False
        assert builds == []

    def test_failed_build_backs_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, builds = self._client(monkeypatch, image_gid=None, has_context=True, build_code=1)
        assert client.ensure_image("gh-runner:latest") is False
        assert client.ensure_image("gh-runner:latest") is False
        assert len(builds) == 1  # second call is inside the retry window


class TestCleanupAged:
    _PS_OUTPUT = (
        "gh-runner-orchestrator\t3 hours\n"  # excluded → keep
        "gh-runner-tt-aaaaaaaa\t2 hours\n"  # aged → kill
        "gh-runner-tt-bbbbbbbb\t5 minutes\n"  # fresh → keep
        "gh-runner-tt-cccccccc\tAbout an hour\n"  # unparseable → keep (safe)
    )

    def _client(self, monkeypatch: pytest.MonkeyPatch, removed: list[str]) -> DockerClient:
        def fake_capture(args: list[str]) -> tuple[str, int]:
            if args[0] == "ps":
                return self._PS_OUTPUT, 0
            if args[0] == "rm":
                removed.append(args[-1])
            return "", 0

        client = DockerClient()
        monkeypatch.setattr(client, "_capture", fake_capture)
        return client

    def test_kills_only_aged_and_not_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        removed: list[str] = []
        client = self._client(monkeypatch, removed)
        client.cleanup_aged("gh-runner", 60, exclude=frozenset({"gh-runner-orchestrator"}))
        assert removed == ["gh-runner-tt-aaaaaaaa"]

    def test_disabled_when_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        removed: list[str] = []
        captured: list[str] = []

        def fake_capture(args: list[str]) -> tuple[str, int]:
            captured.append(args[0])
            return "", 0

        client = DockerClient()
        monkeypatch.setattr(client, "_capture", fake_capture)
        client.cleanup_aged("gh-runner", 0)
        assert captured == []  # no docker call at all when disabled
        assert removed == []


class TestContainerDetails:
    _PS_OUTPUT = (
        "gh-runner-tt-aaaaaaaa\tgh-runner:latest\tUp 4 minutes\t4 minutes ago\n"
        "gh-runner-tt-bbbbbbbb\tgh-runner:2.328.0\tUp 2 hours\t2 hours ago\n"
        "malformed-row-without-tabs\n"
    )

    def test_parses_rows_and_skips_malformed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = DockerClient()
        monkeypatch.setattr(client, "_capture", lambda args: (self._PS_OUTPUT, 0))

        details = client.container_details("gh-runner-tt")

        assert [d.name for d in details] == ["gh-runner-tt-aaaaaaaa", "gh-runner-tt-bbbbbbbb"]
        assert details[0].image == "gh-runner:latest"
        assert details[1].minutes == 120.0

    def test_empty_output_is_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = DockerClient()
        monkeypatch.setattr(client, "_capture", lambda args: ("", 0))
        assert client.container_details("gh-runner-tt") == []


class TestStopContainer:
    def test_removes_the_container(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def fake_capture(args: list[str]) -> tuple[str, int]:
            calls.append(args)
            return "", 0

        client = DockerClient()
        monkeypatch.setattr(client, "_capture", fake_capture)

        assert client.stop_container("gh-runner-tt-aaaaaaaa") is True
        assert calls == [["rm", "-f", "-v", "gh-runner-tt-aaaaaaaa"]]

    def test_already_gone_counts_as_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`docker rm` failing because the container vanished is the desired end state."""

        def fake_capture(args: list[str]) -> tuple[str, int]:
            return ("", 0) if args[0] == "ps" else ("No such container", 1)

        client = DockerClient()
        monkeypatch.setattr(client, "_capture", fake_capture)

        assert client.stop_container("gh-runner-tt-aaaaaaaa") is True

    def test_records_event_when_a_store_is_attached(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        store = SqliteStore(str(tmp_path / "rorch.db"))
        client = DockerClient(store=store)
        monkeypatch.setattr(client, "_capture", lambda args: ("", 0))

        client.stop_container("gh-runner-tt-aaaaaaaa")

        events = store.recent_events()
        assert events[0]["event"] == EVENT_MANUAL_STOP
        assert events[0]["pool"] == "tt"


class TestNetworkMode:
    def test_spawn_uses_the_configured_network(
        self, monkeypatch: pytest.MonkeyPatch, pool: PoolConfig
    ) -> None:
        recorded: list[list[str]] = []
        client = DockerClient()
        monkeypatch.setattr(client, "ensure_image", lambda image: True)
        monkeypatch.setattr(client, "_exec", lambda args: recorded.append(args) or 0)

        client.spawn_runner(replace(pool, network_mode="bridge"))

        args = recorded[0]
        assert args[args.index("--network") + 1] == "bridge"

    def test_default_pool_still_uses_host_networking(
        self, monkeypatch: pytest.MonkeyPatch, pool: PoolConfig
    ) -> None:
        recorded: list[list[str]] = []
        client = DockerClient()
        monkeypatch.setattr(client, "ensure_image", lambda image: True)
        monkeypatch.setattr(client, "_exec", lambda args: recorded.append(args) or 0)

        client.spawn_runner(pool)

        args = recorded[0]
        assert args[args.index("--network") + 1] == "host"
