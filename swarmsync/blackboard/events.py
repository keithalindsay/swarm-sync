"""Append-only event log = pheromone trail + audit trail. DESIGN.md §4.1, §4.3.

Honesty note (C7/WP3.4): the events table is an AUDIT log, not a replay source of
truth. State writes and their event emits are separate autocommit statements (a
crash can separate them), several mutations legitimately emit nothing (run_index,
_ensure_parcel, pheromone decay), and crash recovery reads the `open_integrations`
projection (WP3.2), never this log. The SQLite tables are the state of record.

Built in Unit U6.

emit(conn, type_, agent_id=None, payload=None, ts=None) -> seq
    Insert into `events` (autoincrement seq, ts=now unless given) and return the
    new row's `seq` read straight off `INSERT ... RETURNING seq` (not the
    per-connection `cur.lastrowid`, which races under concurrent emits -- see the
    inline note). Payload is JSON-serialized. `type_` must be one of `blackboard.models.EventType`'s
    literal values -- catches typos the same way `leases.acquire` rejects an
    unrecognized lease mode. This is the single write path every other module
    (leases, agent runner, coordinator) should funnel through so the audit log
    stays complete and uniformly typed: `blackboard/leases.py`'s own private `_emit`
    has been swapped to call this.

tail(conn, since_seq=0, limit=1000) -> list[Event]
    Ordered (`seq` ascending) events with seq > since_seq, capped at `limit`.
    Agents poll this (default 1s) to stay in sync (DESIGN §4.3 step 1).
    `payload` comes back as the raw JSON string (matching the `Event` model /
    schema column) -- callers that want the structured payload call
    `json.loads(ev.payload)` themselves.

compact_events(conn, heartbeat_max_age=None, max_age=None, now=None) -> int
    Retention/compaction for the `events` table (WP3.1, finding S2): prune
    heartbeat-class events older than a short window and ANY event older than a
    long horizon, never touching a seq referenced by
    `open_integrations.started_seq`. See its docstring for the full contract.

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
import os
import sqlite3
import time
from typing import Any, Optional, get_args

from swarmsync.blackboard.models import EventType, Event, Pheromone

_VALID_EVENT_TYPES = frozenset(get_args(EventType))

# --- WP3.1 (finding S2): events retention/compaction ------------------------------

# The marker event one compaction pass leaves behind when it pruned anything.
# NOT in `blackboard.models.EventType`: the registry (and the schema comment
# mirroring it) is owned by models.py, which is frozen to a parallel work
# package -- so `compact_events` INSERTs this row directly (bypassing `emit`'s
# registry check, deliberately and only here) and `tail` constructs it without
# Literal validation. The events table has no CHECK on `type`, so the row is
# schema-legal.
EVENTS_COMPACTED = "events_compacted"

# Heartbeat-class event types: the per-renewal keepalive traffic that dominates
# the log's growth. From reading every emit site: `heartbeat`
# (`blackboard/leases.py::heartbeat`) is emitted on EVERY successful TTL renewal --
# each agent renews each held lease every few seconds for as long as it works,
# so these rows outnumber everything else by orders of magnitude. Every other
# type is per-action (planned / lease_granted / lease_denied / done / released /
# reaped / merge verdicts / reindexed / needs_rebase / integrate_*) and carries
# real audit value, so only `heartbeat` gets the short retention window.
HEARTBEAT_EVENT_TYPES = frozenset({"heartbeat"})

# Short window for heartbeat-class events, in seconds (default 1 hour).
DEFAULT_HEARTBEAT_MAX_AGE = 3600.0
HEARTBEAT_MAX_AGE_ENV = "SWARMSYNC_EVENTS_HEARTBEAT_MAX_AGE"

# Long horizon for ANY event, in seconds (default 7 days).
DEFAULT_EVENT_MAX_AGE = 7 * 86400.0
EVENT_MAX_AGE_ENV = "SWARMSYNC_EVENTS_MAX_AGE"


def _age_from_env(env_var: str, default: float) -> float:
    """Positive float from `env_var`, else `default` (unset/garbage/non-positive
    fall back rather than raise -- same posture as `app._max_body_bytes`)."""
    raw = os.environ.get(env_var)
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return default
        if value > 0:
            return value
    return default


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
    out: list[Event] = []
    for row in rows:
        data = dict(row)
        if data["type"] in _VALID_EVENT_TYPES:
            out.append(Event.model_validate(data))
        else:
            # Maintenance rows (`events_compacted`) carry a type outside the frozen
            # EventType registry (owned by models.py -- see EVENTS_COMPACTED above).
            # They come from our own compactor writing our own table, so constructing
            # without Literal validation is safe; dropping them instead would hide
            # the compaction audit trail from every tailer.
            out.append(Event.model_construct(**data))
    return out


def compact_events(
    conn: sqlite3.Connection,
    heartbeat_max_age: Optional[float] = None,
    max_age: Optional[float] = None,
    now: Optional[float] = None,
) -> int:
    """One retention/compaction pass over `events` (WP3.1, finding S2). Returns
    the number of rows pruned (0 on a no-op).

    Prunes, in ONE statement:
      * heartbeat-class events (`HEARTBEAT_EVENT_TYPES` -- the keepalive renewals
        that dominate growth) older than `heartbeat_max_age` (default
        `SWARMSYNC_EVENTS_HEARTBEAT_MAX_AGE` env or 1 hour);
      * ANY event older than `max_age` (default `SWARMSYNC_EVENTS_MAX_AGE` env or
        7 days);
      * NEVER an event whose `seq` appears in `open_integrations.started_seq`:
        an `integrate_started` row with no terminal verdict is exactly what
        startup crash-recovery resets trunk from, so those starts survive
        unconditionally, however old.

    When anything was pruned, ONE `events_compacted` marker event is inserted
    carrying the pruned count and the [seq_min, seq_max] range; a no-op pass
    inserts nothing (the compactor must not become its own growth source).

    Recovery-safety: since WP3.2 (finding C3), crash recovery
    (`coordinator.integrator.reconcile_orphaned_integrations`) reads the
    `open_integrations` PROJECTION, not the event log -- so compaction cannot
    break recovery. The events table is an audit log; SQLite table state is the
    source of truth. The `started_seq` guard above additionally keeps the
    audit row behind any still-open integrate intact.

    Deletes are seq-keyed (the PRIMARY KEY), and the audit-valued history
    (merged / integrate_orphaned / ...) younger than `max_age` is untouched --
    only heartbeat-class types get the short window.
    """
    now = now if now is not None else time.time()
    hb_age = (
        heartbeat_max_age
        if heartbeat_max_age is not None
        else _age_from_env(HEARTBEAT_MAX_AGE_ENV, DEFAULT_HEARTBEAT_MAX_AGE)
    )
    horizon = (
        max_age if max_age is not None else _age_from_env(EVENT_MAX_AGE_ENV, DEFAULT_EVENT_MAX_AGE)
    )

    type_marks = ",".join("?" * len(HEARTBEAT_EVENT_TYPES))
    # RETURNING seq: the pruned count and seq range come off this statement's own
    # result set (house style -- same reason emit uses RETURNING), no second read.
    rows = conn.execute(
        f"""
        DELETE FROM events
        WHERE ((type IN ({type_marks}) AND ts <= ?) OR ts <= ?)
          AND seq NOT IN (SELECT started_seq FROM open_integrations)
        RETURNING seq
        """,  # noqa: S608 - type_marks is derived from a module constant, never caller input
        (*sorted(HEARTBEAT_EVENT_TYPES), now - hb_age, now - horizon),
    ).fetchall()
    if not rows:
        return 0

    seqs = [row["seq"] for row in rows]
    # Direct INSERT, not `emit`: EVENTS_COMPACTED is outside the models.py
    # EventType registry (frozen to a parallel WP; see the constant's comment),
    # and emit's registry check must stay strict for every other caller.
    conn.execute(
        "INSERT INTO events (agent_id, type, payload, ts) VALUES (?, ?, ?, ?)",
        (
            None,
            EVENTS_COMPACTED,
            json.dumps(
                {
                    "pruned": len(seqs),
                    "seq_min": min(seqs),
                    "seq_max": max(seqs),
                    "heartbeat_max_age": hb_age,
                    "max_age": horizon,
                }
            ),
            now,
        ),
    )
    return len(seqs)


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
