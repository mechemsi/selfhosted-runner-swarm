# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""SQLite persistence for snapshots, events, config overrides and control state.

The orchestrator runs fine without this: `__main__` passes ``None`` when no
database can be opened, and every call site treats a missing store as "behave
exactly as before". Deleting the file returns the orchestrator to pure
``config.yml`` behaviour.

# ponytail: stdlib sqlite3 behind one lock, not a connection pool or an ORM.
# The write rate is a handful of rows per tick; if that ever becomes the
# bottleneck, switch to per-thread connections before reaching for a server DB.
"""

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tick_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    pool       TEXT    NOT NULL,
    display    TEXT    NOT NULL DEFAULT '',
    containers INTEGER NOT NULL,
    online     INTEGER NOT NULL,
    idle       INTEGER NOT NULL,
    busy       INTEGER NOT NULL,
    queued     INTEGER NOT NULL,
    duration   REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON tick_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_snapshots_pool_ts ON tick_snapshots(pool, ts);

CREATE TABLE IF NOT EXISTS runner_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    pool      TEXT NOT NULL DEFAULT '',
    container TEXT NOT NULL DEFAULT '',
    event     TEXT NOT NULL,
    reason    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON runner_events(ts);

CREATE TABLE IF NOT EXISTS pool_overrides (
    pool       TEXT PRIMARY KEY,
    data       TEXT NOT NULL DEFAULT '{}',
    origin     TEXT NOT NULL DEFAULT 'yaml',
    disabled   INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS global_overrides (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pool_state (
    pool     TEXT PRIMARY KEY,
    paused   INTEGER NOT NULL DEFAULT 0,
    draining INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS protected_containers (
    container TEXT PRIMARY KEY,
    since     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runner_status (
    container  TEXT PRIMARY KEY,
    pool       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT '',
    busy       INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    key      TEXT PRIMARY KEY,
    response TEXT NOT NULL,
    ts       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    actor  TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""

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


class SqliteStore:
    """Durable orchestrator state. Every method is safe to call from any thread."""

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            self._db.commit()
        log.info("State store ready at %s", path)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._lock:
            self._db.execute(sql, params)
            self._db.commit()

    def _read(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

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
                cur = self._db.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
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
            "INSERT INTO pool_overrides (pool, data, origin, disabled, updated_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(pool) DO UPDATE SET data=excluded.data,"
            " origin=excluded.origin, disabled=excluded.disabled,"
            " updated_at=excluded.updated_at",
            (pool, json.dumps(data), origin, int(disabled), time.time()),
        )

    def delete_pool_override(self, pool: str) -> None:
        """Remove the override row — the pool reverts to its config.yml definition."""
        self._write("DELETE FROM pool_overrides WHERE pool = ?", (pool,))

    def global_overrides(self) -> dict[str, str]:
        rows = self._read("SELECT key, value FROM global_overrides")
        return {row["key"]: row["value"] for row in rows}

    def set_global(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO global_overrides (key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at",
            (key, value, time.time()),
        )

    def delete_global(self, key: str) -> None:
        self._write("DELETE FROM global_overrides WHERE key = ?", (key,))

    # ── control state ──────────────────────────────────────────────────────

    def pool_states(self) -> dict[str, PoolState]:
        rows = self._read("SELECT pool, paused, draining FROM pool_state")
        return {
            row["pool"]: PoolState(paused=bool(row["paused"]), draining=bool(row["draining"]))
            for row in rows
        }

    def set_pool_state(self, pool: str, paused: bool, draining: bool) -> None:
        self._write(
            "INSERT INTO pool_state (pool, paused, draining) VALUES (?,?,?)"
            " ON CONFLICT(pool) DO UPDATE SET paused=excluded.paused,"
            " draining=excluded.draining",
            (pool, int(paused), int(draining)),
        )

    def protected_containers(self) -> frozenset[str]:
        """Containers the aged reaper must leave alone."""
        rows = self._read("SELECT container FROM protected_containers")
        return frozenset(row["container"] for row in rows)

    def set_protected(self, container: str, protected: bool) -> None:
        if protected:
            self._write(
                "INSERT OR REPLACE INTO protected_containers (container, since) VALUES (?,?)",
                (container, time.time()),
            )
        else:
            self._write("DELETE FROM protected_containers WHERE container = ?", (container,))

    # ── runner status (what GitHub last reported) ──────────────────────────

    def replace_runner_status(self, pool: str, rows: list[tuple[str, str, bool]]) -> None:
        """Swap in this pool's runner list: (container, status, busy)."""
        now = time.time()
        with self._lock:
            self._db.execute("DELETE FROM runner_status WHERE pool = ?", (pool,))
            self._db.executemany(
                "INSERT OR REPLACE INTO runner_status"
                " (container, pool, status, busy, updated_at) VALUES (?,?,?,?,?)",
                [(name, pool, status, int(busy), now) for name, status, busy in rows],
            )
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

    # ── idempotency ────────────────────────────────────────────────────────

    def idempotent_response(self, key: str) -> str | None:
        """Previously recorded response for this key, if the caller retried."""
        rows = self._read("SELECT response FROM idempotency WHERE key = ?", (key,))
        return rows[0]["response"] if rows else None

    def remember_response(self, key: str, response: str) -> None:
        self._write(
            "INSERT OR REPLACE INTO idempotency (key, response, ts) VALUES (?,?,?)",
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


def open_store(path: str) -> SqliteStore | None:
    """Open the store, returning None if the path is unusable.

    A broken database must never stop runners from being provisioned, so this
    degrades to the pre-database behaviour instead of raising.
    """
    try:
        from pathlib import Path

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return SqliteStore(path)
    except Exception:
        log.error("Could not open state store at %s — running without it", path, exc_info=True)
        return None
