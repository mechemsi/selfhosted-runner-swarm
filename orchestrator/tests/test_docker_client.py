# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Tests for Docker client helpers."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Lock
from typing import ClassVar

import pytest

from rorch import docker_client
from rorch.config import PoolConfig
from rorch.docker_client import (
    JOB_STARTED_HOOK_PATH,
    RUNNER_BUILD_CONTEXT,
    RUNNER_WORK_DIR,
    DockerClient,
    _parse_running_minutes,
    _pinned_runner_version,
)
from rorch.store import EVENT_MANUAL_STOP, Store
from rorch.version import expand_image


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
    EXPECTED_BUILD: ClassVar[list[str]] = ["build", "-t", "gh-runner:latest", RUNNER_BUILD_CONTEXT]
    PUBLISHED = "ghcr.io/mechemsi/rorch-runner:1.1.0"

    def _client(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        image_gid: int | str | None,
        has_context: bool,
        build_code: int = 0,
        pulled_gid: int | str | None = None,
    ) -> tuple[DockerClient, list[list[str]]]:
        """image_gid: None = image absent, else its rorch.docker_gid label.

        pulled_gid: the label the image has once a `docker pull` succeeded.
        """
        builds: list[list[str]] = []
        label = {"value": image_gid}
        client = DockerClient()
        monkeypatch.setattr(docker_client, "_host_docker_gid", lambda: self.HOST_GID)
        monkeypatch.setattr(
            client,
            "_capture",
            lambda args: ("", 1) if label["value"] is None else (str(label["value"]), 0),
        )

        def run(args: list[str]) -> int:
            builds.append(args)
            if args[0] == "pull" and build_code == 0:
                label["value"] = pulled_gid
            return build_code

        monkeypatch.setattr(client, "_exec", run)
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

    def test_runtime_gid_image_is_used_as_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Published images join the socket's group at start, on any host.
        client, builds = self._client(monkeypatch, image_gid="runtime", has_context=True)
        assert client.ensure_image(self.PUBLISHED) is True
        assert client.ensure_image("gh-runner:latest") is True
        assert builds == []

    def test_missing_registry_image_is_pulled_not_built(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, builds = self._client(
            monkeypatch, image_gid=None, has_context=True, pulled_gid="runtime"
        )
        assert client.ensure_image(self.PUBLISHED) is True
        assert builds == [["pull", self.PUBLISHED]]

    def test_failed_pull_backs_off_and_never_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, builds = self._client(monkeypatch, image_gid=None, has_context=True, build_code=1)
        assert client.ensure_image(self.PUBLISHED) is False
        assert client.ensure_image(self.PUBLISHED) is False
        assert builds == [["pull", self.PUBLISHED]]  # second call is inside the retry window

    def test_pulled_image_with_a_foreign_gid_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = self._client(monkeypatch, image_gid=None, has_context=True, pulled_gid=988)
        assert client.ensure_image(self.PUBLISHED) is False

    def test_version_placeholder_is_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(docker_client, "expand_image", lambda i: expand_image(i, "1.1.0"))
        client, builds = self._client(
            monkeypatch, image_gid=None, has_context=True, pulled_gid="runtime"
        )
        assert client.ensure_image("ghcr.io/mechemsi/rorch-runner:{version}") is True
        assert builds == [["pull", self.PUBLISHED]]

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
        store = Store(str(tmp_path / "rorch.db"))
        client = DockerClient(store=store)
        monkeypatch.setattr(client, "_capture", lambda args: ("", 0))

        client.stop_container("gh-runner-tt-aaaaaaaa")

        events = store.recent_events()
        assert events[0]["event"] == EVENT_MANUAL_STOP
        assert events[0]["pool"] == "tt"


class TestJobStartedHook:
    def _spawn_args(self, monkeypatch: pytest.MonkeyPatch, pool: PoolConfig) -> list[str]:
        recorded: list[list[str]] = []
        client = DockerClient()
        monkeypatch.setattr(client, "ensure_image", lambda image: True)
        monkeypatch.setattr(client, "_exec", lambda args: recorded.append(args) or 0)
        client.spawn_runner(pool)
        return recorded[0]

    def test_hook_is_mounted_read_only_and_announced_to_the_runner(
        self, monkeypatch: pytest.MonkeyPatch, pool: PoolConfig
    ) -> None:
        args = self._spawn_args(monkeypatch, replace(pool, job_started_hook="/opt/hooks/d.sh"))

        mount = args[args.index("--mount") + 1]
        assert mount == f"type=bind,src=/opt/hooks/d.sh,dst={JOB_STARTED_HOOK_PATH},readonly"
        assert f"ACTIONS_RUNNER_HOOK_JOB_STARTED={JOB_STARTED_HOOK_PATH}" in args
        assert args.index("--mount") < len(args) - 1  # before the image argument

    def test_no_hook_means_no_mount_and_no_env(
        self, monkeypatch: pytest.MonkeyPatch, pool: PoolConfig
    ) -> None:
        args = self._spawn_args(monkeypatch, pool)

        assert "--mount" not in args
        assert not any(a.startswith("ACTIONS_RUNNER_HOOK_JOB_STARTED") for a in args)


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


class TestPinnedRunnerVersion:
    """A pool pinned to gh-runner:2.328.0 must not be auto-built as some other agent."""

    def test_dotted_numeric_tag_is_a_version(self) -> None:
        assert _pinned_runner_version("gh-runner:2.328.0") == "2.328.0"
        assert _pinned_runner_version("gh-runner:2.335") == "2.335"

    def test_non_version_tags_yield_nothing(self) -> None:
        for image in ("gh-runner:latest", "gh-runner", "gh-runner:dev", "custom/img:php8"):
            assert _pinned_runner_version(image) == ""

    def test_build_passes_the_pinned_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[list[str]] = []
        client = DockerClient()
        monkeypatch.setattr(docker_client, "_host_docker_gid", lambda: 988)
        monkeypatch.setattr(client, "_image_state", lambda image, gid: "missing")
        monkeypatch.setattr(docker_client.Path, "exists", lambda self: True)
        monkeypatch.setattr(client, "_exec", lambda args: recorded.append(args) or 0)

        client.ensure_image("gh-runner:2.328.0")

        args = recorded[0]
        assert "RUNNER_VERSION=2.328.0" in args
        assert args[args.index("-t") + 1] == "gh-runner:2.328.0"

    def test_latest_build_leaves_the_version_to_the_dockerfile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: list[list[str]] = []
        client = DockerClient()
        monkeypatch.setattr(docker_client, "_host_docker_gid", lambda: 988)
        monkeypatch.setattr(client, "_image_state", lambda image, gid: "missing")
        monkeypatch.setattr(docker_client.Path, "exists", lambda self: True)
        monkeypatch.setattr(client, "_exec", lambda args: recorded.append(args) or 0)

        client.ensure_image("gh-runner:latest")

        assert not any(a.startswith("RUNNER_VERSION=") for a in recorded[0])


class TestWorkTmpfs:
    def _spawn_args(self, monkeypatch: pytest.MonkeyPatch, pool: PoolConfig) -> list[str]:
        recorded: list[list[str]] = []
        client = DockerClient()
        monkeypatch.setattr(client, "ensure_image", lambda image: True)
        monkeypatch.setattr(client, "_exec", lambda args: recorded.append(args) or 0)
        client.spawn_runner(pool)
        return recorded[0]

    def test_work_dir_is_an_exec_tmpfs_owned_by_the_runner(
        self, monkeypatch: pytest.MonkeyPatch, pool: PoolConfig
    ) -> None:
        args = self._spawn_args(monkeypatch, replace(pool, work_tmpfs_size="3g"))

        assert args[args.index("--tmpfs") + 1] == (
            f"{RUNNER_WORK_DIR}:rw,exec,size=3g,uid=1000,gid=1000"
        )
        # Options precede the image, or docker would pass them to the entrypoint.
        assert args.index("--tmpfs") < args.index(expand_image(pool.runner_image))

    def test_auto_size_follows_the_memory_limit(
        self, monkeypatch: pytest.MonkeyPatch, pool: PoolConfig
    ) -> None:
        args = self._spawn_args(monkeypatch, replace(pool, memory_limit="10g"))
        assert ",size=5120m," in args[args.index("--tmpfs") + 1]

    def test_disabled_tmpfs_adds_no_mount(
        self, monkeypatch: pytest.MonkeyPatch, pool: PoolConfig
    ) -> None:
        args = self._spawn_args(monkeypatch, replace(pool, work_tmpfs_size="0"))
        assert "--tmpfs" not in args


class TestCleanupCiContainers:
    _NOW = datetime(2026, 10, 3, 20, 0, tzinfo=UTC)
    _OLD = "2026-10-03 10:35:44 +0300 EEST"  # 12h24m before _NOW
    _NEW = "2026-10-03 22:26:48 +0300 EEST"  # 33 minutes before _NOW

    _PS_OUTPUT = "\n".join(
        [
            # runner `services:` container on a job network
            f"d883d79bc3f04918b85bdad0b4981ab1_mysql84_39083f\t{_OLD}\t"
            "github_network_11b7677724a74b35b00050ff75047410\t7415c9=",
            # same, but young enough that its job may still be running
            f"aaaa_mysql84_111111\t{_NEW}\tgithub_network_aaaa\t7415c9=",
            # workflow `docker run`, matched by name pattern
            f"petopolis-ci-37103043876-backend-test-376-db\t{_OLD}\t"
            "petopolis-ci-37103043876-backend-test-376-net\t",
            # workflow `docker run`, matched by label pattern
            f"cool_williamson\t{_OLD}\tbridge\tpetopolis-ci=37144039620-sca-376",
            # host infrastructure: never touched
            f"shortlinks-mysql\t{_OLD}\tshortlinks_default\tcom.docker.compose.project=s",
            f"portainer\t{_OLD}\tbridge\t",
            f"rorch-mariadb\t{_OLD}\trorch_default\t",
            f"gh-runner-orchestrator\t{_OLD}\thost\t",
            # a pattern that would match a runner must still spare it
            f"gh-runner-petopolis-ci-aaaaaaaa\t{_OLD}\thost\t",
            # unknown origin, no pattern matches
            f"trusting_gagarin\t{_OLD}\tbridge\t",
            # unparseable date: keep
            "bbbb_mysql84_222222\tyesterday\tgithub_network_bbbb\t",
            "malformed-row",
        ]
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

    def test_removes_only_old_runner_service_containers_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        removed: list[str] = []
        self._client(monkeypatch, removed).cleanup_ci_containers(360, now=self._NOW)
        assert removed == ["d883d79bc3f04918b85bdad0b4981ab1_mysql84_39083f"]

    def test_configured_patterns_add_workflow_started_containers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        removed: list[str] = []
        self._client(monkeypatch, removed).cleanup_ci_containers(
            360, ("petopolis-ci-*", "*petopolis-ci-*", "label=petopolis-ci"), now=self._NOW
        )
        assert sorted(removed) == [
            "cool_williamson",
            "d883d79bc3f04918b85bdad0b4981ab1_mysql84_39083f",
            "petopolis-ci-37103043876-backend-test-376-db",
        ]

    def test_catch_all_pattern_still_spares_runners_and_rorch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        removed: list[str] = []
        self._client(monkeypatch, removed).cleanup_ci_containers(360, ("*",), now=self._NOW)
        assert not any(name.startswith(("gh-runner-", "rorch-")) for name in removed)

    def test_disabled_when_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def fake_capture(args: list[str]) -> tuple[str, int]:
            calls.append(args)
            return "", 0

        client = DockerClient()
        monkeypatch.setattr(client, "_capture", fake_capture)
        client.cleanup_ci_containers(0, ("*",))
        assert calls == []  # no docker call at all when disabled


class TestToolCache:
    def test_runner_is_pointed_at_the_shared_toolcache(
        self, monkeypatch: pytest.MonkeyPatch, pool: PoolConfig
    ) -> None:
        """Unset, the agent falls back to _work/_tool and downloads toolchains per job."""
        recorded: list[list[str]] = []
        client = DockerClient()
        monkeypatch.setattr(client, "ensure_image", lambda image: True)
        monkeypatch.setattr(client, "_exec", lambda args: recorded.append(args) or 0)

        client.spawn_runner(pool)

        args = recorded[0]
        envs = {args[i + 1] for i, arg in enumerate(args) if arg == "-e"}
        assert "RUNNER_TOOL_CACHE=/opt/hostedtoolcache" in envs
        assert "AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache" in envs
        assert "/opt/hostedtoolcache:/opt/hostedtoolcache" in args
        assert args.index("-e") < args.index(expand_image(pool.runner_image))


class TestCleanupStuck:
    _PS_OUTPUT = (
        "gh-runner-tt-online00\t20 minutes\n"  # listed online → keep
        "gh-runner-tt-young000\t2 minutes\n"  # under the timeout → keep
        "gh-runner-tt-working0\t20 minutes\n"  # Runner.Worker alive → keep
        "gh-runner-tt-noinfo00\t20 minutes\n"  # docker top fails → keep (safe)
        "gh-runner-tt-stuck000\t20 minutes\n"  # no worker, never online → kill
    )
    _TOP: ClassVar[dict[str, tuple[str, int]]] = {
        "gh-runner-tt-working0": (
            "UID PID PPID C STIME TTY TIME CMD\n"
            "1000 10 1 0 10:00 ? 00:00:01 /home/runner/actions-runner/bin/Runner.Listener run\n"
            "1000 20 10 0 10:01 ? 00:00:09 /home/runner/actions-runner/bin/Runner.Worker "
            "spawnclient 120 123\n",
            0,
        ),
        "gh-runner-tt-noinfo00": ("", 1),
        "gh-runner-tt-stuck000": (
            "UID PID PPID C STIME TTY TIME CMD\n"
            "1000 10 1 0 10:00 ? 00:00:01 /bin/bash /entrypoint.sh\n",
            0,
        ),
    }

    def _client(self, monkeypatch: pytest.MonkeyPatch, removed: list[str]) -> DockerClient:
        def fake_capture(args: list[str]) -> tuple[str, int]:
            if args[0] == "ps":
                return self._PS_OUTPUT, 0
            if args[0] == "top":
                return self._TOP[args[1]]
            if args[0] == "rm":
                removed.append(args[-1])
            return "", 0

        client = DockerClient()
        monkeypatch.setattr(client, "_capture", fake_capture)
        return client

    def test_kills_only_containers_without_a_running_job(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        removed: list[str] = []
        self._client(monkeypatch, removed).cleanup_stuck(
            "gh-runner-tt", {"gh-runner-tt-online00"}, timeout_minutes=8
        )
        assert removed == ["gh-runner-tt-stuck000"]

    def test_online_and_young_containers_are_not_even_inspected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inspected: list[str] = []
        client = self._client(monkeypatch, [])
        original = client._capture

        def spying_capture(args: list[str]) -> tuple[str, int]:
            if args[0] == "top":
                inspected.append(args[1])
            return original(args)

        monkeypatch.setattr(client, "_capture", spying_capture)
        client.cleanup_stuck("gh-runner-tt", {"gh-runner-tt-online00"}, timeout_minutes=8)

        assert "gh-runner-tt-online00" not in inspected
        assert "gh-runner-tt-young000" not in inspected

    def test_has_running_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._client(monkeypatch, [])
        assert client.has_running_job("gh-runner-tt-working0") is True
        assert client.has_running_job("gh-runner-tt-stuck000") is False
        assert client.has_running_job("gh-runner-tt-noinfo00") is None


class TestPruneNetworks:
    def test_prunes_only_unused_networks_past_the_grace_period(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_capture(args: list[str]) -> tuple[str, int]:
            calls.append(args)
            return "Deleted Networks:\ngithub_network_0d3c\n", 0

        client = DockerClient()
        monkeypatch.setattr(client, "_capture", fake_capture)

        client.prune_networks()

        # `network prune` never touches a network with a container attached;
        # `until` spares a job's network before its first service container joins.
        assert calls == [["network", "prune", "-f", "--filter", "until=30m"]]
