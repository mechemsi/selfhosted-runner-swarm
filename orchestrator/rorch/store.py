# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Durable orchestrator state: SQLite by default, MariaDB when configured.

The orchestrator runs fine without this: `__main__` passes ``None`` when no
database can be opened, and every call site treats a missing store as "behave
exactly as before". That degradation matters more with an external database —
a MariaDB outage must not stop runners being provisioned.

Set ``RORCH_DB_URL=mysql://user:pass@mariadb:3306/rorch`` to use MariaDB;
anything without a scheme is treated as a SQLite file path.

# ponytail: one connection behind one lock, not a pool. The write rate is a
# handful of rows per tick and reads come from one Flask thread; add pooling
# when a measurement says to, not before.
"""

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

from rorch.dialect import Dialect, Dsn, parse_dsn

log = logging.getLogger(__name__)


# The job upsert keeps a COALESCE so a completion time is never overwritten by a
# later scan, which neither dialect helper can express generically.
_JOB_COLUMNS = (
    "job_id, pool, repo, workflow, job_name, runner,"
    " status, conclusion, url, started_at, ts, ended_ts"
)
_JOB_UPSERT_SQLITE = (
    f"INSERT INTO runner_jobs ({_JOB_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
    " ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,"
    " conclusion=excluded.conclusion, runner=excluded.runner,"
    " ended_ts=COALESCE(runner_jobs.ended_ts, excluded.ended_ts)"
)
_JOB_UPSERT_MARIADB = (
    f"INSERT INTO runner_jobs ({_JOB_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
    " ON DUPLICATE KEY UPDATE status=VALUES(status),"
    " conclusion=VALUES(conclusion), runner=VALUES(runner),"
    " ended_ts=COALESCE(ended_ts, VALUES(ended_ts))"
)

# pymysql raises these when the server has closed an idle connection.
_DISCONNECT_CODES = {2006, 2013, 4031}


def _is_disconnect(error: Exception) -> bool:
    code = getattr(error, "args", [None])[0]
    if code in _DISCONNECT_CODES:
        return True
    return "MySQL server has gone away" in str(error) or "Lost connection" in str(error)


# Event names recorded in runner_events.
EVENT_SPAWN = "spawn"
EVENT_SPAWN_FAILED = "spawn_failed"
EVENT_EXIT = "exit"
EVENT_STUCK_KILL = "stuck_kill"
EVENT_AGED_KILL = "aged_kill"
EVENT_DEREGISTER = "deregister"
EVENT_MANUAL_STOP = "manual_stop"


@dataclass(frozen=True)
class PoolState:
    """Operator-set control flags for one pool."""

    paused: bool = False
    draining: bool = False


class Store:
    """Durable orchestrator state. Every method is safe to call from any thread."""

    def __init__(self, url: str) -> None:
        self._lock = threading.Lock()
        self._dsn: Dsn = parse_dsn(url)
        self.dialect = Dialect(self._dsn)
        self._db = self._connect()
        with self._lock:
            for statement in self.dialect.schema():
                self._execute(statement)
            self._db.commit()
        log.info("State store ready at %s", self._dsn.describe())

    @property
    def is_mariadb(self) -> bool:
        return self._dsn.is_mariadb

    def _connect(self) -> Any:
        if not self._dsn.is_mariadb:
            db = sqlite3.connect(self._dsn.path, check_same_thread=False)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            return db

        import pymysql  # imported lazily so SQLite installs need no driver
        from pymysql.cursors import DictCursor

        return pymysql.connect(
            host=self._dsn.host,
            port=self._dsn.port,
            user=self._dsn.user,
            password=self._dsn.password,
            database=self._dsn.database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=DictCursor,
        )

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        """Run one statement, reconnecting once if the server dropped us.

        MariaDB closes idle connections (wait_timeout), and the orchestrator is
        idle between ticks by design, so a stale-connection retry is required
        rather than optional.
        """
        statement = self.dialect.sql(sql)
        try:
            return self._run(statement, params)
        except Exception as error:
            if not self._dsn.is_mariadb or not _is_disconnect(error):
                raise
            log.warning("Database connection lost, reconnecting: %s", error)
            self._db = self._connect()
            return self._run(statement, params)

    def _run(self, statement: str, params: tuple[Any, ...]) -> Any:
        if self._dsn.is_mariadb:
            cursor = self._db.cursor()
            cursor.execute(statement, params)
            return cursor
        return self._db.execute(statement, params)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._lock:
            self._execute(sql, params)
            self._db.commit()

    def _read(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        with self._lock:
            return list(self._execute(sql, params).fetchall())

    # ── history ────────────────────────────────────────────────────────────

    def record_tick(
        self,
        pool: str,
        display: str,
        containers: int,
        online: int,
        idle: int,
        busy: int,
        queued: int,
        duration: float,
    ) -> None:
        self._write(
            "INSERT INTO tick_snapshots"
            " (ts, pool, display, containers, online, idle, busy, queued, duration)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), pool, display, containers, online, idle, busy, queued, duration),
        )

    def record_event(
        self, event: str, container: str = "", pool: str = "", reason: str = ""
    ) -> None:
        self._write(
            "INSERT INTO runner_events (ts, pool, container, event, reason) VALUES (?,?,?,?,?)",
            (time.time(), pool, container, event, reason),
        )

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._read(
            "SELECT ts, pool, container, event, reason FROM runner_events ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def latest_snapshots(self) -> dict[str, dict[str, Any]]:
        """Most recent snapshot per pool."""
        rows = self._read(
            "SELECT s.* FROM tick_snapshots s"
            " JOIN (SELECT pool, MAX(id) AS id FROM tick_snapshots GROUP BY pool) m"
            " ON s.id = m.id"
        )
        return {row["pool"]: dict(row) for row in rows}

    def snapshots_since(self, hours: float) -> list[dict[str, Any]]:
        cutoff = time.time() - hours * 3600
        rows = self._read(
            "SELECT ts, pool, containers, online, idle, busy, queued, duration"
            " FROM tick_snapshots WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        )
        return [dict(row) for row in rows]

    def event_counts_since(self, hours: float) -> dict[str, int]:
        cutoff = time.time() - hours * 3600
        rows = self._read(
            "SELECT event, COUNT(*) AS n FROM runner_events WHERE ts >= ? GROUP BY event",
            (cutoff,),
        )
        return {row["event"]: row["n"] for row in rows}

    def prune(self, days: int) -> int:
        """Drop history older than `days`. Returns rows deleted."""
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        with self._lock:
            deleted = 0
            for table in ("tick_snapshots", "runner_events", "audit_log"):
                cur = self._execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                deleted += cur.rowcount
            self._db.commit()
        if deleted:
            log.info("Pruned %d history row(s) older than %d day(s)", deleted, days)
        return deleted

    # ── config overrides ───────────────────────────────────────────────────

    def pool_overrides(self) -> dict[str, dict[str, Any]]:
        rows = self._read("SELECT pool, data, origin, disabled FROM pool_overrides")
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                data = json.loads(row["data"])
            except ValueError:
                log.warning("Ignoring unparseable override for pool '%s'", row["pool"])
                continue
            out[row["pool"]] = {
                "data": data,
                "origin": row["origin"],
                "disabled": bool(row["disabled"]),
            }
        return out

    def set_pool_override(
        self,
        pool: str,
        data: dict[str, Any],
        origin: str = "yaml",
        disabled: bool = False,
    ) -> None:
        self._write(
            self.dialect.upsert(
                "pool_overrides",
                ["pool", "data", "origin", "disabled", "updated_at"],
                key="pool",
                updates=["data", "origin", "disabled", "updated_at"],
            ),
            (pool, json.dumps(data), origin, int(disabled), time.time()),
        )

    def delete_pool_override(self, pool: str) -> None:
        """Remove the override row — the pool reverts to its config.yml definition."""
        self._write("DELETE FROM pool_overrides WHERE pool = ?", (pool,))

    def global_overrides(self) -> dict[str, str]:
        rows = self._read("SELECT `key`, value FROM global_overrides")
        return {row["key"]: row["value"] for row in rows}

    def set_global(self, key: str, value: str) -> None:
        self._write(
            self.dialect.upsert(
                "global_overrides",
                ["key", "value", "updated_at"],
                key="`key`",
                updates=["value", "updated_at"],
            ),
            (key, value, time.time()),
        )

    def delete_global(self, key: str) -> None:
        self._write("DELETE FROM global_overrides WHERE `key` = ?", (key,))

    # ── control state ──────────────────────────────────────────────────────

    def pool_states(self) -> dict[str, PoolState]:
        rows = self._read("SELECT pool, paused, draining FROM pool_state")
        return {
            row["pool"]: PoolState(paused=bool(row["paused"]), draining=bool(row["draining"]))
            for row in rows
        }

    def set_pool_state(self, pool: str, paused: bool, draining: bool) -> None:
        self._write(
            self.dialect.upsert(
                "pool_state",
                ["pool", "paused", "draining"],
                key="pool",
                updates=["paused", "draining"],
            ),
            (pool, int(paused), int(draining)),
        )

    def protected_containers(self) -> frozenset[str]:
        """Containers the aged reaper must leave alone."""
        rows = self._read("SELECT container FROM protected_containers")
        return frozenset(row["container"] for row in rows)

    def set_protected(self, container: str, protected: bool) -> None:
        if protected:
            self._write(
                self.dialect.upsert(
                    "protected_containers",
                    ["container", "since"],
                    key="container",
                    updates=["since"],
                ),
                (container, time.time()),
            )
        else:
            self._write("DELETE FROM protected_containers WHERE container = ?", (container,))

    # ── runner status (what GitHub last reported) ──────────────────────────

    def replace_runner_status(self, pool: str, rows: list[tuple[str, str, bool]]) -> None:
        """Swap in this pool's runner list: (container, status, busy)."""
        now = time.time()
        upsert = self.dialect.upsert(
            "runner_status",
            ["container", "pool", "status", "busy", "updated_at"],
            key="container",
            updates=["pool", "status", "busy", "updated_at"],
        )
        with self._lock:
            self._execute("DELETE FROM runner_status WHERE pool = ?", (pool,))
            for name, status, busy in rows:
                self._execute(upsert, (name, pool, status, int(busy), now))
            self._db.commit()

    def runner_status(self) -> dict[str, dict[str, Any]]:
        rows = self._read("SELECT container, pool, status, busy FROM runner_status")
        return {
            row["container"]: {
                "pool": row["pool"],
                "status": row["status"],
                "busy": bool(row["busy"]),
            }
            for row in rows
        }

    # ── jobs (what each runner actually ran) ───────────────────────────────

    def sync_jobs(
        self, pool: str, jobs: list[tuple[int, str, str, str, str, str, str, str, str]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Record this scan's jobs. Returns (newly started, just finished).

        Each tuple is (job_id, repo, workflow, job_name, runner, status,
        conclusion, url, started_at). The two returned lists are what the caller
        logs, so a started or finished job produces exactly one log line.
        """
        now = time.time()
        started: list[dict[str, Any]] = []
        finished: list[dict[str, Any]] = []

        with self._lock:
            known = {
                row["job_id"]: dict(row)
                for row in self._execute(
                    "SELECT job_id, status, ended_ts FROM runner_jobs WHERE pool = ?", (pool,)
                ).fetchall()
            }
            seen: set[int] = set()

            for job_id, repo, workflow, name, runner, status, conclusion, url, start in jobs:
                seen.add(job_id)
                previous = known.get(job_id)
                record = {
                    "job_id": job_id,
                    "repo": repo,
                    "workflow": workflow,
                    "job_name": name,
                    "runner": runner,
                    "status": status,
                    "conclusion": conclusion,
                    "url": url,
                }
                ended = now if status == "completed" else None
                self._execute(
                    _JOB_UPSERT_MARIADB if self.is_mariadb else _JOB_UPSERT_SQLITE,
                    (
                        job_id,
                        pool,
                        repo,
                        workflow,
                        name,
                        runner,
                        status,
                        conclusion,
                        url,
                        start,
                        now,
                        ended,
                    ),
                )
                if previous is None:
                    started.append(record)
                elif status == "completed" and previous["ended_ts"] is None:
                    finished.append(record)

            # A job whose run left the queued/in_progress window stops being
            # reported. Close it out rather than leaving it "running" forever.
            for job_id, previous in known.items():
                if job_id in seen or previous["ended_ts"] is not None:
                    continue
                self._execute(
                    "UPDATE runner_jobs SET status='completed',"
                    " conclusion=CASE conclusion WHEN '' THEN 'unobserved' ELSE conclusion END,"
                    " ended_ts=? WHERE job_id=?",
                    (now, job_id),
                )
                found = self._execute(
                    "SELECT job_id, repo, workflow, job_name, runner, conclusion, url"
                    " FROM runner_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchall()
                row = found[0] if found else None
                if row is not None:
                    finished.append(dict(row))

            self._db.commit()

        return started, finished

    def recent_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._read(
            "SELECT job_id, pool, repo, workflow, job_name, runner, status, conclusion,"
            " url, started_at, ts, ended_ts FROM runner_jobs ORDER BY ts DESC, job_id DESC"
            " LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def jobs_by_runner(self) -> dict[str, dict[str, Any]]:
        """The job each runner is currently executing, keyed by runner name."""
        rows = self._read(
            "SELECT runner, repo, workflow, job_name, url FROM runner_jobs"
            " WHERE ended_ts IS NULL AND runner != '' AND status = 'in_progress'"
        )
        return {row["runner"]: dict(row) for row in rows}

    # ── idempotency ────────────────────────────────────────────────────────

    def idempotent_response(self, key: str) -> str | None:
        """Previously recorded response for this key, if the caller retried."""
        rows = self._read("SELECT response FROM idempotency WHERE `key` = ?", (key,))
        return rows[0]["response"] if rows else None

    def remember_response(self, key: str, response: str) -> None:
        self._write(
            self.dialect.upsert(
                "idempotency", ["key", "response", "ts"], key="`key`", updates=["response", "ts"]
            ),
            (key, response, time.time()),
        )

    def prune_idempotency(self, hours: float = 24.0) -> None:
        self._write("DELETE FROM idempotency WHERE ts < ?", (time.time() - hours * 3600,))

    # ── audit ──────────────────────────────────────────────────────────────

    def audit(self, actor: str, action: str, target: str = "", detail: str = "") -> None:
        self._write(
            "INSERT INTO audit_log (ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
            (time.time(), actor, action, target, detail),
        )

    def recent_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._read(
            "SELECT ts, actor, action, target, detail FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]


def open_store(url: str) -> Store | None:
    """Open the store, returning None if it is unusable.

    A broken database must never stop runners from being provisioned, so this
    degrades to the pre-database behaviour instead of raising. That matters more
    with MariaDB than it did with a local file: a database outage should cost
    the dashboard, not the runners.
    """
    try:
        dsn = parse_dsn(url)
        if not dsn.is_mariadb:
            from pathlib import Path

            Path(dsn.path).parent.mkdir(parents=True, exist_ok=True)
        return Store(url)
    except Exception:
        try:
            target = parse_dsn(url).describe()
        except Exception:
            target = "<unparseable RORCH_DB_URL>"
        log.error("Could not open state store at %s — running without it", target, exc_info=True)
        return None
