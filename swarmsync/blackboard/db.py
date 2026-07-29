"""SQLite (WAL) connection + schema init for the blackboard. DESIGN.md §4.

Build in Unit U1. Responsibilities:
  - open a sqlite3 connection in WAL mode (PRAGMA journal_mode=WAL, foreign_keys=ON)
  - `init_db(path)`: execute schema.sql (idempotent CREATE TABLE IF NOT EXISTS),
    gated on `SCHEMA_VERSION` -- refuses legacy/mismatched DBs (WP3.4)
  - `connect(path)`: open one more independent connection to the DB file
  - `transaction(conn)`: run a multi-statement write batch as ONE crisp,
    non-nesting transaction on a single connection
  - `bind_managed_root` / `stored_managed_root`: pin a DB file to the one repo
    root it coordinates (WP3.4; server wiring owned by a separate work package)
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
from typing import Iterator, Optional, Union

StrPath = Union[str, "Path"]

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA = SCHEMA_PATH.read_text()

# Version of the schema this code reads and writes, stamped into `meta` under key
# `schema_version` when `init_db` creates a fresh DB. The migration policy is
# "refuse + rotate" (NOT a migration framework): `init_db` refuses to touch any DB
# whose stamp is missing or different, and the operator rotates the old file aside
# (`swarmsync-serve --fresh`) or deletes it and re-indexes.
#
# History:
#   v1 -- every pre-`meta` DB (implicit; such DBs carry no stamp at all).
#   v2 -- relative to the last public (v1) state: the `meta` table itself (WP3.4),
#         the `open_integrations` crash-recovery projection (WP3.2), and the
#         `idx_leases_reap` index (WP1.2).
#   v3 -- `open_integrations.reconcile_attempts`: startup reconciliation now KEEPS
#         an orphan row whose rollback failed (deleting it stranded an un-gated
#         merge on trunk with nothing left that could detect it), so the retry
#         needs a bounded attempt count. Additive column; a v2 DB is refused and
#         rotated per the policy below rather than migrated in place.
SCHEMA_VERSION = 3


class SchemaVersionError(RuntimeError):
    """The DB file's schema version is missing (legacy) or does not match
    `SCHEMA_VERSION`. Raised by `init_db` BEFORE any DDL touches the file."""


class ManagedRootMismatchError(RuntimeError):
    """The DB file is already bound (`meta.managed_root`) to a DIFFERENT repo root.

    Parcel ids are root-relative, so proceeding would silently mix two repos'
    parcel maps. Raised by `bind_managed_root`."""

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


def _pow_fallback(base: float, exponent: float) -> float:
    """Python stand-in for SQLite's `pow()` on builds compiled without
    SQLITE_ENABLE_MATH_FUNCTIONS (see `_configure`). Matches the built-in's
    always-REAL result for the arguments the decay statement passes."""
    return float(base) ** float(exponent)


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
    # C15 (WP4.5): `events.decay_pheromone` decays every pheromone row in ONE
    # UPDATE (`strength * pow(0.5, dt / half_life)`) so each row decays from its
    # current committed value atomically. SQLite's `pow()` only exists when the
    # library was compiled with SQLITE_ENABLE_MATH_FUNCTIONS -- true on this
    # dev box, not guaranteed everywhere Python ships. Probe once per connection
    # and register a deterministic Python fallback when the build lacks it, so
    # every blackboard connection can run the decay statement. `deterministic=True`
    # lets SQLite treat it like the built-in (safe in indexes/partial evaluation).
    try:
        conn.execute("SELECT pow(2.0, 2.0)")
    except sqlite3.OperationalError:
        conn.create_function("pow", 2, _pow_fallback, deterministic=True)


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

    **Schema versioning -- the migration policy IS "refuse + rotate" (WP3.4).**
    There is deliberately no migration framework. A fresh DB is created at
    `SCHEMA_VERSION` and stamped in `meta`; any existing DB whose stamp is missing
    (legacy, pre-`meta`) or different (older OR newer code wrote it) is REFUSED
    with `SchemaVersionError` before any DDL runs -- `CREATE ... IF NOT EXISTS`
    would otherwise silently leave such a DB half-shaped. The remedy is to rotate
    the old file aside (`swarmsync-serve --fresh`) or delete it and re-index.
    """
    conn = connect(path)
    # Inspect sqlite_master BEFORE running the schema script: once
    # `CREATE ... IF NOT EXISTS` has run, a legacy DB and a fresh one are
    # indistinguishable, which is exactly how legacy DBs got stranded silently.
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    version: Optional[str] = None
    if "meta" in names:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is not None:
            version = row["value"]
    if version is None and any(table in names for table in EXPECTED_TABLES):
        conn.close()
        raise SchemaVersionError(
            f"blackboard DB {path} predates schema versioning (schema v1: "
            "application tables exist but no `meta` schema_version stamp); "
            f"this code requires schema v{SCHEMA_VERSION} and has no migration "
            "system. Remedy: run `swarmsync-serve --fresh` to rotate the old "
            "DB aside, or delete the DB file and re-index."
        )
    if version is not None and version != str(SCHEMA_VERSION):
        conn.close()
        raise SchemaVersionError(
            f"blackboard DB {path} is schema v{version}, but this code "
            f"requires schema v{SCHEMA_VERSION} (the stamp may come from older "
            "or newer swarm-sync code; there is no migration system). Remedy: "
            "run `swarmsync-serve --fresh` to rotate the old DB aside, or "
            "delete the DB file and re-index."
        )
    # Fresh DB, or an already-stamped current-version DB (idempotent no-op).
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
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


def bind_managed_root(conn: sqlite3.Connection, root: str) -> None:
    """Pin the blackboard DB behind `conn` to the ONE repo root it coordinates.

    Parcel ids are `<relpath>::<symbol>` -- relative to the indexed root -- so
    reusing a DB file against a DIFFERENT repo silently mixes two repos' parcel
    maps (finding U8). This helper makes the binding explicit and sticky:

      - first call: stores `root` in `meta` under key `managed_root`;
      - later calls with the SAME root: no-op;
      - a DIFFERENT root: raises `ManagedRootMismatchError` naming both roots.

    `conn` must be to an `init_db`-initialized DB (the `meta` table must exist).
    The first-bind write is `INSERT OR IGNORE` + read-back, so two concurrent
    first binds race safely: exactly one wins, the other either no-ops (same
    root) or raises (different root).

    NOTE (WP3.4 scope): server wiring -- calling this from `server/app.py`'s
    startup against the configured managed root -- is owned by a separate work
    package; this module only provides the primitive.
    """
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('managed_root', ?)",
        (root,),
    )
    stored = stored_managed_root(conn)
    if stored != root:
        raise ManagedRootMismatchError(
            f"blackboard DB is bound to managed root {stored!r}, but this server "
            f"is coordinating {root!r}. Parcel ids are root-relative, so reusing "
            "the DB across repos would silently mix their parcel maps. Remedy: "
            "run `swarmsync-serve --fresh` to rotate the old DB aside, or point "
            f"the server back at the original root ({stored!r})."
        )


def stored_managed_root(conn: sqlite3.Connection) -> Optional[str]:
    """Return the repo root this DB is bound to (`meta.managed_root`), or None
    if `bind_managed_root` has never been called on it."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'managed_root'"
    ).fetchone()
    return None if row is None else str(row["value"])
