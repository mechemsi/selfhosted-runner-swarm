#!/usr/bin/env python3
# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Copy an existing SQLite store into MariaDB.

Upgrading to the MariaDB backend leaves the old SQLite file behind in its
Docker volume rather than deleting it, so history is recoverable. This moves it
across instead of starting from an empty dashboard.

    python3 scripts/migrate-sqlite-to-mariadb.py \\
        --from /var/lib/docker/volumes/rorch_rorch-data/_data/rorch.db \\
        --to  'mysql://rorch:pw@127.0.0.1:3306/rorch'

Idempotent: rows are upserted on their primary key, so re-running is safe.
Tables with an auto-increment id (snapshots, events, audit) are appended, so
running twice would duplicate those — use --skip-history to move only the
keyed tables.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator"))

from rorch.store import Store

# Keyed tables can be re-run safely; the rest append.
KEYED = {
    "pool_overrides": "pool",
    "global_overrides": "key",
    "pool_state": "pool",
    "protected_containers": "container",
    "runner_status": "container",
    "runner_jobs": "job_id",
    "idempotency": "key",
}
HISTORY = ["tick_snapshots", "runner_events", "audit_log"]


def columns(source: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in source.execute(f"PRAGMA table_info({table})")]


def copy_table(source: sqlite3.Connection, target: Store, table: str, key: str | None) -> int:
    cols = [c for c in columns(source, table) if not (table in HISTORY and c == "id")]
    if not cols:
        return 0
    rows = source.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
    if not rows:
        return 0

    if key:
        updates = [c for c in cols if c != key]
        statement = target.dialect.upsert(table, cols, key=f"`{key}`", updates=updates or cols)
    else:
        marks = ", ".join(["?"] * len(cols))
        quoted = ", ".join(f"`{c}`" for c in cols)
        statement = target.dialect.sql(
            f"INSERT INTO {table} ({quoted}) VALUES ({marks})"
        )

    for row in rows:
        target._write(statement, tuple(row))
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="source", required=True, help="path to rorch.db")
    parser.add_argument("--to", dest="target", required=True, help="mysql://user:pw@host/db")
    parser.add_argument(
        "--skip-history",
        action="store_true",
        help="copy only keyed tables — safe to re-run without duplicating snapshots",
    )
    args = parser.parse_args()

    if not Path(args.source).exists():
        print(f"error: {args.source} does not exist", file=sys.stderr)
        return 1

    source = sqlite3.connect(args.source)
    target = Store(args.target)
    if not target.is_mariadb:
        print("error: --to must be a mysql:// URL", file=sys.stderr)
        return 1

    total = 0
    for table, key in KEYED.items():
        moved = copy_table(source, target, table, key)
        total += moved
        print(f"  {table:24} {moved:6d} rows")

    if args.skip_history:
        print("  (history tables skipped)")
    else:
        for table in HISTORY:
            moved = copy_table(source, target, table, None)
            total += moved
            print(f"  {table:24} {moved:6d} rows")

    print(f"\nMigrated {total} rows into {args.target.split('@')[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
