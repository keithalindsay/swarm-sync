"""SQLite (WAL) connection + schema init for the blackboard. DESIGN.md §4.

Build in Unit U1. Responsibilities:
  - open a sqlite3 connection in WAL mode (PRAGMA journal_mode=WAL, foreign_keys=ON)
  - `init_db(path)`: execute schema.sql (idempotent CREATE TABLE IF NOT EXISTS)
  - a single-writer connection helper; row_factory = sqlite3.Row
  - a `reset(path)` helper for tests

Keep this the ONLY module that opens the DB file so single-writer semantics hold.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Union

StrPath = Union[str, "Path"]

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA = SCHEMA_PATH.read_text()

# All tables the schema is expected to create. Kept explicit (rather than parsed out
# of schema.sql) so a test can assert against a known-good list independent of the
# DDL text itself.
EXPECTED_TABLES = (
    "parcels",
    "leases",
    "contracts",
    "pheromone",
    "intents",
    "events",
)


def _configure(conn: sqlite3.Connection) -> None:
    """Apply the pragmas + row factory every connection to the blackboard must use."""
    conn.row_factory = sqlite3.Row
    # journal_mode is a database-level (not connection-level) setting but must be
    # (re)asserted per-connection to take effect on that handle; foreign_keys is
    # explicitly per-connection in SQLite and defaults OFF, so it must be set here.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # busy_timeout: U12's broker is the first unit to genuinely dispatch
    # concurrent writers against this one shared connection (co-schedulable
    # tasks run their worktree-edit/lease/heartbeat steps in parallel threads).
    # SQLite's own default busy behavior is to fail immediately
    # (`sqlite3.OperationalError: database is locked`) on a write that loses
    # a brief lock race; 5s lets a losing writer simply wait for the winner to
    # finish its single short statement instead of erroring out. Harmless for
    # every earlier, single-threaded unit's tests (only engages under real
    # contention, which none of them exercised).
    conn.execute("PRAGMA busy_timeout = 5000")


def connect(path: StrPath) -> sqlite3.Connection:
    """Open a connection to the blackboard DB at `path` with WAL + FKs enabled.

    Does NOT create the schema — callers that need a guaranteed-initialized DB
    should call `init_db` first (or use it, which returns an already-configured
    connection).
    """
    # check_same_thread=False: the FastAPI server (U7) holds one connection for
    # the app's lifetime and serves sync route handlers off Starlette's
    # threadpool, so the thread that opens the connection is not reliably the
    # thread every request executes on. SQLite itself is safe for this on the
    # default (serialized) build Python ships against; single-writer semantics
    # here mean "one shared connection/DB file", not "one OS thread".
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    _configure(conn)
    return conn


def init_db(path: StrPath) -> sqlite3.Connection:
    """Idempotently create the blackboard schema at `path` and return a connection.

    Safe to call repeatedly against the same file: schema.sql uses
    `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`, so a second call
    on an already-initialized DB is a no-op (no errors, no data loss, no duplicate
    tables/indexes).
    """
    conn = connect(path)
    conn.executescript(SCHEMA)
    return conn


def reset(path: StrPath) -> None:
    """Delete the DB file (and any WAL/SHM sidecars) at `path`. Test helper only."""
    p = Path(path)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(p) + suffix)
        if candidate.exists():
            candidate.unlink()
