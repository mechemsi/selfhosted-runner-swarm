# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Database dialect layer: SQLite by default, MariaDB when configured.

The store speaks one SQL shape and this module translates it. Three things
genuinely differ and none of them can be papered over:

* parameter placeholders — ``?`` versus ``%s``
* auto-increment — ``INTEGER PRIMARY KEY AUTOINCREMENT`` versus ``BIGINT AUTO_INCREMENT``
* upserts — ``ON CONFLICT ... DO UPDATE`` versus ``ON DUPLICATE KEY UPDATE``

MariaDB also cannot index ``TEXT`` without a prefix length, so every keyed
column is ``VARCHAR(191)``. 191 rather than 255 because it stays inside the
767-byte index limit on older ``utf8mb4`` installs.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

log = logging.getLogger(__name__)

# Longest indexable VARCHAR under utf8mb4 on the historic 767-byte limit.
KEY_LEN = 191


@dataclass(frozen=True)
class Dsn:
    """Parsed connection target."""

    driver: str  # "sqlite" or "mariadb"
    host: str = ""
    port: int = 3306
    user: str = ""
    password: str = ""
    database: str = ""
    path: str = ""

    @property
    def is_mariadb(self) -> bool:
        return self.driver == "mariadb"

    def describe(self) -> str:
        """Safe for logs — never includes the password."""
        if self.is_mariadb:
            return f"mariadb://{self.user}@{self.host}:{self.port}/{self.database}"
        return f"sqlite://{self.path}"


def parse_dsn(url: str) -> Dsn:
    """Accept a bare path (SQLite) or a mysql://user:pass@host:port/db URL."""
    if "://" not in url:
        return Dsn(driver="sqlite", path=url)

    parsed = urlparse(url)
    if parsed.scheme in {"sqlite", "file"}:
        return Dsn(driver="sqlite", path=parsed.path or url.split("://", 1)[1])
    if parsed.scheme not in {"mysql", "mariadb"}:
        raise ValueError(f"unsupported database scheme '{parsed.scheme}'")

    database = parsed.path.lstrip("/")
    if not database:
        raise ValueError("database name is required, e.g. mysql://user:pass@host/rorch")
    return Dsn(
        driver="mariadb",
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=database,
    )


class Cursorish(Protocol):
    def fetchall(self) -> Any: ...
    @property
    def rowcount(self) -> int: ...


class Dialect:
    """Translates the store's SQL into what the target engine accepts."""

    def __init__(self, dsn: Dsn) -> None:
        self.dsn = dsn

    @property
    def placeholder(self) -> str:
        return "%s" if self.dsn.is_mariadb else "?"

    def sql(self, statement: str) -> str:
        """Rewrite placeholders for the target driver."""
        if not self.dsn.is_mariadb:
            return statement
        # Only bare ? placeholders are ours; the store never embeds a literal ?.
        return statement.replace("?", "%s")

    def upsert(self, table: str, columns: list[str], key: str, updates: list[str]) -> str:
        """One INSERT ... upsert statement in the target dialect."""
        # Always backtick: `key` is reserved in MariaDB and SQLite accepts the
        # same quoting, so the store never has to care which engine it is on.
        cols = ", ".join(f"`{c}`" for c in columns)
        marks = ", ".join(["?"] * len(columns))
        if self.dsn.is_mariadb:
            assignments = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in updates)
            statement = f"INSERT INTO {table} ({cols}) VALUES ({marks}) "
            statement += f"ON DUPLICATE KEY UPDATE {assignments}"
        else:
            assignments = ", ".join(f"`{c}`=excluded.`{c}`" for c in updates)
            statement = f"INSERT INTO {table} ({cols}) VALUES ({marks}) "
            statement += f"ON CONFLICT({key}) DO UPDATE SET {assignments}"
        return self.sql(statement)

    def schema(self) -> list[str]:
        """DDL statements, in order, for this engine."""
        return _MARIADB_SCHEMA if self.dsn.is_mariadb else _SQLITE_SCHEMA


def _split(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


_SQLITE_SCHEMA = _split(
    """
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
    `key`      TEXT PRIMARY KEY,
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
CREATE TABLE IF NOT EXISTS runner_jobs (
    job_id     INTEGER PRIMARY KEY,
    pool       TEXT NOT NULL DEFAULT '',
    repo       TEXT NOT NULL DEFAULT '',
    workflow   TEXT NOT NULL DEFAULT '',
    job_name   TEXT NOT NULL DEFAULT '',
    runner     TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT '',
    conclusion TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    ts         REAL NOT NULL,
    ended_ts   REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_ts ON runner_jobs(ts);
CREATE INDEX IF NOT EXISTS idx_jobs_runner ON runner_jobs(runner);
CREATE INDEX IF NOT EXISTS idx_jobs_repo ON runner_jobs(repo);
CREATE TABLE IF NOT EXISTS idempotency (
    `key`    TEXT PRIMARY KEY,
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
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)
"""
)

# MariaDB will not index TEXT without a prefix length, so keyed columns are
# VARCHAR. CREATE INDEX has no IF NOT EXISTS before 10.5, so indexes are
# declared inline in CREATE TABLE instead.
_MARIADB_SCHEMA = _split(
    f"""
CREATE TABLE IF NOT EXISTS tick_snapshots (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts         DOUBLE  NOT NULL,
    pool       VARCHAR({KEY_LEN}) NOT NULL,
    display    VARCHAR(255) NOT NULL DEFAULT '',
    containers INT NOT NULL,
    online     INT NOT NULL,
    idle       INT NOT NULL,
    busy       INT NOT NULL,
    queued     INT NOT NULL,
    duration   DOUBLE NOT NULL DEFAULT 0,
    INDEX idx_snapshots_ts (ts),
    INDEX idx_snapshots_pool_ts (pool, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS runner_events (
    id        BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts        DOUBLE NOT NULL,
    pool      VARCHAR({KEY_LEN}) NOT NULL DEFAULT '',
    container VARCHAR({KEY_LEN}) NOT NULL DEFAULT '',
    event     VARCHAR(64) NOT NULL,
    reason    TEXT NOT NULL,
    INDEX idx_events_ts (ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS pool_overrides (
    pool       VARCHAR({KEY_LEN}) PRIMARY KEY,
    data       TEXT NOT NULL,
    origin     VARCHAR(16) NOT NULL DEFAULT 'yaml',
    disabled   TINYINT NOT NULL DEFAULT 0,
    updated_at DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS global_overrides (
    `key`      VARCHAR({KEY_LEN}) PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS pool_state (
    pool     VARCHAR({KEY_LEN}) PRIMARY KEY,
    paused   TINYINT NOT NULL DEFAULT 0,
    draining TINYINT NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS protected_containers (
    container VARCHAR({KEY_LEN}) PRIMARY KEY,
    since     DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS runner_status (
    container  VARCHAR({KEY_LEN}) PRIMARY KEY,
    pool       VARCHAR({KEY_LEN}) NOT NULL DEFAULT '',
    status     VARCHAR(32) NOT NULL DEFAULT '',
    busy       TINYINT NOT NULL DEFAULT 0,
    updated_at DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS runner_jobs (
    job_id     BIGINT PRIMARY KEY,
    pool       VARCHAR({KEY_LEN}) NOT NULL DEFAULT '',
    repo       VARCHAR({KEY_LEN}) NOT NULL DEFAULT '',
    workflow   VARCHAR(255) NOT NULL DEFAULT '',
    job_name   VARCHAR(255) NOT NULL DEFAULT '',
    runner     VARCHAR({KEY_LEN}) NOT NULL DEFAULT '',
    status     VARCHAR(32) NOT NULL DEFAULT '',
    conclusion VARCHAR(32) NOT NULL DEFAULT '',
    url        VARCHAR(512) NOT NULL DEFAULT '',
    started_at VARCHAR(64) NOT NULL DEFAULT '',
    ts         DOUBLE NOT NULL,
    ended_ts   DOUBLE NULL,
    INDEX idx_jobs_ts (ts),
    INDEX idx_jobs_runner (runner),
    INDEX idx_jobs_repo (repo)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS idempotency (
    `key`    VARCHAR({KEY_LEN}) PRIMARY KEY,
    response TEXT NOT NULL,
    ts       DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS audit_log (
    id     BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts     DOUBLE NOT NULL,
    actor  VARCHAR({KEY_LEN}) NOT NULL DEFAULT '',
    action VARCHAR(64) NOT NULL,
    target VARCHAR({KEY_LEN}) NOT NULL DEFAULT '',
    detail TEXT NOT NULL,
    INDEX idx_audit_ts (ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""
)

# SQLite accepts `key` backticks too, so the store can use one spelling.
assert all("CREATE" in s for s in _SQLITE_SCHEMA)
assert all("CREATE" in s for s in _MARIADB_SCHEMA)

_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)")


def table_names(statements: list[str]) -> list[str]:
    """Tables a schema declares — used by tests to assert parity."""
    return [m.group(1) for s in statements for m in [_TABLE_RE.search(s)] if m]
