# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Tests for the SQLite state store."""

import time
from pathlib import Path

import pytest

from rorch.store import EVENT_SPAWN, EVENT_STUCK_KILL, SqliteStore, open_store


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(str(tmp_path / "rorch.db"))


class TestSchema:
    def test_init_is_idempotent(self, tmp_path: Path) -> None:
        path = str(tmp_path / "rorch.db")
        first = SqliteStore(path)
        first.record_event(EVENT_SPAWN, container="gh-runner-a-1", pool="a")
        first.close()

        second = SqliteStore(path)
        assert len(second.recent_events()) == 1

    def test_open_store_creates_parent_directory(self, tmp_path: Path) -> None:
        opened = open_store(str(tmp_path / "nested" / "deep" / "rorch.db"))
        assert opened is not None
        assert (tmp_path / "nested" / "deep" / "rorch.db").exists()

    def test_open_store_returns_none_on_unusable_path(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        assert open_store(str(blocker / "sub" / "rorch.db")) is None


class TestHistory:
    def test_records_and_reads_ticks(self, store: SqliteStore) -> None:
        store.record_tick("pool-a", "org/repo", 2, 2, 1, 1, 3, 0.5)
        latest = store.latest_snapshots()
        assert latest["pool-a"]["containers"] == 2
        assert latest["pool-a"]["queued"] == 3

    def test_latest_snapshot_wins_per_pool(self, store: SqliteStore) -> None:
        store.record_tick("pool-a", "org/repo", 1, 1, 1, 0, 0, 0.1)
        store.record_tick("pool-a", "org/repo", 5, 5, 0, 5, 2, 0.2)
        assert store.latest_snapshots()["pool-a"]["containers"] == 5

    def test_event_counts_group_by_event(self, store: SqliteStore) -> None:
        store.record_event(EVENT_SPAWN, pool="a")
        store.record_event(EVENT_SPAWN, pool="a")
        store.record_event(EVENT_STUCK_KILL, pool="a")
        assert store.event_counts_since(1) == {EVENT_SPAWN: 2, EVENT_STUCK_KILL: 1}

    def test_snapshots_since_excludes_older_rows(self, store: SqliteStore) -> None:
        store.record_tick("pool-a", "org/repo", 1, 1, 1, 0, 0, 0.1)
        store._write(  # pyright: ignore[reportPrivateUsage]
            "UPDATE tick_snapshots SET ts = ?", (time.time() - 7200,)
        )
        store.record_tick("pool-a", "org/repo", 9, 9, 9, 0, 0, 0.1)
        recent = store.snapshots_since(0.5)
        assert [row["containers"] for row in recent] == [9]


class TestRetention:
    def test_prune_drops_only_old_rows(self, store: SqliteStore) -> None:
        store.record_event(EVENT_SPAWN, pool="old")
        store._write(  # pyright: ignore[reportPrivateUsage]
            "UPDATE runner_events SET ts = ?", (time.time() - 40 * 86400,)
        )
        store.record_event(EVENT_SPAWN, pool="new")

        assert store.prune(30) == 1
        remaining = store.recent_events()
        assert [row["pool"] for row in remaining] == ["new"]

    def test_prune_disabled_at_zero_days(self, store: SqliteStore) -> None:
        store.record_event(EVENT_SPAWN, pool="a")
        assert store.prune(0) == 0
        assert len(store.recent_events()) == 1


class TestOverrides:
    def test_roundtrip_pool_override(self, store: SqliteStore) -> None:
        store.set_pool_override("pool-a", {"max_runners": 9})
        assert store.pool_overrides()["pool-a"]["data"] == {"max_runners": 9}

    def test_override_upsert_replaces_previous(self, store: SqliteStore) -> None:
        store.set_pool_override("pool-a", {"max_runners": 9})
        store.set_pool_override("pool-a", {"max_runners": 2})
        assert store.pool_overrides()["pool-a"]["data"] == {"max_runners": 2}

    def test_delete_override_removes_row(self, store: SqliteStore) -> None:
        store.set_pool_override("pool-a", {"max_runners": 9})
        store.delete_pool_override("pool-a")
        assert store.pool_overrides() == {}

    def test_globals_roundtrip(self, store: SqliteStore) -> None:
        store.set_global("max_total_runners", "12")
        assert store.global_overrides()["max_total_runners"] == "12"
        store.delete_global("max_total_runners")
        assert store.global_overrides() == {}


class TestControlState:
    def test_pool_state_roundtrip(self, store: SqliteStore) -> None:
        store.set_pool_state("pool-a", paused=True, draining=False)
        state = store.pool_states()["pool-a"]
        assert state.paused is True
        assert state.draining is False

    def test_protected_containers_toggle(self, store: SqliteStore) -> None:
        store.set_protected("gh-runner-a-1", True)
        assert store.protected_containers() == frozenset({"gh-runner-a-1"})
        store.set_protected("gh-runner-a-1", False)
        assert store.protected_containers() == frozenset()

    def test_runner_status_replaced_per_pool(self, store: SqliteStore) -> None:
        store.replace_runner_status("a", [("gh-runner-a-1", "online", True)])
        store.replace_runner_status("b", [("gh-runner-b-1", "online", False)])
        store.replace_runner_status("a", [("gh-runner-a-2", "online", False)])

        status = store.runner_status()
        assert "gh-runner-a-1" not in status
        assert status["gh-runner-a-2"]["busy"] is False
        assert status["gh-runner-b-1"]["pool"] == "b"


class TestIdempotency:
    def test_remembers_and_replays(self, store: SqliteStore) -> None:
        assert store.idempotent_response("k") is None
        store.remember_response("k", '{"ok": true}')
        assert store.idempotent_response("k") == '{"ok": true}'

    def test_prune_drops_expired_keys(self, store: SqliteStore) -> None:
        store.remember_response("k", "{}")
        store._write(  # pyright: ignore[reportPrivateUsage]
            "UPDATE idempotency SET ts = ?", (time.time() - 48 * 3600,)
        )
        store.prune_idempotency(24)
        assert store.idempotent_response("k") is None


class TestAudit:
    def test_audit_rows_are_ordered_newest_first(self, store: SqliteStore) -> None:
        store.audit("alice", "stop", "gh-runner-a-1", "busy=False")
        store.audit("bob", "pool_state", "pool-a", "paused=True")
        rows = store.recent_audit()
        assert [row["actor"] for row in rows] == ["bob", "alice"]
