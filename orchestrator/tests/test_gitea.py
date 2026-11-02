# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Gitea pools: config, API routing, job detection and runner spawn."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from rorch.config import PoolConfig, _load_from_yaml, validation_errors
from rorch.docker_client import DockerClient
from rorch.gitea_client import ForgeClients, GiteaClient
from rorch.github_client import GitHubClient
from rorch.resolver import IDENTITY_FIELDS, TUNABLE_FIELDS

URL = "http://gitea.lan:3000"


def _pool(**overrides: Any) -> PoolConfig:
    fields: dict[str, Any] = {
        "name": "gitea",
        "pat": "gitea-token",
        "owner": "me",
        "repo": "app",
        "scope": "repository",
        "provider": "gitea",
        "base_url": URL,
        "runner_labels": "self-hosted,gitea",
    }
    return PoolConfig(**(fields | overrides))


class _Response:
    status = 200

    def __init__(self, body: Any) -> None:
        self.headers: dict[str, str] = {}
        self._body = json.dumps(body).encode()

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class TestConfig:
    def test_never_inherits_the_global_github_token(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yml"
        path.write_text(
            "defaults: {pat: github-token}\n"
            f"pools:\n  - {{name: g, owner: me, provider: gitea, base_url: '{URL}/'}}\n"
            "  - {name: h, owner: me}\n"
        )
        gitea, github = _load_from_yaml(str(path))
        assert gitea.pat == ""
        assert gitea.base_url == URL
        assert github.pat == "github-token"
        assert any("pat is missing" in e for e in validation_errors([gitea]))

    def test_github_pools_stay_the_default(self) -> None:
        pool = PoolConfig(name="p", pat="t", owner="o")
        assert not pool.is_gitea
        assert validation_errors([pool]) == []

    def test_gitea_needs_a_url(self) -> None:
        assert any("needs base_url" in e for e in validation_errors([_pool(base_url="")]))
        assert validation_errors([_pool()]) == []

    def test_rejects_unknown_provider_and_stray_base_url(self) -> None:
        assert any("unsupported provider" in e for e in validation_errors([_pool(provider="x")]))
        github = PoolConfig(name="p", pat="t", owner="o", base_url=URL)
        assert any("only applies" in e for e in validation_errors([github]))

    def test_labels_are_only_the_listed_ones(self) -> None:
        pool = _pool()
        assert pool.label_set == {"self-hosted", "gitea"}
        assert pool.serves_labels(["gitea"])
        # A GitHub pool would claim linux because config.sh adds it; this runner has no such label.
        assert not pool.serves_labels(["linux"])


class TestGiteaClient:
    def test_calls_the_configured_server_with_gitea_paging(self) -> None:
        client = GiteaClient(f"{URL}/")
        with patch("urllib.request.urlopen", return_value=_Response({"workflow_runs": []})) as call:
            assert client.get_queued_count(_pool()) == 0
        urls = [c.args[0].full_url for c in call.call_args_list]
        assert urls
        assert all(u.startswith(f"{URL}/api/v1/repos/me/app/actions/runs?") for u in urls)
        assert all("limit=50" in u and "per_page" not in u for u in urls)
        assert GitHubClient.API_BASE == "https://api.github.com"

    def test_discovery_reads_every_page_of_gitea_sized_pages(self) -> None:
        client = GiteaClient(URL)
        page = [{"name": f"r{i}", "owner": {"login": "me"}, "private": True} for i in range(50)]
        client._get = MagicMock(side_effect=[page, page[:1]])  # type: ignore[method-assign]
        pool = _pool(repo="", scope="personal")
        assert client.list_repositories(pool) is not None
        assert client._get.call_count == 2

    def test_counts_a_queued_job_for_this_pools_labels_only(self) -> None:
        client = GiteaClient(URL)

        def fake(_pat: str, path: str) -> Any:
            if "/jobs" in path:
                return {
                    "jobs": [
                        {"id": 1, "status": "queued", "labels": ["gitea"]},
                        {"id": 2, "status": "queued", "labels": ["macos"]},
                    ]
                }
            return {"workflow_runs": [{"id": 9}] if "status=queued" in path else []}

        client._get = MagicMock(side_effect=fake)  # type: ignore[method-assign]
        client._get_shared = MagicMock(side_effect=fake)  # type: ignore[method-assign]
        assert client.get_queued_count(_pool()) == 1


class TestForgeClients:
    def test_routes_by_pool_and_reuses_one_client_per_server(self) -> None:
        github = MagicMock(spec=GitHubClient)
        forge = ForgeClients(github)
        plain = PoolConfig(name="p", pat="t", owner="o")
        forge.list_runners(plain)
        github.list_runners.assert_called_once_with(plain)

        gitea = _pool()
        assert forge._for(gitea) is forge._for(_pool(name="other"))
        assert isinstance(forge._for(gitea), GiteaClient)
        github.list_runners.reset_mock()
        with patch.object(GiteaClient, "list_runners", return_value=[]) as listed:
            forge.list_runners(gitea)
        listed.assert_called_once_with(gitea)
        github.list_runners.assert_not_called()


IDLE = "UID PID PPID C STIME TTY TIME CMD\n1000 10 1 0 00:00 ? 00:00 gitea-runner daemon --config c"
BUSY = IDLE + "\n1000 11 10 0 00:00 ? 00:00 bash /tmp/step.sh"
LISTENER = "UID PID PPID C STIME TTY TIME CMD\n1000 10 1 0 00:00 ? 00:00 bin/Runner.Listener run"


class TestJobDetection:
    @pytest.mark.parametrize(
        ("top", "expected"),
        [
            (IDLE, False),
            (BUSY, True),
            # A GitHub listener with no worker is idle, however many processes sit beside it.
            (LISTENER + "\n1000 11 10 0 00:00 ? 00:00 bash run.sh", False),
            (LISTENER + "\n1000 12 10 0 00:00 ? 00:00 bin/Runner.Worker spawn", True),
        ],
    )
    def test_has_running_job(self, top: str, expected: bool) -> None:
        client = DockerClient()
        client._capture = MagicMock(return_value=(top, 0))  # type: ignore[method-assign]
        assert client.has_running_job("c") is expected


class TestAccountTokenStaysOut:
    def test_api_cannot_set_the_server_a_token_goes_to(self) -> None:
        assert not {"provider", "base_url"} & (IDENTITY_FIELDS | TUNABLE_FIELDS)

    def test_registration_token_is_requested_from_the_pools_repo(self) -> None:
        client = GiteaClient(URL)
        with patch("urllib.request.urlopen", return_value=_Response({"token": "reg"})) as call:
            assert client.registration_token(_pool()) == "reg"
        request = call.call_args.args[0]
        assert request.method == "POST"
        assert request.full_url == f"{URL}/api/v1/repos/me/app/actions/runners/registration-token"

    def _spawn(self, pool: PoolConfig, token: str | None) -> tuple[bool, list[str]]:
        client = DockerClient(registration_token=lambda _pool: token)
        client.ensure_image = MagicMock(return_value=True)  # type: ignore[method-assign]
        seen: list[str] = []

        def run(args: list[str]) -> int:
            seen.extend(args)
            return 0

        client._exec = MagicMock(side_effect=run)  # type: ignore[method-assign]
        return client.spawn_runner(pool), seen

    def test_spawn_passes_the_registration_token_and_never_the_account_token(self) -> None:
        ok, args = self._spawn(_pool(), "reg")
        assert ok
        assert "GITEA_RUNNER_REGISTRATION_TOKEN=reg" in args
        assert f"GITEA_URL={URL}" in args
        assert not any("gitea-token" in arg or arg.startswith("GITHUB_PAT=") for arg in args)

    def test_spawn_fails_without_a_token(self) -> None:
        ok, args = self._spawn(_pool(), None)
        assert not ok
        assert args == []

    def test_github_spawn_still_passes_its_pat(self) -> None:
        ok, args = self._spawn(PoolConfig(name="p", pat="gh", owner="o", repo="r"), None)
        assert ok
        assert "GITHUB_PAT=gh" in args
        assert not any(arg.startswith("GITEA_") for arg in args)
