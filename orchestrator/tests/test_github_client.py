# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Tests for GitHub API repository discovery."""

import io
import urllib.error
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from rorch.config import PoolConfig
from rorch.errors import GitHubRateLimitError
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


def _response(data: bytes, headers: dict[str, str], status: int = 200) -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = status
    response.headers = headers
    response.read.return_value = data
    return response


class TestRateLimitProtection:
    def test_reuses_cached_body_after_conditional_304(self) -> None:
        client = GitHubClient(rate_limit_reserve=0)
        first = _response(b'{"value": 1}', {"ETag": '"version-1"'})
        not_modified = urllib.error.HTTPError(
            url="https://api.github.com/test",
            code=304,
            msg="Not Modified",
            hdrs={},
            fp=io.BytesIO(b""),
        )

        with patch("urllib.request.urlopen", side_effect=[first, not_modified]) as urlopen:
            first_data = client._get("token", "/test")
            second_data = client._get("token", "/test")

        assert first_data == {"value": 1}
        assert second_data == first_data
        second_request = urlopen.call_args_list[1].args[0]
        assert second_request.get_header("If-none-match") == '"version-1"'

    def test_stops_requests_until_primary_limit_reset(self) -> None:
        clock = MagicMock(return_value=100.0)
        client = GitHubClient(rate_limit_reserve=0, wall_clock=clock)
        exhausted = urllib.error.HTTPError(
            url="https://api.github.com/test",
            code=403,
            msg="Forbidden",
            hdrs={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "200"},
            fp=io.BytesIO(b'{"message":"API rate limit exceeded"}'),
        )

        with patch("urllib.request.urlopen", side_effect=exhausted) as urlopen:
            with pytest.raises(GitHubRateLimitError) as first_error:
                client._get("token", "/test")
            with pytest.raises(GitHubRateLimitError):
                client._get("token", "/another-test")

        assert first_error.value.retry_at_epoch == 201
        urlopen.assert_called_once()

    def test_preserves_configured_request_reserve(self) -> None:
        clock = MagicMock(return_value=100.0)
        client = GitHubClient(rate_limit_reserve=100, wall_clock=clock)
        response = _response(
            b'{"value": 1}',
            {"X-RateLimit-Remaining": "100", "X-RateLimit-Reset": "200"},
        )

        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            with pytest.raises(GitHubRateLimitError) as reserve_error:
                client._get("token", "/test")
            with pytest.raises(GitHubRateLimitError):
                client._get("token", "/another-test")

        assert reserve_error.value.retry_at_epoch == 201
        urlopen.assert_called_once()

    def test_uses_retry_after_for_secondary_limit(self) -> None:
        clock = MagicMock(return_value=100.0)
        client = GitHubClient(rate_limit_reserve=0, wall_clock=clock)
        limited = urllib.error.HTTPError(
            url="https://api.github.com/test",
            code=429,
            msg="Too Many Requests",
            hdrs={"Retry-After": "90"},
            fp=io.BytesIO(b'{"message":"slow down"}'),
        )

        with (
            patch("urllib.request.urlopen", side_effect=limited),
            pytest.raises(GitHubRateLimitError) as error,
        ):
            client._get("token", "/test")

        assert error.value.retry_at_epoch == 190


class TestRepositoryDiscoveryFilters:
    """A self-hosted runner on a public repo is code execution for any forker."""

    _REPOS: ClassVar[list[dict[str, object]]] = [
        {"name": "private-api", "private": True, "owner": {"login": "acme"}},
        {"name": "public-docs", "private": False, "owner": {"login": "acme"}},
        {"name": "legacy-thing", "private": True, "owner": {"login": "acme"}},
        {"name": "archived", "private": True, "owner": {"login": "acme"}, "archived": True},
        {"name": "someone-else", "private": True, "owner": {"login": "other"}},
    ]

    def _discover(self, pool: PoolConfig, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        client = GitHubClient()
        monkeypatch.setattr(
            client, "_get", lambda pat, path: self._REPOS if "page=1" in path else []
        )
        return client.list_repositories(pool) or []

    def test_public_repos_excluded_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = PoolConfig(name="p", pat="ghp_x", owner="acme", scope="personal")
        assert self._discover(pool, monkeypatch) == ["legacy-thing", "private-api"]

    def test_public_repos_included_when_opted_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = PoolConfig(
            name="p", pat="ghp_x", owner="acme", scope="personal", include_public_repos=True
        )
        assert "public-docs" in self._discover(pool, monkeypatch)

    def test_exclude_repos_applies_on_top(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = PoolConfig(
            name="p", pat="ghp_x", owner="acme", scope="personal", exclude_repos="legacy-*"
        )
        assert self._discover(pool, monkeypatch) == ["private-api"]

    def test_archived_and_other_owners_still_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = PoolConfig(
            name="p", pat="ghp_x", owner="acme", scope="personal", include_public_repos=True
        )
        found = self._discover(pool, monkeypatch)
        assert "archived" not in found
        assert "someone-else" not in found


class TestPublicRepoDetection:
    def _client(self, payload: object, monkeypatch: pytest.MonkeyPatch) -> GitHubClient:
        client = GitHubClient()
        monkeypatch.setattr(client, "_get", lambda pat, path: payload)
        return client

    def test_public_repo_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = PoolConfig(name="p", pat="ghp_x", owner="acme", repo="docs")
        assert self._client({"private": False}, monkeypatch).is_public_repo(pool) is True

    def test_private_repo_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = PoolConfig(name="p", pat="ghp_x", owner="acme", repo="api")
        assert self._client({"private": True}, monkeypatch).is_public_repo(pool) is False

    def test_discovery_pool_is_not_applicable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = PoolConfig(name="p", pat="ghp_x", owner="acme", scope="personal")
        assert self._client({"private": False}, monkeypatch).is_public_repo(pool) is None

    def test_api_failure_is_inconclusive_not_a_false_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = PoolConfig(name="p", pat="ghp_x", owner="acme", repo="api")
        assert self._client(None, monkeypatch).is_public_repo(pool) is None
