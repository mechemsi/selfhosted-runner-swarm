# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Tests for config.yml → database override resolution."""

from pathlib import Path

import pytest

from rorch.config import PoolConfig
from rorch.resolver import ConfigResolver, pool_to_dict
from rorch.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(str(tmp_path / "rorch.db"))


@pytest.fixture
def resolver(pool: PoolConfig, store: Store) -> ConfigResolver:
    return ConfigResolver(
        [pool], base_max_total_runners=10, base_max_runner_lifetime=0, store=store
    )


class TestWithoutStore:
    def test_returns_yaml_config_unchanged(self, pool: PoolConfig) -> None:
        resolver = ConfigResolver([pool], 7, 90, store=None)
        effective = resolver.resolve()

        assert effective.pools == [pool]
        assert effective.max_total_runners == 7
        assert effective.max_runner_lifetime == 90
        assert effective.paused is False
        assert effective.protected == frozenset()


class TestOverrides:
    def test_empty_database_matches_yaml(self, resolver: ConfigResolver, pool: PoolConfig) -> None:
        effective = resolver.resolve()
        assert effective.pools == [pool]
        assert effective.max_total_runners == 10

    def test_override_replaces_single_field(
        self, resolver: ConfigResolver, store: Store, pool: PoolConfig
    ) -> None:
        store.set_pool_override(pool.name, {"max_runners": 42})
        resolved = resolver.resolve().pools[0]

        assert resolved.max_runners == 42
        # Everything else still comes from config.yml.
        assert resolved.min_idle == pool.min_idle
        assert resolved.owner == pool.owner

    def test_deleting_override_reverts_to_yaml(
        self, resolver: ConfigResolver, store: Store, pool: PoolConfig
    ) -> None:
        store.set_pool_override(pool.name, {"max_runners": 42})
        store.delete_pool_override(pool.name)
        assert resolver.resolve().pools[0].max_runners == pool.max_runners

    def test_identity_fields_are_never_overridable(
        self, resolver: ConfigResolver, store: Store, pool: PoolConfig
    ) -> None:
        store.set_pool_override(pool.name, {"owner": "attacker", "pat": "leaked"})
        resolved = resolver.resolve().pools[0]

        assert resolved.owner == pool.owner
        assert resolved.pat == pool.pat

    def test_disabled_pool_is_dropped(
        self, resolver: ConfigResolver, store: Store, pool: PoolConfig
    ) -> None:
        store.set_pool_override(pool.name, {}, disabled=True)
        assert resolver.resolve().pools == []

    def test_string_values_are_coerced(
        self, resolver: ConfigResolver, store: Store, pool: PoolConfig
    ) -> None:
        store.set_pool_override(pool.name, {"max_runners": "8", "cpu_limit": "1.5"})
        resolved = resolver.resolve().pools[0]

        assert resolved.max_runners == 8
        assert resolved.cpu_limit == 1.5


class TestGlobals:
    def test_global_override_applies(self, resolver: ConfigResolver, store: Store) -> None:
        store.set_global("max_total_runners", "3")
        store.set_global("max_runner_lifetime", "120")
        effective = resolver.resolve()

        assert effective.max_total_runners == 3
        assert effective.max_runner_lifetime == 120

    def test_paused_flag(self, resolver: ConfigResolver, store: Store) -> None:
        store.set_global("paused", "1")
        assert resolver.resolve().paused is True

    def test_unparseable_global_falls_back_to_yaml(
        self, resolver: ConfigResolver, store: Store
    ) -> None:
        store.set_global("max_total_runners", "not-a-number")
        assert resolver.resolve().max_total_runners == 10


class TestUiCreatedPools:
    def test_pool_from_database_uses_env_pat(
        self,
        resolver: ConfigResolver,
        store: Store,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("EXTRA_PAT", "ghp_from_environment")
        store.set_pool_override(
            "extra",
            {"owner": "acme", "repo": "widgets", "pat_env": "EXTRA_PAT", "max_runners": 2},
            origin="ui",
        )
        created = next(p for p in resolver.resolve().pools if p.name == "extra")

        assert created.pat == "ghp_from_environment"
        assert created.owner == "acme"
        assert created.max_runners == 2

    def test_pool_skipped_when_pat_env_missing(
        self,
        resolver: ConfigResolver,
        store: Store,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("MISSING_PAT", raising=False)
        store.set_pool_override("extra", {"owner": "acme", "pat_env": "MISSING_PAT"}, origin="ui")
        assert [p.name for p in resolver.resolve().pools] == ["test-pool"]

    def test_invalid_pool_definition_is_skipped(
        self,
        resolver: ConfigResolver,
        store: Store,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("EXTRA_PAT", "ghp_from_environment")
        store.set_pool_override(
            "extra",
            {"owner": "", "pat_env": "EXTRA_PAT"},  # owner is required
            origin="ui",
        )
        assert [p.name for p in resolver.resolve().pools] == ["test-pool"]


class TestSerialisation:
    def test_pool_to_dict_never_exposes_the_pat(self, pool: PoolConfig) -> None:
        data = pool_to_dict(pool)
        assert "pat" not in data
        assert pool.pat not in str(data)
        assert data["display"] == pool.display
