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

run(conn, interval=1.0, half_life=DEFAULT_HALF_LIFE, iterations=None,
    stop=None, compact_interval=None) -> None (async)
    The background loop: every `interval` seconds, call `reap_once` then
    `decay_once` (plus, throttled to at most once per `compact_interval`,
    `events.compact_events` -- WP3.1's events retention). `iterations`, when
    given, bounds the loop to exactly that many passes and returns instead of
    looping forever -- this is what lets a test exercise `run()` itself
    deterministically without racing a real wall-clock sleep or needing a
    second task to cancel it. When `iterations` is `None` (the real server's
    use, wired from `server/app.py`'s lifespan via `asyncio.create_task`), the
    loop runs until `stop` (an `asyncio.Event` the lifespan sets on shutdown)
    is set -- checked between passes and while sleeping, so an in-flight
    `to_thread` pass always COMPLETES before `run()` returns and the caller
    can close the connection only after the task finishes. Plain
    `task.cancel()` remains a last-resort fallback (see WP3.1 P2 below), not
    the primary shutdown path.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from typing import Optional

from swarmsync.server import events as events_mod

logger = logging.getLogger(__name__)

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

# WP3.1 (finding S2): events-compaction throttle, in seconds. The reaper loop may
# tick every second (or faster in tests), but a full-table DELETE scan every tick
# would be pure waste -- one compaction per minute keeps the heartbeat backlog
# bounded to ~1 minute of overshoot past the retention window.
DEFAULT_COMPACT_INTERVAL = 60.0
COMPACT_INTERVAL_ENV = "SWARMSYNC_EVENTS_COMPACT_INTERVAL"


def _compact_interval_from_env() -> float:
    """Positive float from `SWARMSYNC_EVENTS_COMPACT_INTERVAL`, else the 60s
    default (unset/garbage/non-positive fall back rather than raise)."""
    raw = os.environ.get(COMPACT_INTERVAL_ENV)
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return DEFAULT_COMPACT_INTERVAL
        if value > 0:
            return value
    return DEFAULT_COMPACT_INTERVAL


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
    stop: Optional[asyncio.Event] = None,
    compact_interval: Optional[float] = None,
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

    Resilience (finding C4):

    * Each pass runs OFF the event-loop thread via `asyncio.to_thread`. `reap_once`
      and `decay_once` are blocking SQLite calls on a connection whose
      `busy_timeout` is 5s (`db._configure`); running them inline on the loop
      thread would freeze the entire ASGI server for up to that long on any
      contended write, every interval. Offloading keeps the loop responsive. The
      reaper's dedicated `conn` is used ONLY from these `to_thread` workers and the
      passes are awaited sequentially, so at most one thread ever touches the
      connection at a time -- the same "sequential hand-off across threadpool
      workers" guarantee `server.app.get_conn` relies on, and safe because
      `db.connect` opens the handle with `check_same_thread=False` under SQLite's
      serialized threading mode.
    * Each pass is wrapped in try/except: a transient error (e.g.
      `sqlite3.OperationalError: database is locked` when a write loses the
      busy-timeout race) is logged at WARNING with its traceback and the loop
      CONTINUES. The reaper must never die on a transient error -- previously an
      unguarded raise killed the task for the rest of the process lifetime (no
      reaping, no pheromone decay, and nothing observed the dead task).
      `asyncio.CancelledError` is a `BaseException`, not caught here, so the
      last-resort `task.cancel()` fallback still propagates cleanly.

    Events compaction (WP3.1, finding S2): each pass may additionally run
    `events.compact_events` on the same connection (same `to_thread` pattern),
    THROTTLED to at most one compaction per `compact_interval` seconds (default
    `SWARMSYNC_EVENTS_COMPACT_INTERVAL` env or 60s) regardless of how fast the
    reaper ticks. The first pass always compacts (the throttle bounds the rate,
    not the start), so an `iterations=1` test drive exercises compaction too.

    Deterministic shutdown (WP3.1, adversarial-review P2): `stop`, when given, is
    checked between passes and preempts the inter-pass sleep. Previously the ONLY
    shutdown path was `task.cancel()`, and the CancelledError interrupts the
    AWAIT on `asyncio.to_thread`, not the worker thread inside it -- `run()`
    propagated immediately while `reap_once`/`decay_once` was still executing on
    this connection, the lifespan then closed that connection from the loop
    thread, and the still-running worker hit `sqlite3.ProgrammingError: Cannot
    operate on a closed database` into a discarded future (worst case, closing a
    handle mid-`sqlite3_step` is documented SQLite misuse -- observed as a real
    segfault under the test suite). It also falsified the paragraph above: a
    second thread (the orphaned worker) could touch the connection after run()
    had returned. With `stop`, the lifespan sets the event and AWAITS the task;
    an in-flight pass completes before `run()` returns, so the caller closes the
    connection only after the last worker is done -- the "at most one thread
    ever touches the connection at a time" claim holds again.
    """
    if compact_interval is None:
        compact_interval = _compact_interval_from_env()
    last_compact = float("-inf")
    done = 0
    while True:
        if stop is not None and stop.is_set():
            return
        now = time.time()
        try:
            # Off-thread so a contended 5s busy_timeout write cannot stall the loop.
            await asyncio.to_thread(reap_once, conn, now)
            await asyncio.to_thread(decay_once, conn, half_life, now)
            if now - last_compact >= compact_interval:
                # Mark BEFORE the pass: a failing compaction must not retry every
                # tick (it would spam the log at reaper cadence, not compact cadence).
                last_compact = now
                await asyncio.to_thread(events_mod.compact_events, conn, None, None, now)
        except Exception:
            # Never let a transient error kill the reaper: log + continue. (Does not
            # catch CancelledError, which is a BaseException -- the last-resort
            # cancellation fallback still propagates out of run().)
            logger.warning("reaper pass failed; continuing", exc_info=True)
        done += 1
        if iterations is not None and done >= iterations:
            return
        if stop is None:
            await asyncio.sleep(interval)
        else:
            # Sleep, but wake IMMEDIATELY if shutdown is signalled mid-sleep --
            # `stop.wait()` resolving means "exit now", a timeout means "next pass".
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
