# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

import json
import urllib.request
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, call

import pytest

from rorch import __main__ as daemon
from rorch.config import PoolConfig, load_config, validation_errors
from rorch.docker_client import DockerClient
from rorch.github_client import GitHubClient
from rorch.protocols import RunnerInfo
from rorch.resolver import ConfigResolver
from rorch.store import Store

WORKFLOW = ".github/workflows/main-ci.yml"


@pytest.fixture
def trusted(pool: PoolConfig) -> PoolConfig:
    return replace(
        pool,
        main_workflow=WORKFLOW,
        main_runner_group_id=42,
        main_actions_policy_id=17,
        min_idle=0,
        runner_labels="main-trusted",
        network_mode="bridge",
    )


@pytest.fixture
def trusted_runtime(
    trusted: PoolConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> daemon.Runtime:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RORCH_DB", "off")
    monkeypatch.setattr(daemon, "load_config", lambda: [trusted])
    return daemon._build_runtime()


@pytest.mark.parametrize(
    ("event", "branch", "labels", "expected"),
    [
        ("push", "main", ["main-trusted"], 1),
        ("pull_request", "main", ["main-trusted"], 0),
        ("pull_request_target", "main", ["main-trusted"], 0),
        ("workflow_dispatch", "main", ["main-trusted"], 0),
        ("push", "feature", ["main-trusted"], 0),
        ("push", "main", [], 0),
        ("push", "main", ["docker"], 0),
    ],
)
def test_counts_only_main_pushes_for_the_trusted_pool(
    trusted: PoolConfig,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    branch: str,
    labels: list[str],
    expected: int,
) -> None:
    client = GitHubClient()
    scan = Mock(
        side_effect=[
            {"workflow_runs": [{"id": 1, "event": event, "head_branch": branch}]},
            {"jobs": [{"status": "queued", "labels": labels}]},
            {"workflow_runs": []},
            {"workflow_runs": []},
        ]
    )
    monkeypatch.setattr(client, "_get_shared", scan)

    assert client.get_queued_count(trusted) == expected
    assert "/actions/workflows/main-ci.yml/runs?" in scan.call_args_list[0].args[1]


def test_only_main_pushes_count_on_later_run_pages(
    trusted: PoolConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued_job = {"jobs": [{"status": "queued", "labels": ["main-trusted"]}]}
    scan = Mock(
        side_effect=[
            {"workflow_runs": []},
            {"workflow_runs": []},
            {"total_count": 52, "workflow_runs": [{"id": index} for index in range(1, 51)]},
            *[{"jobs": []}] * 50,
            {
                "total_count": 52,
                "workflow_runs": [
                    {"id": 51, "event": "push", "head_branch": "main"},
                    {"id": 52, "event": "pull_request", "head_branch": "main"},
                ],
            },
            queued_job,
            queued_job,
        ]
    )
    client = GitHubClient()
    monkeypatch.setattr(client, "_get_shared", scan)
    second_page = (
        f"/repos/{trusted.owner}/{trusted.repo}/actions/workflows/main-ci.yml/runs"
        "?status=in_progress&per_page=50&branch=main&event=push&page=2"
    )

    assert client.get_queued_count(trusted) == 1
    assert call(trusted.pat, second_page) in scan.call_args_list


def test_trusted_and_ordinary_pools_cannot_coexist(trusted: PoolConfig, pool: PoolConfig) -> None:
    assert validation_errors([trusted, replace(pool, name="ordinary")])


def test_loads_main_trusted_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.yml"
    path.write_text(f"""
pools:
  - name: trusted
    owner: test-org
    repo: test-repo
    main_workflow: {WORKFLOW}
    main_runner_group_id: 42
    main_actions_policy_id: 17
    runner_labels: main-trusted
    min_idle: 0
    network_mode: bridge
""")
    monkeypatch.setenv("GITHUB_PAT", "fake")

    [pool] = load_config(str(path))
    assert pool.is_main_trusted
    assert pool.main_runner_group_id == 42
    assert pool.main_actions_policy_id == 17
    assert pool.main_workflow == WORKFLOW
    assert validation_errors([pool]) == []


@pytest.mark.parametrize(
    "settings",
    [
        {"main_runner_group_id": 0},
        {"main_actions_policy_id": True},
        {"main_workflow": "unqualified.yml"},
        {"repo": ""},
        {"provider": "gitea", "base_url": "https://gitea.example"},
        {"min_idle": 1},
        {"network_mode": "host"},
    ],
)
def test_rejects_invalid_main_config(trusted: PoolConfig, settings: dict[str, Any]) -> None:
    assert validation_errors([replace(trusted, **settings)])


def test_regular_pool_cannot_use_reserved_label(pool: PoolConfig) -> None:
    assert validation_errors([replace(pool, runner_labels="docker, Main-Trusted")])


def test_main_host_ignores_unsafe_overlays(
    trusted: PoolConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_PAT", "fake")
    with closing(Store(":memory:")) as store:
        store.set_pool_override("old", {"owner": "test-org", "repo": "pr-ci"}, origin="ui")
        store.set_pool_override(trusted.name, {"network_mode": "host", "min_idle": 2})

        effective = ConfigResolver([trusted], 0, 0, store).resolve()

    assert effective.pools == [trusted]


def _event_rule(*events: str) -> dict[str, object]:
    return {"type": "restrict_action_events", "parameters": {"allowed_events": list(events)}}


@pytest.fixture
def github_policy_responses(trusted: PoolConfig) -> list[dict[str, Any]]:
    return [
        {
            "id": 42,
            "default": False,
            "visibility": "selected",
            "allows_public_repositories": False,
            "restricted_to_workflows": True,
            "selected_workflows": [f"{trusted.owner}/{trusted.repo}/{WORKFLOW}@refs/heads/main"],
        },
        {
            "enforcement": "active",
            "conditions": {
                "repository_name": {"include": [trusted.repo], "exclude": []},
                "workflow_path": {"include": [WORKFLOW], "exclude": []},
            },
            "rules": [_event_rule("push")],
        },
        {
            "total_count": 1,
            "repositories": [{"full_name": f"{trusted.owner}/{trusted.repo}", "private": True}],
        },
    ]


def test_requests_jit_registration_in_verified_group(
    trusted: PoolConfig,
    github_policy_responses: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[urllib.request.Request] = []
    payloads = iter([*github_policy_responses, {"encoded_jit_config": "jit-fixture"}])

    @contextmanager
    def urlopen(request: urllib.request.Request, *, timeout: int) -> Iterator[SimpleNamespace]:
        calls.append(request)
        body = json.dumps(next(payloads)).encode()
        yield SimpleNamespace(headers={}, status=200, read=lambda: body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    assert GitHubClient().create_main_jit_config(trusted, "runner") == "jit-fixture"
    assert [call.full_url for call in calls] == [
        "https://api.github.com/orgs/test-org/actions/runner-groups/42",
        "https://api.github.com/orgs/test-org/actions/policies/17",
        "https://api.github.com/orgs/test-org/actions/runner-groups/42/repositories",
        "https://api.github.com/orgs/test-org/actions/runners/generate-jitconfig",
    ]
    assert calls[1].get_header("X-github-api-version") == "2026-03-10"
    assert calls[3].get_method() == "POST"
    assert json.loads(calls[3].data or b"") == {
        "name": "runner",
        "runner_group_id": 42,
        "labels": ["main-trusted"],
        "work_folder": "_work",
    }


@pytest.mark.parametrize("failures", [1, 3])
def test_versioned_policy_read_retries_before_jit_registration(
    trusted: PoolConfig,
    github_policy_responses: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    failures: int,
) -> None:
    calls: list[urllib.request.Request] = []
    responses = iter(
        [
            github_policy_responses[0],
            *(TimeoutError() for _ in range(failures)),
            *github_policy_responses[1:],
            {"encoded_jit_config": "jit-fixture"},
        ]
    )

    @contextmanager
    def urlopen(request: urllib.request.Request, *, timeout: int) -> Iterator[SimpleNamespace]:
        calls.append(request)
        response = next(responses)
        if isinstance(response, TimeoutError):
            raise response
        yield SimpleNamespace(headers={}, status=200, read=lambda: json.dumps(response).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    sleep = Mock()
    client = GitHubClient(sleep=sleep, wall_clock=lambda: 10.0)

    config = client.create_main_jit_config(trusted, "runner")

    assert config == ("jit-fixture" if failures == 1 else None)
    versions = [
        item.get_header("X-github-api-version") for item in calls if "/policies/" in item.full_url
    ]
    assert versions == ["2026-03-10"] * min(failures + 1, 3)
    assert [item.get_method() for item in calls].count("POST") == int(failures == 1)
    assert sleep.call_args_list == [call(1.0), call(2.0)][:failures]


@pytest.mark.parametrize(
    ("response_index", "key", "value"),
    [
        (0, "restricted_to_workflows", False),
        (0, "selected_workflows", [f"test-org/test-repo/{WORKFLOW}@refs/heads/feature"]),
        (0, "allows_public_repositories", True),
        (1, "enforcement", "evaluate"),
        (1, "conditions", {}),
        (1, "rules", [_event_rule("push", "pull_request_target")]),
        (1, "rules", None),
        (2, "total_count", 2),
        (2, "repositories", []),
        (2, "repositories", [{"full_name": "test-org/other", "private": True}]),
        (2, "repositories", [{"full_name": "test-org/test-repo", "private": False}]),
    ],
)
def test_rejects_jit_registration_without_verified_assignment_rules(
    trusted: PoolConfig,
    github_policy_responses: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    response_index: int,
    key: str,
    value: object,
) -> None:
    github_policy_responses[response_index][key] = value
    client = GitHubClient()
    responses = iter(github_policy_responses)
    request = Mock(
        side_effect=lambda pat, path, method="GET", **kwargs: (
            {"encoded_jit_config": "jit-fixture"} if method == "POST" else next(responses)
        )
    )
    monkeypatch.setattr(client, "_request", request)

    assert client.create_main_jit_config(trusted, "runner") is None
    assert request.call_count == response_index + 1


@pytest.mark.parametrize(
    ("second_page", "expected_tail"),
    [
        (
            {
                "total_count": 101,
                "runners": [
                    {"id": 101, "name": "runner-101", "status": "online", "busy": True},
                ],
            },
            [RunnerInfo(id=101, name="runner-101", status="online", busy=True)],
        ),
        (None, None),
    ],
)
def test_trusted_runner_listing_reads_second_page(
    trusted: PoolConfig,
    trusted_runtime: daemon.Runtime,
    monkeypatch: pytest.MonkeyPatch,
    second_page: dict[str, object] | None,
    expected_tail: list[RunnerInfo] | None,
) -> None:
    first_page = [
        {"id": index, "name": f"runner-{index}", "status": "offline"} for index in range(1, 101)
    ]
    get = Mock(side_effect=[{"total_count": 101, "runners": first_page}, second_page])
    monkeypatch.setattr(trusted_runtime.github, "_get", get)

    runners = trusted_runtime.forge.list_runners(trusted)

    assert (runners[100:] if runners is not None else None) == expected_tail
    path = "/orgs/test-org/actions/runner-groups/42/runners?per_page=100"
    assert get.call_args_list == [call(trusted.pat, f"{path}&page={page}") for page in (1, 2)]


@pytest.mark.parametrize(
    ("first_total", "second_total", "second_ids"),
    [
        pytest.param(101, 101, [100], id="overlapping-pages"),
        pytest.param(102, 102, [101, 101], id="duplicate-on-page"),
        pytest.param(101, 102, [101, 102], id="growing-total"),
        pytest.param(102, 101, [101], id="shrinking-total"),
        pytest.param(101, 101, [None], id="invalid-runner-id"),
    ],
)
def test_trusted_runner_listing_rejects_inconsistent_pages(
    trusted: PoolConfig,
    trusted_runtime: daemon.Runtime,
    monkeypatch: pytest.MonkeyPatch,
    first_total: int,
    second_total: int,
    second_ids: list[int | None],
) -> None:
    pages = iter(
        {"total_count": total, "runners": [{"id": id_, "name": f"runner-{id_}"} for id_ in ids]}
        for total, ids in ((first_total, range(1, 101)), (second_total, second_ids))
    )

    @contextmanager
    def urlopen(request: urllib.request.Request, *, timeout: int) -> Iterator[SimpleNamespace]:
        body = json.dumps(next(pages)).encode()
        yield SimpleNamespace(headers={}, status=200, read=lambda: body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    assert trusted_runtime.forge.list_runners(trusted) is None


def test_trusted_runner_uses_private_docker(
    trusted: PoolConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = DockerClient(main_jit_config=Mock(return_value="jit-fixture"))
    launch = Mock(return_value=0)
    monkeypatch.setattr(client, "ensure_image", Mock(return_value=True))
    monkeypatch.setattr(client, "_exec", launch)

    assert client.spawn_runner(trusted)
    args = launch.call_args.args[0]
    assert "--entrypoint" in args
    assert args[args.index("--entrypoint") + 1] == "/main-trusted-entrypoint.sh"
    assert "RORCH_MAIN_JIT_CONFIG=jit-fixture" in args
    assert "--privileged" in args
    assert args[args.index("--network") + 1] == "bridge"
    assert "-v" not in args
    assert "--mount" not in args
    assert not any(arg.startswith("GITHUB_PAT=") for arg in args)


def test_missing_jit_config_prevents_trusted_launch(
    trusted: PoolConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DockerClient(main_jit_config=Mock(return_value=None))
    launch = Mock(return_value=0)
    monkeypatch.setattr(client, "ensure_image", Mock(return_value=True))
    monkeypatch.setattr(client, "_exec", launch)

    assert not client.spawn_runner(trusted)
    launch.assert_not_called()
