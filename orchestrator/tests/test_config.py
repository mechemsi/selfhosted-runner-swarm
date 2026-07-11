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
    def test_is_org_level(
        self, pool: PoolConfig, org_pool: PoolConfig, personal_pool: PoolConfig
    ) -> None:
        assert not pool.is_org_level
        assert org_pool.is_org_level
        assert not personal_pool.is_org_level
        assert personal_pool.is_personal_level

    def test_display(self, pool: PoolConfig, org_pool: PoolConfig) -> None:
        assert pool.display == "test-org/test-repo"
        assert org_pool.display == "test-org [org]"

    def test_personal_display(self, personal_pool: PoolConfig) -> None:
        assert personal_pool.display == "test-user [personal: all repos]"

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

    def test_for_repository(self, personal_pool: PoolConfig) -> None:
        repo_pool = personal_pool.for_repository("project-one")

        assert repo_pool.name == "personal-pool-project-one"
        assert repo_pool.repo == "project-one"
        assert repo_pool.scope == "repository"
        assert repo_pool.min_idle == 0
        assert repo_pool.max_runners == personal_pool.max_runners


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

    def test_personal_env_scope_defaults_to_zero_idle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_PAT", "env-token")
        monkeypatch.setenv("GITHUB_OWNER", "env-owner")
        monkeypatch.setenv("GITHUB_SCOPE", "personal")

        pools = load_config("/nonexistent/config.yml")

        assert pools[0].is_personal_level
        assert pools[0].min_idle == 0

    def test_loads_personal_scope_with_zero_idle_default(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pathlib

        config = textwrap.dedent("""\
            defaults:
              min_idle: 2
            pools:
              - name: personal
                owner: account-owner
                scope: personal
                pat: direct-token
                repo_discovery_ttl: 900
        """)
        config_path = pathlib.Path(str(tmp_path)) / "config.yml"
        config_path.write_text(config)

        pools = load_config(str(config_path))

        assert pools[0].scope == "personal"
        assert pools[0].min_idle == 0
        assert pools[0].repo_discovery_ttl == 900


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

    def test_unknown_scope_exits(self, pool: PoolConfig) -> None:
        pool.scope = "account"
        with pytest.raises(SystemExit):
            validate_pools([pool])

    def test_personal_scope_with_repo_exits(self, pool: PoolConfig) -> None:
        pool.scope = "personal"
        with pytest.raises(SystemExit):
            validate_pools([pool])

    def test_repository_scope_without_repo_exits(self, org_pool: PoolConfig) -> None:
        org_pool.scope = "repository"
        with pytest.raises(SystemExit):
            validate_pools([org_pool])

    def test_negative_repo_discovery_ttl_exits(self, personal_pool: PoolConfig) -> None:
        personal_pool.repo_discovery_ttl = -1
        with pytest.raises(SystemExit):
            validate_pools([personal_pool])
