# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Tests for GitHub API repository discovery."""

from unittest.mock import MagicMock

from rorch.config import PoolConfig
from rorch.github_client import GitHubClient


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
