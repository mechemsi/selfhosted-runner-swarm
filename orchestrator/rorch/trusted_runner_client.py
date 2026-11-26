# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""GitHub runner-group inventory for main-only pools."""

from rorch.config import PoolConfig
from rorch.github_client import GitHubClient, _main_group_path


class TrustedRunnerClient(GitHubClient):
    """Require a consistent inventory before inspecting a main-only pool."""

    def _list_main_group_runners(self, pool: PoolConfig) -> list[object] | None:
        page_size = 100
        path = f"{_main_group_path(pool)}/runners?per_page={page_size}"
        runners: list[object] = []
        seen_ids: set[int] = set()
        expected_total: int | None = None
        page = 1
        while True:
            data = self._get(pool.pat, f"{path}&page={page}")
            if not isinstance(data, dict):
                return None
            entries = data.get("runners")
            total = data.get("total_count")
            if not isinstance(entries, list) or type(total) is not int:
                return None
            if expected_total is None:
                expected_total = total
            if total != expected_total or not _add_runner_ids(entries, seen_ids):
                return None
            runners.extend(entries)
            if len(runners) == expected_total:
                return runners
            if len(entries) != page_size or len(runners) > expected_total:
                return None
            page += 1


def _add_runner_ids(entries: list[object], seen_ids: set[int]) -> bool:
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            return False
        runner_id = entry.get("id")
        if type(runner_id) is not int or runner_id in seen_ids:
            return False
        seen_ids.add(runner_id)
    return True
