"""Lease manager: atomic compare-and-swap acquire/release/heartbeat. DESIGN.md §5.2.

Built in Unit U5. This is the load-bearing mutual-exclusion primitive: the safety
net that lets the broker/planner trust its own touch-set predictions, because a
race to double-lease one parcel simply loses on this CAS and serializes instead of
corrupting anything.

acquire(conn, parcel_id, agent_id, mode, ttl) -> LeaseResult
    Single SQL statement (atomic in SQLite regardless of `isolation_level`, since a
    single statement is its own implicit transaction): insert a lease row ONLY IF no
    conflicting active, un-expired lease exists on the same parcel. Conflict rule
    (DESIGN §5.2): read leases are mutually shared; write/exclusive conflicts with
    ANY other active lease (read, write, or exclusive) on the same parcel, and any
    other active lease conflicts with an incoming write/exclusive request:

        (existing.mode IN ('write', 'exclusive') OR incoming.mode IN ('write', 'exclusive'))

    `cursor.rowcount == 1` -> granted (row inserted); `== 0` -> denied (NOT EXISTS
    failed, i.e. a conflicting lease is already active). An expired lease
    (`ttl_expires_at <= now`) is treated as not-active by the same WHERE clause, so
    an acquire against an only-expired holder succeeds without a separate reap step
    -- lazy expiry. The reaper (U11) still exists to *mark* rows `reaped` and emit
    the `reaped` event for observability/reassignment, but correctness here does not
    depend on the reaper having run first.

heartbeat(conn, lease_id, agent_id, ttl) -> bool
    Bumps `heartbeat_at` + `ttl_expires_at` on the caller's own active lease. Scoped
    to `(id, agent_id, status='active')` so a stale/foreign/expired heartbeat is a
    silent no-op (returns False) rather than reviving a lease that lost its CAS race
    or was reaped out from under a crashed agent.

release(conn, lease_id, agent_id) -> bool
    Marks the caller's own active lease `released`. Same ownership scoping as
    heartbeat. After release, the parcel is immediately acquirable again (the WHERE
    NOT EXISTS clause only matches `status='active'`).

Event emission: this module emits `lease_granted` / `lease_denied` / `heartbeat` /
`released` events via `server.events.emit` (U6) -- the shared, single write path
into the `events` table so it really is the one source of truth for replay
(DESIGN §4.1).

CORRECTNESS TEST (U5 done-when): two acquire() calls for the same parcel in write
mode must yield exactly one granted and one denied; a read+read pair must both
grant; after release, the parcel must be acquirable again.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Optional

from swarmsync.blackboard.models import LeaseMode, LeaseResult
from swarmsync.server.events import emit as _emit

DEFAULT_TTL_SECONDS = 30.0


def acquire(
    conn: sqlite3.Connection,
    parcel_id: str,
    agent_id: str,
    mode: LeaseMode = "write",
    ttl: float = DEFAULT_TTL_SECONDS,
    intent: Optional[str] = None,
) -> LeaseResult:
    """Atomic CAS acquire. Returns LeaseResult(granted, lease_id, reason)."""
    if mode not in ("read", "write", "exclusive"):
        raise ValueError(f"unrecognized lease mode: {mode!r}")

    now = time.time()
    ttl_expires_at = now + ttl
    # RETURNING id (not `cur.lastrowid`): U12 is the first unit to genuinely
    # dispatch concurrent CAS acquires from multiple threads against this ONE
    # shared connection (the broker's co-schedulable waves). `cur.lastrowid`
    # is backed by `sqlite3_last_insert_rowid()`, which is a per-CONNECTION
    # (not per-statement) value -- a real race window exists between "this
    # thread's INSERT executes" and "this thread reads lastrowid," during
    # which another thread's INSERT on the same connection can clobber it,
    # handing this acquire() call back a DIFFERENT lease's id (observed:
    # under real concurrency, two distinct grants both reported the same
    # `lease_id`, so the loser's later `release()` silently no-op'd on the
    # wrong row -- ownership-scoped and hence "safe" but leaving the real
    # lease stuck active forever). `RETURNING id` reads the id straight off
    # this statement's own result set, immune to that race.
    cur = conn.execute(
        """
        INSERT INTO leases
            (parcel_id, agent_id, mode, acquired_at, ttl_expires_at, heartbeat_at,
             intent, status)
        SELECT :parcel_id, :agent_id, :mode, :now, :ttl_expires_at, :now,
               :intent, 'active'
        WHERE NOT EXISTS (
            SELECT 1 FROM leases l
            WHERE l.parcel_id = :parcel_id
              AND l.status = 'active'
              AND l.ttl_expires_at > :now
              AND (l.mode IN ('write', 'exclusive') OR :mode IN ('write', 'exclusive'))
        )
        RETURNING id
        """,
        {
            "parcel_id": parcel_id,
            "agent_id": agent_id,
            "mode": mode,
            "now": now,
            "ttl_expires_at": ttl_expires_at,
            "intent": intent,
        },
    )
    row = cur.fetchone()

    if row is not None:
        lease_id = row["id"]
        _emit(
            conn,
            "lease_granted",
            agent_id,
            {"parcel_id": parcel_id, "lease_id": lease_id, "mode": mode},
            ts=now,
        )
        return LeaseResult(granted=True, lease_id=lease_id)

    _emit(
        conn,
        "lease_denied",
        agent_id,
        {"parcel_id": parcel_id, "mode": mode},
        ts=now,
    )
    return LeaseResult(
        granted=False,
        reason=f"conflicting active lease on {parcel_id!r}",
    )


def heartbeat(
    conn: sqlite3.Connection,
    lease_id: int,
    agent_id: str,
    ttl: float = DEFAULT_TTL_SECONDS,
) -> bool:
    """Bump heartbeat_at/ttl_expires_at on the caller's own active lease.

    Returns True if a row was updated, False if the lease doesn't exist, isn't
    owned by `agent_id`, or is no longer active (already released/reaped) --
    heartbeating a lease you no longer hold is a silent no-op, never an error.
    """
    now = time.time()
    cur = conn.execute(
        """
        UPDATE leases
        SET heartbeat_at = :now, ttl_expires_at = :ttl_expires_at
        WHERE id = :lease_id AND agent_id = :agent_id AND status = 'active'
        """,
        {
            "now": now,
            "ttl_expires_at": now + ttl,
            "lease_id": lease_id,
            "agent_id": agent_id,
        },
    )
    ok = cur.rowcount == 1
    if ok:
        _emit(conn, "heartbeat", agent_id, {"lease_id": lease_id}, ts=now)
    return ok


def release(conn: sqlite3.Connection, lease_id: int, agent_id: str) -> bool:
    """Mark the caller's own active lease `released`. Idempotent: releasing an
    already-released/foreign/nonexistent lease id returns False and is a no-op.
    """
    row = conn.execute(
        "SELECT parcel_id FROM leases WHERE id = ? AND agent_id = ? AND status = 'active'",
        (lease_id, agent_id),
    ).fetchone()
    if row is None:
        return False

    now = time.time()
    cur = conn.execute(
        """
        UPDATE leases SET status = 'released'
        WHERE id = :lease_id AND agent_id = :agent_id AND status = 'active'
        """,
        {"lease_id": lease_id, "agent_id": agent_id},
    )
    ok = cur.rowcount == 1
    if ok:
        _emit(
            conn,
            "released",
            agent_id,
            {"lease_id": lease_id, "parcel_id": row["parcel_id"]},
            ts=now,
        )
    return ok
