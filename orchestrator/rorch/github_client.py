# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""GitHub API client for runner management."""

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rorch.config import PoolConfig
from rorch.errors import GitHubRateLimitError
from rorch.protocols import JobInfo, RunnerInfo

log = logging.getLogger(__name__)

MAX_CONDITIONAL_CACHE_ENTRIES = 2048
SECONDARY_RATE_LIMIT_BASE_SECONDS = 60
SECONDARY_RATE_LIMIT_MAX_SECONDS = 900
MUTATION_DELAY_SECONDS = 1.0


@dataclass(frozen=True)
class ConditionalResponse:
    """Last successful GET response and its GitHub validator."""

    etag: str
    data: Any


def _job_info(job: dict[str, Any], run: dict[str, Any], repo: str) -> JobInfo:
    """Map one GitHub jobs-API entry onto JobInfo."""
    return JobInfo(
        job_id=int(job.get("id", 0)),
        repo=repo,
        # `name` is the run's display name; workflow_name is the file's name and
        # is the more stable label when a run sets a custom run-name.
        workflow=str(run.get("workflow_name") or run.get("name") or ""),
        job_name=str(job.get("name", "")),
        runner=str(job.get("runner_name") or ""),
        status=str(job.get("status", "")),
        conclusion=str(job.get("conclusion") or ""),
        url=str(job.get("html_url") or ""),
        started_at=str(job.get("started_at") or ""),
        completed_at=str(job.get("completed_at") or ""),
    )


class GitHubClient:
    """Handles all GitHub REST API communication for runner orchestration."""

    API_BASE = "https://api.github.com"
    API_VERSION = "2022-11-28"
    TIMEOUT = 10

    def __init__(
        self,
        rate_limit_reserve: int = 100,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rate_limit_reserve = rate_limit_reserve
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._request_lock = threading.Lock()
        self._blocked_until: dict[bytes, float] = {}
        self._secondary_failures: dict[bytes, int] = {}
        self._conditional_cache: OrderedDict[tuple[bytes, str], ConditionalResponse] = OrderedDict()
        self._last_mutation_at = 0.0
        self._last_remaining: int | None = None
        self._last_reset_at: float | None = None
        # Highest retry_at across tokens, so the dashboard can report a
        # cooldown without iterating _blocked_until while a request mutates it.
        self._blocked_until_any = 0.0

    def _request(self, pat: str, path: str, method: str = "GET") -> Any | None:
        token_key = hashlib.sha256(pat.encode()).digest()
        cache_key = (token_key, path)

        with self._request_lock:
            now = self._wall_clock()
            self._raise_if_blocked(token_key, now)
            self._pace_mutation(method, now)

            url = f"{self.API_BASE}{path}"
            req = urllib.request.Request(url, method=method)
            req.add_header("Authorization", f"Bearer {pat}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("X-GitHub-Api-Version", self.API_VERSION)
            req.add_header("User-Agent", "rorch-runner-orchestrator")
            cached = self._conditional_cache.get(cache_key) if method == "GET" else None
            if cached is not None:
                req.add_header("If-None-Match", cached.etag)

            try:
                with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                    self._update_rate_limit(token_key, resp.headers)
                    self._secondary_failures.pop(token_key, None)
                    if method == "DELETE":
                        return resp.status == 204
                    data = json.loads(resp.read())
                    self._cache_conditional_response(cache_key, resp.headers.get("ETag"), data)
                    return data
            except urllib.error.HTTPError as error:
                if error.code == 304 and cached is not None:
                    self._update_rate_limit(token_key, error.headers)
                    self._secondary_failures.pop(token_key, None)
                    self._conditional_cache.move_to_end(cache_key)
                    return cached.data

                body = error.read().decode(errors="replace")
                if self._is_rate_limit_error(error, body):
                    retry_at, reason = self._rate_limit_retry(token_key, error, body)
                    self._block_token(token_key, retry_at, reason)
                    raise GitHubRateLimitError(retry_at, reason) from error

                log.error(
                    "GitHub API %s %d: %s — %s",
                    method,
                    error.code,
                    path,
                    body[:200],
                )
                return None
            except GitHubRateLimitError:
                raise
            except Exception as error:
                log.error("GitHub %s request failed: %s", method, error)
                return None

    def _raise_if_blocked(self, token_key: bytes, now: float) -> None:
        blocked_until = self._blocked_until.get(token_key, 0.0)
        if blocked_until <= now:
            self._blocked_until.pop(token_key, None)
            return
        raise GitHubRateLimitError(blocked_until, "GitHub API cooldown is active")

    def _pace_mutation(self, method: str, now: float) -> None:
        if method not in {"POST", "PATCH", "PUT", "DELETE"}:
            return
        delay = MUTATION_DELAY_SECONDS - (now - self._last_mutation_at)
        if delay > 0:
            self._sleep(delay)
        self._last_mutation_at = self._wall_clock()

    def rate_limit_status(self) -> dict[str, Any]:
        """Last-seen API budget, for the dashboard. Read-only, makes no API call.

        Deliberately lock-free: `_request_lock` is held for the duration of each
        GitHub call, so taking it here would stall a dashboard refresh behind a
        10s API timeout. These are plain attribute reads of values written under
        that lock, so the worst case is a status one tick stale.
        """
        blocked_until = self._blocked_until_any
        return {
            "remaining": self._last_remaining,
            "reset_at": self._last_reset_at,
            "reserve": self._rate_limit_reserve,
            "blocked_until": blocked_until if blocked_until > self._wall_clock() else 0.0,
        }

    def _update_rate_limit(self, token_key: bytes, headers: Any) -> None:
        remaining = self._parse_int_header(headers, "X-RateLimit-Remaining")
        reset_at = self._parse_int_header(headers, "X-RateLimit-Reset")
        if remaining is not None:
            self._last_remaining = remaining
        if reset_at is not None:
            self._last_reset_at = float(reset_at)
        if (
            remaining is not None
            and remaining <= self._rate_limit_reserve
            and reset_at is not None
            and reset_at > self._wall_clock()
        ):
            retry_at = float(reset_at + 1)
            reason = f"GitHub API reserve reached ({remaining} remaining)"
            self._block_token(
                token_key,
                retry_at,
                reason,
            )
            raise GitHubRateLimitError(retry_at, reason)

    def _rate_limit_retry(
        self,
        token_key: bytes,
        error: urllib.error.HTTPError,
        body: str,
    ) -> tuple[float, str]:
        now = self._wall_clock()
        retry_after = self._parse_int_header(error.headers, "Retry-After")
        reset_at = self._parse_int_header(error.headers, "X-RateLimit-Reset")
        remaining = self._parse_int_header(error.headers, "X-RateLimit-Remaining")

        if retry_after is not None:
            return now + retry_after, f"GitHub requested retry after {retry_after}s"
        if remaining == 0 and reset_at is not None:
            return float(reset_at + 1), f"GitHub primary rate limit exhausted: {body[:120]}"

        failures = self._secondary_failures.get(token_key, 0) + 1
        self._secondary_failures[token_key] = failures
        delay = min(
            SECONDARY_RATE_LIMIT_BASE_SECONDS * (2 ** (failures - 1)),
            SECONDARY_RATE_LIMIT_MAX_SECONDS,
        )
        return now + delay, f"GitHub secondary rate limit; backing off {delay}s"

    @staticmethod
    def _is_rate_limit_error(error: urllib.error.HTTPError, body: str) -> bool:
        remaining = error.headers.get("X-RateLimit-Remaining")
        return (
            error.code == 429
            or remaining == "0"
            or "rate limit" in body.lower()
            or error.headers.get("Retry-After") is not None
        )

    def _block_token(self, token_key: bytes, retry_at: float, reason: str) -> None:
        previous = self._blocked_until.get(token_key, 0.0)
        self._blocked_until[token_key] = max(previous, retry_at)
        self._blocked_until_any = max(self._blocked_until_any, retry_at)
        if retry_at > previous:
            wait_seconds = max(1, int(retry_at - self._wall_clock()))
            log.warning("%s; pausing GitHub requests for %ds", reason, wait_seconds)

    def _cache_conditional_response(
        self,
        cache_key: tuple[bytes, str],
        etag: str | None,
        data: Any,
    ) -> None:
        if not etag:
            self._conditional_cache.pop(cache_key, None)
            return
        self._conditional_cache[cache_key] = ConditionalResponse(etag=etag, data=data)
        self._conditional_cache.move_to_end(cache_key)
        while len(self._conditional_cache) > MAX_CONDITIONAL_CACHE_ENTRIES:
            self._conditional_cache.popitem(last=False)

    @staticmethod
    def _parse_int_header(headers: Any, name: str) -> int | None:
        value = headers.get(name)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _get(self, pat: str, path: str) -> Any | None:
        return self._request(pat, path, "GET")

    def _delete(self, pat: str, path: str) -> bool | None:
        return self._request(pat, path, "DELETE")

    def list_repositories(self, pool: PoolConfig) -> list[str] | None:
        """List accessible, active repositories owned by a personal account."""
        repositories: set[str] = set()
        skipped_public: set[str] = set()
        excluded: set[str] = set()
        page = 1

        while True:
            data = self._get(
                pool.pat,
                f"/user/repos?affiliation=owner&visibility=all&per_page=100&page={page}",
            )
            if not isinstance(data, list):
                return None

            for repo in data:
                owner = repo.get("owner", {}).get("login", "")
                name = repo.get("name", "")
                if owner.lower() != pool.owner.lower() or not name:
                    continue
                if repo.get("archived") or repo.get("disabled"):
                    continue
                # A self-hosted runner on a public repo is code execution for
                # anyone who can open a fork PR, on the host holding the Docker
                # socket. Skipped unless the pool explicitly opts in.
                if not repo.get("private", True) and not pool.include_public_repos:
                    skipped_public.add(name)
                    continue
                if pool.excludes_repo(name):
                    excluded.add(name)
                    continue
                repositories.add(name)

            if len(data) < 100:
                break
            page += 1

        if skipped_public:
            log.info(
                "[%s] Skipping %d public repositor%s (set include_public_repos to override): %s",
                pool.name,
                len(skipped_public),
                "y" if len(skipped_public) == 1 else "ies",
                ", ".join(sorted(skipped_public)),
            )
        if excluded:
            log.info(
                "[%s] Excluded %d repositor%s by exclude_repos: %s",
                pool.name,
                len(excluded),
                "y" if len(excluded) == 1 else "ies",
                ", ".join(sorted(excluded)),
            )
        return sorted(repositories)

    def is_public_repo(self, pool: PoolConfig) -> bool | None:
        """Whether a repo-level pool points at a public repository.

        None when it cannot be determined (API error), so callers can stay
        quiet rather than warn on a transient failure.
        """
        if not pool.repo:
            return None
        data = self._get(pool.pat, f"/repos/{pool.owner}/{pool.repo}")
        if not isinstance(data, dict) or "private" not in data:
            return None
        return not data["private"]

    def get_queued_jobs_for_repo(
        self,
        pat: str,
        owner: str,
        repo: str,
        jobs: list[JobInfo] | None = None,
    ) -> int:
        """Count jobs waiting for a runner in a single repo.

        When `jobs` is supplied, every non-queued job seen along the way is
        appended to it. That is what powers "which runner ran what" — this walk
        already fetches the full job payload to count the queued ones, so
        recording the rest costs no extra API requests.
        """
        total = 0
        for status in ("queued", "in_progress"):
            runs_data = self._get(
                pat, f"/repos/{owner}/{repo}/actions/runs?status={status}&per_page=50"
            )
            if not runs_data:
                continue
            for run in runs_data.get("workflow_runs", []):
                run_id = run["id"]
                jobs_data = self._get(
                    pat,
                    f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=50",
                )
                if not jobs_data:
                    continue
                for job in jobs_data.get("jobs", []):
                    if job.get("status") == "queued":
                        total += 1
                    elif jobs is not None:
                        jobs.append(_job_info(job, run, repo))
        return total

    def get_queued_count(self, pool: PoolConfig, jobs: list[JobInfo] | None = None) -> int:
        """Count all queued jobs for a pool (single repo or entire org).

        Pass `jobs` to also collect the running/finished jobs seen on the way.
        """
        if pool.repo:
            return self.get_queued_jobs_for_repo(pool.pat, pool.owner, pool.repo, jobs)

        if pool.is_personal_level:
            repositories = self.list_repositories(pool) or []
            return sum(
                self.get_queued_jobs_for_repo(pool.pat, pool.owner, repo, jobs)
                for repo in repositories
            )

        repos = self._get(pool.pat, f"/orgs/{pool.owner}/repos?per_page=100&type=all")
        if not repos or not isinstance(repos, list):
            return 0

        total = 0
        for repo in repos:
            total += self.get_queued_jobs_for_repo(pool.pat, pool.owner, repo["name"], jobs)
        return total

    def list_runners(self, pool: PoolConfig) -> list[RunnerInfo] | None:
        """Fetch runner state once for cleanup and scaling decisions."""
        data = self._get(pool.pat, pool.api_runners_path)
        if not isinstance(data, dict):
            return None

        raw_runners = data.get("runners")
        if not isinstance(raw_runners, list):
            return None

        runners: list[RunnerInfo] = []
        for runner in raw_runners:
            if not isinstance(runner, dict):
                continue
            runner_id = runner.get("id")
            name = runner.get("name")
            status = runner.get("status")
            if not isinstance(runner_id, int) or not isinstance(name, str):
                continue
            if not isinstance(status, str):
                status = "unknown"
            runners.append(
                RunnerInfo(
                    id=runner_id,
                    name=name,
                    status=status,
                    busy=runner.get("busy") is True,
                )
            )
        return runners

    def deregister_runner(self, pool: PoolConfig, runner_id: int) -> bool:
        """Delete one runner registration."""
        return self._delete(pool.pat, f"{pool.api_runners_path}/{runner_id}") is True
