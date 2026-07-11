# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Tests for GitHub API repository discovery."""

from unittest.mock import MagicMock

from rorch.config import PoolConfig
from rorch.github_client import GitHubClient
from rorch.protocols import RunnerInfo


def _repo(name: str, owner: str = "test-user", **overrides: object) -> dict[str, object]:
    repo: dict[str, object] = {
        "name": name,
        "owner": {"login": owner},
        "archived": False,
        "disabled": False,
    }
    repo.update(overrides)
    return repo


class TestListRepositories:
    def test_returns_none_when_discovery_request_fails(self) -> None:
        client = GitHubClient()
        client._get = MagicMock(return_value=None)  # type: ignore[method-assign]
        pool = PoolConfig(
            name="personal",
            pat="token",
            owner="test-user",
            scope="personal",
        )

        assert client.list_repositories(pool) is None

    def test_returns_only_active_repositories_owned_by_account(self) -> None:
        client = GitHubClient()
        client._get = MagicMock(  # type: ignore[method-assign]
            return_value=[
                _repo("zeta"),
                _repo("alpha"),
                _repo("archived", archived=True),
                _repo("disabled", disabled=True),
                _repo("someone-elses", owner="another-user"),
            ]
        )
        pool = PoolConfig(
            name="personal",
            pat="token",
            owner="Test-User",
            scope="personal",
        )

        repositories = client.list_repositories(pool)

        assert repositories == ["alpha", "zeta"]
        client._get.assert_called_once_with(  # type: ignore[attr-defined]
            "token",
            "/user/repos?affiliation=owner&visibility=all&per_page=100&page=1",
        )

    def test_paginates_until_github_returns_less_than_one_page(self) -> None:
        client = GitHubClient()
        first_page = [_repo(f"repo-{index}") for index in range(100)]
        client._get = MagicMock(  # type: ignore[method-assign]
            side_effect=[first_page, [_repo("last-repo")]]
        )
        pool = PoolConfig(
            name="personal",
            pat="token",
            owner="test-user",
            scope="personal",
        )

        repositories = client.list_repositories(pool)

        assert len(repositories) == 101
        assert "last-repo" in repositories
        assert client._get.call_count == 2  # type: ignore[attr-defined]


class TestRunnerOperations:
    def test_lists_typed_runner_snapshot(self) -> None:
        client = GitHubClient()
        client._get = MagicMock(  # type: ignore[method-assign]
            return_value={
                "runners": [
                    {"id": 1, "name": "idle", "status": "online", "busy": False},
                    {"id": 2, "name": "busy", "status": "online", "busy": True},
                ]
            }
        )
        pool = PoolConfig(name="repo", pat="token", owner="owner", repo="project")

        runners = client.list_runners(pool)

        assert runners == [
            RunnerInfo(id=1, name="idle", status="online", busy=False),
            RunnerInfo(id=2, name="busy", status="online", busy=True),
        ]

    def test_deregisters_one_runner(self) -> None:
        client = GitHubClient()
        client._delete = MagicMock(return_value=True)  # type: ignore[method-assign]
        pool = PoolConfig(name="repo", pat="token", owner="owner", repo="project")

        assert client.deregister_runner(pool, 42)
        client._delete.assert_called_once_with(  # type: ignore[attr-defined]
            "token", "/repos/owner/project/actions/runners/42"
        )
