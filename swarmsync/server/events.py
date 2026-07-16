"""Append-only event log = pheromone trail + recovery source of truth. DESIGN.md §4.1, §4.3.

Built in Unit U6.

emit(conn, type_, agent_id=None, payload=None, ts=None) -> seq
    Insert into `events` (autoincrement seq, ts=now unless given) and return the
    new row's `seq` read straight off `INSERT ... RETURNING seq` (not the
    per-connection `cur.lastrowid`, which races under concurrent emits -- see the
    inline note). Payload is JSON-serialized. `type_` must be one of `blackboard.models.EventType`'s
    literal values -- catches typos the same way `leases.acquire` rejects an
    unrecognized lease mode. This is the single write path every other module
    (leases, agent runner, coordinator) should funnel through so `events` really
    is the one source of truth for replay (DESIGN §4.1): `server/leases.py`'s
    own private `_emit` has been swapped to call this.

tail(conn, since_seq=0, limit=1000) -> list[Event]
    Ordered (`seq` ascending) events with seq > since_seq, capped at `limit`.
    Agents poll this (default 1s) to stay in sync (DESIGN §4.3 step 1).
    `payload` comes back as the raw JSON string (matching the `Event` model /
    schema column) -- callers that want the structured payload call
    `json.loads(ev.payload)` themselves.

Also houses pheromone helpers (drop/decay) since they ride the same event
stream (DESIGN §2's "decaying pheromone trails"):

drop_pheromone(conn, parcel_id, agent_id, kind, strength, ts=None) -> Pheromone
    Upsert (parcel_id, agent_id, kind) is the PRIMARY KEY in schema.sql) --
    dropping the same (parcel, agent, kind) pheromone again replaces the
    strength/updated_at rather than duplicating a row.

decay_pheromone(conn, half_life, ts=None) -> int
    Multiplicative exponential decay of every pheromone row's `strength`
    toward 0, based on elapsed wall-clock time since that row's `updated_at`:
    `strength *= 0.5 ** (elapsed / half_life)`. `updated_at` is bumped to `now`
    on every row touched, so repeated periodic calls (the U11 decay loop)
    compound correctly off real elapsed time instead of double-decaying.
    Clamped to a floor of 0.0 (exponential decay of a non-negative strength
    can't mathematically go negative, but the floor is explicit per the
    BUILD_PLAN done-when: "never below 0"). Returns the number of rows
    touched; a no-op on an empty table returns 0 without error.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional, get_args

from swarmsync.blackboard.models import EventType, Event, Pheromone

_VALID_EVENT_TYPES = frozenset(get_args(EventType))


def emit(
    conn: sqlite3.Connection,
    type_: str,
    agent_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    ts: Optional[float] = None,
) -> int:
    """Insert one row into the append-only `events` log. Returns the new seq.

    Raises `ValueError` for a `type_` outside `blackboard.models.EventType` --
    a typo here would otherwise silently poison the replay log.
    """
    if type_ not in _VALID_EVENT_TYPES:
        raise ValueError(f"unrecognized event type: {type_!r}")

    # RETURNING seq (not `cur.lastrowid`): mirrors `leases.acquire`'s
    # `RETURNING id`. `cur.lastrowid` is backed by `sqlite3_last_insert_rowid()`,
    # a per-CONNECTION value read after the GIL is re-acquired post-`step`; under
    # concurrent emits on the one shared connection (the broker's parallel
    # waves), another thread's INSERT can land in that window and this call would
    # return the WRONG seq -- two distinct events reporting the same seq, silently
    # corrupting the replay log's identity. Reading `seq` straight off this
    # statement's own RETURNING result set is immune to that race.
    cur = conn.execute(
        "INSERT INTO events (agent_id, type, payload, ts) VALUES (?, ?, ?, ?) "
        "RETURNING seq",
        (
            agent_id,
            type_,
            json.dumps(payload) if payload is not None else None,
            ts if ts is not None else time.time(),
        ),
    )
    return cur.fetchone()["seq"]


def tail(
    conn: sqlite3.Connection,
    since_seq: int = 0,
    limit: int = 1000,
) -> list[Event]:
    """Ordered events with seq > since_seq, oldest first, capped at `limit`."""
    rows = conn.execute(
        "SELECT * FROM events WHERE seq > ? ORDER BY seq ASC LIMIT ?",
        (since_seq, limit),
    ).fetchall()
    return [Event.model_validate(dict(row)) for row in rows]


def drop_pheromone(
    conn: sqlite3.Connection,
    parcel_id: str,
    agent_id: str,
    kind: str,
    strength: float,
    ts: Optional[float] = None,
) -> Pheromone:
    """Upsert a pheromone signal for (parcel_id, agent_id, kind).

    A second drop for the same key replaces strength/updated_at in place
    (the schema's PRIMARY KEY is exactly this triple) rather than duplicating
    a row.
    """
    now = ts if ts is not None else time.time()
    conn.execute(
        """
        INSERT INTO pheromone (parcel_id, agent_id, kind, strength, updated_at)
        VALUES (:parcel_id, :agent_id, :kind, :strength, :now)
        ON CONFLICT (parcel_id, agent_id, kind) DO UPDATE SET
            strength = excluded.strength,
            updated_at = excluded.updated_at
        """,
        {
            "parcel_id": parcel_id,
            "agent_id": agent_id,
            "kind": kind,
            "strength": strength,
            "now": now,
        },
    )
    row = conn.execute(
        "SELECT * FROM pheromone WHERE parcel_id = ? AND agent_id = ? AND kind = ?",
        (parcel_id, agent_id, kind),
    ).fetchone()
    return Pheromone.model_validate(dict(row))


def decay_pheromone(
    conn: sqlite3.Connection,
    half_life: float,
    ts: Optional[float] = None,
) -> int:
    """Multiplicatively decay every pheromone row's strength toward 0.

    `strength *= 0.5 ** (elapsed_since_updated_at / half_life)`, clamped to a
    floor of 0.0. `updated_at` is advanced to `now` for every row touched so a
    repeated call (e.g. a periodic decay loop) measures elapsed time from the
    *last* decay, not the original drop. Returns the count of rows updated;
    a no-op (empty table) returns 0 and never raises.
    """
    if half_life <= 0:
        raise ValueError(f"half_life must be positive, got {half_life!r}")

    now = ts if ts is not None else time.time()
    rows = conn.execute(
        "SELECT parcel_id, agent_id, kind, strength, updated_at FROM pheromone"
    ).fetchall()
    if not rows:
        return 0

    updates = []
    for row in rows:
        elapsed = max(0.0, now - row["updated_at"])  # clock-skew guard
        decayed = max(0.0, row["strength"] * (0.5 ** (elapsed / half_life)))
        updates.append((decayed, now, row["parcel_id"], row["agent_id"], row["kind"]))

    conn.executemany(
        """
        UPDATE pheromone SET strength = ?, updated_at = ?
        WHERE parcel_id = ? AND agent_id = ? AND kind = ?
        """,
        updates,
    )
    return len(updates)
