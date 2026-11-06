# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Gitea Actions API client, and the per-pool choice between forges."""

from threading import Lock
from typing import Any

from rorch.config import PoolConfig
from rorch.github_client import DEFAULT_MAX_CONCURRENT_REQUESTS, GitHubClient
from rorch.protocols import JobInfo, RunnerInfo


class GiteaClient(GitHubClient):
    """Gitea's Actions API has GitHub's paths, statuses and payloads for what rorch reads."""

    # Gitea's default cap (API.MAX_RESPONSE_ITEMS). A full page must equal this,
    # or repository discovery stops after the first page.
    REPO_PAGE_SIZE = 50

    def __init__(self, base_url: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.API_BASE = f"{base_url.rstrip('/')}/api/v1"

    def _request(self, pat: str, path: str, method: str = "GET") -> Any | None:
        return super()._request(pat, path.replace("per_page=", "limit="), method)

    def registration_token(self, pool: PoolConfig) -> str | None:
        """Token that can only register runners, so a runner never holds the account token."""
        data = self._request(pool.pat, f"{pool.api_runners_path}/registration-token", "POST")
        token = data.get("token") if isinstance(data, dict) else None
        return token if isinstance(token, str) and token else None


class ForgeClients:
    """RunnerAPIClient that sends each pool's calls to the forge that pool belongs to.

    A token must only ever reach its own server, so nothing outside this class
    picks a client for a pool.
    """

    def __init__(
        self,
        github: GitHubClient,
        max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS,
    ) -> None:
        self._github = github
        self._concurrency = max_concurrent_requests
        self._gitea: dict[str, GiteaClient] = {}
        self._lock = Lock()

    def _for(self, pool: PoolConfig) -> GitHubClient:
        return self._gitea_client(pool) if pool.is_gitea else self._github

    def _gitea_client(self, pool: PoolConfig) -> GiteaClient:
        with self._lock:
            client = self._gitea.get(pool.base_url)
            if client is None:
                client = GiteaClient(pool.base_url, max_concurrent_requests=self._concurrency)
                self._gitea[pool.base_url] = client
            return client

    def registration_token(self, pool: PoolConfig) -> str | None:
        return self._gitea_client(pool).registration_token(pool)

    def list_repositories(self, pool: PoolConfig) -> list[str] | None:
        return self._for(pool).list_repositories(pool)

    def is_public_repo(self, pool: PoolConfig) -> bool | None:
        return self._for(pool).is_public_repo(pool)

    def get_queued_count(self, pool: PoolConfig, jobs: list[JobInfo] | None = None) -> int:
        return self._for(pool).get_queued_count(pool, jobs)

    def get_queued_ages(self, pool: PoolConfig, jobs: list[JobInfo] | None = None) -> list[float]:
        return self._for(pool).get_queued_ages(pool, jobs)

    def list_runners(self, pool: PoolConfig) -> list[RunnerInfo] | None:
        return self._for(pool).list_runners(pool)

    def deregister_runner(self, pool: PoolConfig, runner_id: int) -> bool:
        return self._for(pool).deregister_runner(pool, runner_id)
