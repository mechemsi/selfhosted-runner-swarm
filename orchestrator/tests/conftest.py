# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Shared test fixtures."""

import pytest

from rorch.config import PoolConfig


@pytest.fixture
def pool() -> PoolConfig:
    """A minimal pool config for testing."""
    return PoolConfig(
        name="test-pool",
        pat="ghp_test_token_1234567890",
        owner="test-org",
        repo="test-repo",
        max_runners=5,
        min_idle=1,
    )


@pytest.fixture
def org_pool() -> PoolConfig:
    """An org-level pool config (no repo)."""
    return PoolConfig(
        name="org-pool",
        pat="ghp_test_token_1234567890",
        owner="test-org",
        repo="",
        max_runners=3,
        min_idle=2,
    )
