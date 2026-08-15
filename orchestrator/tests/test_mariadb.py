# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Store behaviour against a real MariaDB.

Skipped unless RORCH_TEST_DB_URL points at a live server, so the default suite
stays hermetic. CI runs these with a MariaDB service container; locally:

    docker run -d --name rorch-mariadb -p 127.0.0.1:13306:3306 \\
      -e MARIADB_ROOT_PASSWORD=rootpw -e MARIADB_DATABASE=rorch \\
      -e MARIADB_USER=rorch -e MARIADB_PASSWORD=rorchpw mariadb:11
    RORCH_TEST_DB_URL=mysql://rorch:rorchpw@127.0.0.1:13306/rorch pytest tests/test_mariadb.py

These exist because the dialect differences are exactly the kind that pass
every SQLite test and then fail on the first production write.
"""

import os

import pytest

from rorch.dialect import parse_dsn, table_names
from rorch.store import EVENT_SPAWN, Store, open_store

DB_URL = os.environ.get("RORCH_TEST_DB_URL", "")
pytestmark = pytest.mark.skipif(not DB_URL, reason="RORCH_TEST_DB_URL is not set")


@pytest.fixture
def store() -> Store:
    db = Store(DB_URL)
    # Each test starts from a clean slate; the schema itself is idempotent.
    for table in table_names(db.dialect.schema()):
        db._write(f"DELETE FROM {table}")  # pyright: ignore[reportPrivateUsage]
    return db


class TestSchema:
    def test_connects_and_creates_every_table(self, store: Store) -> None:
        assert store.is_mariadb
        rows = store._read("SHOW TABLES")  # pyright: ignore[reportPrivateUsage]
        present = {next(iter(dict(r).values())) for r in rows}
        for table in table_names(store.dialect.schema()):
            assert table in present, table

    def test_schema_is_idempotent(self) -> None:
        """A restart re-runs the DDL; it must not fail on existing tables."""
        first = Store(DB_URL)
        first.record_event(EVENT_SPAWN, container="gh-runner-a-1", pool="a")
        first.close()

        second = Store(DB_URL)
        assert any(e["container"] == "gh-runner-a-1" for e in second.recent_events())

    def test_both_dialects_declare_the_same_tables(self, store: Store) -> None:
        from rorch.dialect import Dialect

        sqlite_tables = set(table_names(Dialect(parse_dsn("/tmp/x.db")).schema()))
        assert set(table_names(store.dialect.schema())) == sqlite_tables


class TestUpserts:
    """Every ON CONFLICT became ON DUPLICATE KEY UPDATE — verify each one."""

    def test_pool_override_upsert(self, store: Store) -> None:
        store.set_pool_override("pool-a", {"max_runners": 9})
        store.set_pool_override("pool-a", {"max_runners": 2})
        assert store.pool_overrides()["pool-a"]["data"] == {"max_runners": 2}

    def test_global_override_upsert(self, store: Store) -> None:
        store.set_global("max_total_runners", "12")
        store.set_global("max_total_runners", "3")
        assert store.global_overrides()["max_total_runners"] == "3"

    def test_pool_state_upsert(self, store: Store) -> None:
        store.set_pool_state("pool-a", paused=True, draining=False)
        store.set_pool_state("pool-a", paused=False, draining=True)
        state = store.pool_states()["pool-a"]
        assert state.paused is False
        assert state.draining is True

    def test_protected_container_upsert(self, store: Store) -> None:
        store.set_protected("gh-runner-a-1", True)
        store.set_protected("gh-runner-a-1", True)
        assert store.protected_containers() == frozenset({"gh-runner-a-1"})

    def test_idempotency_upsert(self, store: Store) -> None:
        store.remember_response("k", '{"first": true}')
        store.remember_response("k", '{"second": true}')
        assert store.idempotent_response("k") == '{"second": true}'

    def test_runner_status_replaced_per_pool(self, store: Store) -> None:
        store.replace_runner_status("a", [("gh-runner-a-1", "online", True)])
        store.replace_runner_status("a", [("gh-runner-a-2", "online", False)])
        status = store.runner_status()
        assert "gh-runner-a-1" not in status
        assert status["gh-runner-a-2"]["busy"] is False


class TestJobs:
    _JOB = (101, "widgets", "CI", "build", "gh-runner-a-1", "in_progress", "", "http://j/1", "")

    def test_job_lifecycle(self, store: Store) -> None:
        started, finished = store.sync_jobs("a", [self._JOB])
        assert [j["job_id"] for j in started] == [101]
        assert finished == []

        done = (*self._JOB[:5], "completed", "success", self._JOB[7], "")
        _, finished = store.sync_jobs("a", [done])
        assert [j["conclusion"] for j in finished] == ["success"]

    def test_completion_time_is_not_overwritten(self, store: Store) -> None:
        """The COALESCE in the MariaDB upsert must behave like SQLite's."""
        done = (*self._JOB[:5], "completed", "success", self._JOB[7], "")
        store.sync_jobs("a", [done])
        first = next(j for j in store.recent_jobs() if j["job_id"] == 101)["ended_ts"]

        store.sync_jobs("a", [done])
        again = next(j for j in store.recent_jobs() if j["job_id"] == 101)["ended_ts"]
        assert first == again

    def test_vanished_job_is_closed_out(self, store: Store) -> None:
        store.sync_jobs("a", [self._JOB])
        _, finished = store.sync_jobs("a", [])
        assert [j["conclusion"] for j in finished] == ["unobserved"]

    def test_history_survives_reconnect(self, store: Store) -> None:
        store.sync_jobs("a", [self._JOB])
        store.close()

        reopened = Store(DB_URL)
        assert any(j["job_id"] == 101 for j in reopened.recent_jobs())


class TestHistoryAndRetention:
    def test_snapshots_and_latest(self, store: Store) -> None:
        store.record_tick("pool-a", "org/repo", 1, 1, 1, 0, 0, 0.1)
        store.record_tick("pool-a", "org/repo", 5, 5, 0, 5, 2, 0.2)
        assert store.latest_snapshots()["pool-a"]["containers"] == 5

    def test_prune_removes_old_rows(self, store: Store) -> None:
        import time

        store.record_event(EVENT_SPAWN, pool="old")
        store._write(  # pyright: ignore[reportPrivateUsage]
            "UPDATE runner_events SET ts = ?", (time.time() - 40 * 86400,)
        )
        store.record_event(EVENT_SPAWN, pool="new")

        assert store.prune(30) == 1
        assert [r["pool"] for r in store.recent_events()] == ["new"]

    def test_event_counts(self, store: Store) -> None:
        store.record_event(EVENT_SPAWN, pool="a")
        store.record_event(EVENT_SPAWN, pool="a")
        assert store.event_counts_since(1)[EVENT_SPAWN] == 2


class TestDegradation:
    def test_unreachable_server_returns_none_rather_than_raising(self) -> None:
        """A database outage must cost the dashboard, never the runners."""
        assert open_store("mysql://nobody:nope@127.0.0.1:1/rorch") is None
