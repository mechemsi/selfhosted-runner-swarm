"""GitHub API client for runner management."""

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from rorch.config import PoolConfig

log = logging.getLogger(__name__)


class GitHubClient:
    """Handles all GitHub REST API communication for runner orchestration."""

    API_BASE = "https://api.github.com"
    API_VERSION = "2022-11-28"
    TIMEOUT = 10

    def _request(self, pat: str, path: str, method: str = "GET") -> Any | None:
        url = f"{self.API_BASE}{path}"
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"Bearer {pat}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", self.API_VERSION)
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                if method == "DELETE":
                    return resp.status == 204
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            log.error("GitHub API %s %d: %s — %s", method, e.code, path, body[:200])
            return None
        except Exception as e:
            log.error("GitHub %s request failed: %s", method, e)
            return None

    def _get(self, pat: str, path: str) -> Any | None:
        return self._request(pat, path, "GET")

    def _delete(self, pat: str, path: str) -> bool | None:
        return self._request(pat, path, "DELETE")

    def get_queued_jobs_for_repo(self, pat: str, owner: str, repo: str) -> int:
        """Count jobs waiting for a runner in a single repo."""
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
        return total

    def get_queued_count(self, pool: PoolConfig) -> int:
        """Count all queued jobs for a pool (single repo or entire org)."""
        if pool.repo:
            return self.get_queued_jobs_for_repo(pool.pat, pool.owner, pool.repo)

        repos = self._get(pool.pat, f"/orgs/{pool.owner}/repos?per_page=100&type=all")
        if not repos:
            repos = self._get(pool.pat, f"/users/{pool.owner}/repos?per_page=100&type=owner")
        if not repos or not isinstance(repos, list):
            return 0

        total = 0
        for repo in repos:
            total += self.get_queued_jobs_for_repo(pool.pat, pool.owner, repo["name"])
        return total

    def get_runner_stats(self, pool: PoolConfig) -> tuple[int, int]:
        """Return (idle, busy) counts of online runners."""
        data = self._get(pool.pat, pool.api_runners_path)
        if not data:
            return 0, 0
        runners = data.get("runners", [])
        online = [r for r in runners if r.get("status") == "online"]
        busy = sum(1 for r in online if r.get("busy") is True)
        idle = sum(1 for r in online if r.get("busy") is False)
        return idle, busy

    def get_online_runner_names(self, pool: PoolConfig) -> set[str]:
        """Get names of all online runners for this pool."""
        data = self._get(pool.pat, pool.api_runners_path)
        if not data:
            return set()
        return {r["name"] for r in data.get("runners", []) if r.get("status") == "online"}

    def deregister_offline_runners(self, pool: PoolConfig, running_names: set[str]) -> None:
        """Remove offline runner registrations that have no running container."""
        data = self._get(pool.pat, pool.api_runners_path)
        if not data:
            return

        offline = [r for r in data.get("runners", []) if r.get("status") == "offline"]
        for runner in offline:
            name = runner.get("name", "")
            rid = runner.get("id")

            if not name.startswith(pool.container_prefix):
                continue
            if name in running_names:
                continue

            log.info("  🧹  Deregistering offline runner: %s (id=%s)", name, rid)
            result = self._delete(pool.pat, f"{pool.api_runners_path}/{rid}")
            if result is not None:
                log.info("  ✓  Deregistered %s", name)
            else:
                log.warning("  ✗  Failed to deregister %s", name)
