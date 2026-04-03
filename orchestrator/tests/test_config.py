# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Tests for configuration loading and validation."""

import textwrap

import pytest

from rorch.config import PoolConfig, load_config, resolve_env, validate_pools


class TestResolveEnv:
    def test_empty_string(self) -> None:
        assert resolve_env("") == ""

    def test_plain_value(self) -> None:
        assert resolve_env("hello") == "hello"

    def test_env_var_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_TOKEN", "secret123")
        assert resolve_env("${MY_TOKEN}") == "secret123"

    def test_missing_env_var(self) -> None:
        assert resolve_env("${NONEXISTENT_VAR_12345}") == ""

    def test_partial_syntax_not_expanded(self) -> None:
        assert resolve_env("${PARTIAL") == "${PARTIAL"
        assert resolve_env("PARTIAL}") == "PARTIAL}"


class TestPoolConfig:
    def test_is_org_level(self, pool: PoolConfig, org_pool: PoolConfig) -> None:
        assert not pool.is_org_level
        assert org_pool.is_org_level

    def test_display(self, pool: PoolConfig, org_pool: PoolConfig) -> None:
        assert pool.display == "test-org/test-repo"
        assert org_pool.display == "test-org [org]"

    def test_container_prefix(self, pool: PoolConfig) -> None:
        assert pool.container_prefix == "gh-runner-test-pool"

    def test_container_prefix_sanitizes(self) -> None:
        p = PoolConfig(name="My Pool/Test", pat="x", owner="o")
        assert p.container_prefix == "gh-runner-my-pool-test"

    def test_api_runners_path_repo(self, pool: PoolConfig) -> None:
        assert pool.api_runners_path == "/repos/test-org/test-repo/actions/runners"

    def test_api_runners_path_org(self, org_pool: PoolConfig) -> None:
        assert org_pool.api_runners_path == "/orgs/test-org/actions/runners"

    def test_registration_url_repo(self, pool: PoolConfig) -> None:
        assert pool.registration_url == "https://github.com/test-org/test-repo"

    def test_registration_url_org(self, org_pool: PoolConfig) -> None:
        assert org_pool.registration_url == "https://github.com/test-org"


class TestLoadConfig:
    def test_loads_from_yaml(self, tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
        import pathlib

        config = textwrap.dedent("""\
            defaults:
              runner_image: my-runner:latest
              max_runners: 5
            pools:
              - name: my-pool
                owner: my-org
                repo: my-repo
                pat: direct-token
        """)
        config_path = pathlib.Path(str(tmp_path)) / "config.yml"
        config_path.write_text(config)

        pools = load_config(str(config_path))
        assert len(pools) == 1
        assert pools[0].name == "my-pool"
        assert pools[0].pat == "direct-token"
        assert pools[0].runner_image == "my-runner:latest"
        assert pools[0].max_runners == 5

    def test_falls_back_to_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_PAT", "env-token")
        monkeypatch.setenv("GITHUB_OWNER", "env-owner")
        pools = load_config("/nonexistent/config.yml")
        assert len(pools) == 1
        assert pools[0].name == "default"
        assert pools[0].pat == "env-token"
        assert pools[0].owner == "env-owner"


class TestValidatePools:
    def test_valid_pool_passes(self, pool: PoolConfig) -> None:
        validate_pools([pool])  # should not exit

    def test_missing_pat_exits(self, pool: PoolConfig) -> None:
        pool.pat = ""
        with pytest.raises(SystemExit):
            validate_pools([pool])

    def test_unresolved_pat_exits(self, pool: PoolConfig) -> None:
        pool.pat = "${UNRESOLVED}"
        with pytest.raises(SystemExit):
            validate_pools([pool])

    def test_missing_owner_exits(self, pool: PoolConfig) -> None:
        pool.owner = ""
        with pytest.raises(SystemExit):
            validate_pools([pool])
