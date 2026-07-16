"""Lease TTL reaper + pheromone decay. DESIGN.md §6 (agent crash mid-edit).

Built in Unit U11. This is the "agent crash mid-edit" line of DESIGN §6's failure
table: heartbeats stop -> the reaper marks the lease `reaped` once its
`ttl_expires_at` passes -> the coordinator/broker (U12) observes the `reaped`
event and reassigns the task; the orphan worktree branch never reached trunk
(merges are gated, U10), so nothing partial poisons the integration branch.

Correctness note (carried over from U5/U6's handoff): `leases.acquire`'s CAS
already treats an expired lease (`ttl_expires_at <= now`) as not-active via lazy
expiry, so a fresh agent can re-acquire a dead agent's parcel *without* the
reaper having run first. The reaper's job is purely observability + bookkeeping
-- flipping `status` from `active` to `reaped` and emitting the `reaped` event
so the blackboard's audit trail (and anything tailing `events`, e.g. the broker)
sees the crash explicitly rather than just noticing the lease quietly aged out.

reap_once(conn, now=None) -> list[int]
    In ONE atomic statement (`UPDATE ... WHERE status='active' AND
    ttl_expires_at<=now RETURNING ...`), flip every timed-out `active` lease to
    `status='reaped'` and emit one `reaped` event per row
    (`payload={"lease_id", "parcel_id", "agent_id"}`). Returns the list of
    lease ids reaped, in `id` order. A no-op (returns `[]`) when nothing is
    past its TTL. The single statement re-checks the ttl at write time, so a
    lease whose ttl was just renewed by a heartbeat is excluded rather than
    reaped out from under the agent still holding it.

decay_once(conn, half_life=DEFAULT_HALF_LIFE, ts=None) -> int
    Thin pass-through to `server.events.decay_pheromone` -- multiplicative
    exponential decay of every pheromone row's `strength` toward 0 (DESIGN §2's
    "decaying pheromone trails"). Kept as its own function (rather than the
    loop calling `events.decay_pheromone` directly) so a test can exercise one
    decay pass in isolation without spinning up the async loop, matching this
    module's own pre-existing docstring contract.

run(conn, interval=1.0, half_life=DEFAULT_HALF_LIFE, iterations=None) -> None (async)
    The background loop: every `interval` seconds, call `reap_once` then
    `decay_once`. `iterations`, when given, bounds the loop to exactly that many
    passes and returns instead of looping forever -- this is what lets a test
    exercise `run()` itself deterministically without racing a real wall-clock
    sleep or needing a second task to cancel it. When `iterations` is `None`
    (the real server's use, wired from `server/app.py`'s lifespan via
    `asyncio.create_task`), the loop runs until the task is cancelled; the
    pending `asyncio.sleep` raises `asyncio.CancelledError` at the next
    scheduling point, which propagates out of `run()` normally -- the caller's
    `task.cancel()` + awaiting the cancelled task is the intended shutdown path,
    same shape as any other asyncio background task.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Optional

from swarmsync.server import events as events_mod

# Pheromone half-life, in seconds. Not prescribed by DESIGN.md (U6's handoff
# note explicitly leaves this constant for U11 to pick) -- 60s means a signal
# dropped at task-declare time ("planned") or on completion ("done") is still
# more than half-strength for about a minute, long enough to be a useful
# "someone is/was just working here" hint to a polling agent (DESIGN §4.3 polls
# `events`/`parcels` on a ~1s cadence) without lingering indefinitely once work
# has moved on.
DEFAULT_HALF_LIFE = 60.0

# Background loop cadence, in seconds. Matches DESIGN §8's explicit choice to
# poll `events` every 1s instead of standing up a push-based bus.
DEFAULT_INTERVAL = 1.0


def reap_once(conn: sqlite3.Connection, now: Optional[float] = None) -> list[int]:
    """Mark every timed-out active lease `reaped` and emit a `reaped` event each.

    A lease is timed out when `ttl_expires_at <= now` -- the same boundary
    `leases.acquire`'s CAS already treats as "not active" (its WHERE clause
    requires `ttl_expires_at > now` to count as blocking), so a lease this
    function reaps was already silently re-acquirable before this call ran.

    Returns the list of `lease_id`s reaped, in `id` order. A no-op (returns
    `[]`) when nothing is past its TTL.
    """
    now = now if now is not None else time.time()

    # ONE atomic statement: select-and-flip in the same UPDATE so the timeout
    # predicate is re-evaluated at write time, not at some earlier read time.
    # The old two-step form (SELECT expired ids, then UPDATE each WHERE
    # status='active') left a TOCTOU window: a heartbeat that renewed a lease's
    # `ttl_expires_at` AFTER the SELECT snapshot but BEFORE the per-row UPDATE
    # would still be reaped, because that UPDATE only re-checked `status`, never
    # the (now-future) ttl. Folding the ttl predicate into the UPDATE's own WHERE
    # means a lease renewed the instant before this statement runs simply fails
    # `ttl_expires_at <= now` and is left `active` -- correct, and atomic against
    # a concurrent heartbeat on the same single-writer connection. RETURNING
    # hands back exactly the rows this statement flipped (mirrors
    # `leases.acquire`'s `RETURNING id`), so there is no separate read to race.
    rows = conn.execute(
        """
        UPDATE leases SET status = 'reaped'
        WHERE status = 'active' AND ttl_expires_at <= ?
        RETURNING id, agent_id, parcel_id
        """,
        (now,),
    ).fetchall()

    # RETURNING row order is unspecified; sort by id so the returned list (and the
    # order events are emitted) is deterministic and matches the documented
    # "in id order" contract.
    rows = sorted(rows, key=lambda r: r["id"])

    reaped_ids: list[int] = []
    for row in rows:
        events_mod.emit(
            conn,
            "reaped",
            row["agent_id"],
            {
                "lease_id": row["id"],
                "parcel_id": row["parcel_id"],
                "agent_id": row["agent_id"],
            },
            ts=now,
        )
        reaped_ids.append(row["id"])

    return reaped_ids


def decay_once(
    conn: sqlite3.Connection,
    half_life: float = DEFAULT_HALF_LIFE,
    ts: Optional[float] = None,
) -> int:
    """One pheromone-decay pass. Thin wrapper over `events.decay_pheromone` so
    the reaper's public surface (`reap_once`/`decay_once`/`run`) is self
    contained and matches this module's pre-existing docstring contract.

    Returns the number of pheromone rows touched (0 on an empty table).
    Propagates `events.decay_pheromone`'s own `ValueError` for a non-positive
    `half_life` -- a caller misconfiguring the constant is a bug, not something
    to swallow here.
    """
    return events_mod.decay_pheromone(conn, half_life, ts=ts)


async def run(
    conn: sqlite3.Connection,
    interval: float = DEFAULT_INTERVAL,
    half_life: float = DEFAULT_HALF_LIFE,
    iterations: Optional[int] = None,
) -> None:
    """The reaper's background loop: wired into `server/app.py`'s lifespan as a
    background `asyncio` task (DESIGN §4.2, §6 "Agent crash mid-edit").

    Every `interval` seconds: `reap_once` then `decay_once`. Runs forever when
    `iterations` is `None` (the real deployment shape -- `server/app.py`'s
    lifespan starts this as an `asyncio.create_task` and cancels it on
    shutdown); bounded to exactly `iterations` passes otherwise, which is what
    lets tests await this coroutine directly instead of cancelling it from
    another task.

    Each pass runs immediately (reap/decay first, sleep after) so a single-shot
    `iterations=1` call actually reaps/decays before returning, rather than only
    sleeping once and doing nothing.
    """
    done = 0
    while True:
        now = time.time()
        reap_once(conn, now)
        decay_once(conn, half_life, ts=now)
        done += 1
        if iterations is not None and done >= iterations:
            return
        await asyncio.sleep(interval)
