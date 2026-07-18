"""SQLite (WAL) connection + schema init for the blackboard. DESIGN.md §4.

Build in Unit U1. Responsibilities:
  - open a sqlite3 connection in WAL mode (PRAGMA journal_mode=WAL, foreign_keys=ON)
  - `init_db(path)`: execute schema.sql (idempotent CREATE TABLE IF NOT EXISTS)
  - `connect(path)`: open one more independent connection to the DB file
  - `transaction(conn)`: run a multi-statement write batch as ONE crisp,
    non-nesting transaction on a single connection
  - a `reset(path)` helper for tests

**Connection model (S4).** WAL's promise is "one writer, many concurrent readers"
-- but only *across separate connections*. A single process-wide connection shared
by every request thread serializes on that one handle and gives away all of WAL's
concurrency, and (worse) folds unrelated single-statement writers into whatever
explicit transaction happens to be open on it (SQLite has exactly ONE transaction
per connection). So callers that serve concurrent work open one connection **per
request / per thread** via `connect()` (the server's `get_conn` dependency does
exactly this): each such connection has its own transaction scope, real WAL reader
concurrency, and no cross-talk. `init_db` remains the one-time schema bootstrap and
also hands back a connection callers may keep for direct out-of-band inspection.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

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

# SQLite busy_timeout (see `_configure`): how long a write that loses a brief lock
# race waits for the winner instead of failing with "database is locked". Exposed as
# a named constant (rather than a bare literal buried in `_configure`) so callers that
# must sit ABOVE it can reference it directly -- notably the hook adapter's HTTP client
# timeout, which has to outlast a busy server's lock wait or a merely-contended (not
# dead) blackboard trips the hook's fail path. See hooks/adapter.py `_DEFAULT_TIMEOUT_SECONDS`.
BUSY_TIMEOUT_SECONDS = 5.0


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
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")


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


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a multi-statement write batch as ONE transaction on `conn`.

    Uses `BEGIN IMMEDIATE` (not the default deferred `BEGIN`) so the write lock is
    taken up front: two writers can never each hold a read lock and then deadlock
    trying to upgrade to a write under WAL -- the loser simply waits on
    `busy_timeout`. On any exception the block `ROLLBACK`s and re-raises; otherwise
    it `COMMIT`s.

    Crucially, this transaction is scoped to THIS connection alone. Because the
    server hands every request its own connection (see the module docstring), a
    `ROLLBACK` here can only ever undo the statements this `with` block issued on
    this connection -- it can never swallow a *concurrent single-statement writer*,
    which runs on a different connection under its own autocommit transaction. The
    `in_transaction` guard makes the "one transaction per connection" invariant
    explicit: entering while a transaction is already open on `conn` is a bug (it
    would silently make the outer writer's fate depend on this block's rollback),
    so we refuse rather than nest.
    """
    if conn.in_transaction:
        raise sqlite3.ProgrammingError(
            "db.transaction(): connection is already inside a transaction; "
            "one transaction per connection (open a separate connection instead)"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def reset(path: StrPath) -> None:
    """Delete the DB file (and any WAL/SHM sidecars) at `path`. Test helper only."""
    p = Path(path)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(p) + suffix)
        if candidate.exists():
            candidate.unlink()
