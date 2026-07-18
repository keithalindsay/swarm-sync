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

    ...EXCEPT the incoming request's OWN same-mode active lease is not a conflict with
    itself (`NOT (l.agent_id = :agent_id AND l.mode = :mode)`). Claude Code batches
    parallel Edit tool calls from ONE agent: two prechecks both see no lease and both
    POST /lease. Without this exemption the second CAS matches the first's just-granted
    write lease and DENIES the agent a parcel it already holds -- self-defeating for the
    batched-edit pattern the lock exists to support. With it, a repeat same-(parcel,
    agent,mode) request is granted (idempotent), while a DIFFERENT agent -- or the same
    agent in a conflicting mode -- is still denied, so real mutual exclusion is intact.
    The exemption is same-statement (inside the atomic CAS itself), so it identifies the
    self-holder atomically and introduces no TOCTOU window.

    Idempotent here is literal (WP3.3): a repeat same-(parcel, agent, mode) request
    first REFRESHES the caller's existing active lease (a single UPDATE with the same
    SQLite-clock liveness predicate as `heartbeat`) and returns `granted=True` with the
    EXISTING lease id, so N re-acquires leave exactly one row and one lease_id the
    caller can heartbeat/release. Only when no own live same-mode lease exists does the
    CAS INSERT run. (Pre-WP3.3 the self-exemption alone made each re-acquire INSERT a
    fresh duplicate row: callers tracking one lease_id could not cleanly free the
    parcel, and the stray duplicate expired un-renewed later, emitting a spurious
    `reaped` event.)

    WP3.3 also bounds the acquire surface itself (findings S1/P2): a per-agent cap on
    active leases (`SWARMSYNC_MAX_LEASES_PER_AGENT`, default 256) and, on the
    `ensure_parcel` path, parcel-id shape validation -- both deny with a reason rather
    than minting unbounded parcels/leases/events rows for any client that can reach
    POST /lease.

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
`released` events via `blackboard.events.emit` (U6) -- the shared, single write path
into the `events` table so the audit log stays complete and uniformly typed. (The
log is audit/observability, not a replay source of truth -- the lease TABLE is the
state of record; see events.py's honesty note.)

CORRECTNESS TEST (U5 done-when): two acquire() calls for the same parcel in write
mode must yield exactly one granted and one denied; a read+read pair must both
grant; after release, the parcel must be acquirable again.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Optional

from swarmsync.blackboard.models import LeaseMode, LeaseResult
from swarmsync.blackboard.events import emit as _emit
from swarmsync.blackboard.parcel_id import split as _split_parcel_id

DEFAULT_TTL_SECONDS = 30.0

# WP3.3 (finding S1/P2): cap how many active leases one agent id may hold at once.
# `POST /lease` with `ensure_parcel=True` otherwise lets any client that clears the
# token gate mint unlimited parcels rows + acquirable leases + events. The default is
# deliberately generous -- a legitimate broker wave leases tens of parcels, not
# hundreds -- and the knob is an env var so an operator with a genuinely huge repo can
# raise it without a code change.
DEFAULT_MAX_LEASES_PER_AGENT = 256
MAX_LEASES_PER_AGENT_ENV = "SWARMSYNC_MAX_LEASES_PER_AGENT"

# WP3.3 (finding S1/P2): bound on parcel-id shape for the `ensure_parcel` auto-create
# path. Legitimate ids are `<relative-path>::<symbol>` (e.g. `path.py::<module>`,
# `pkg/sub/file.ts::<module>`) -- always non-empty, short, single-line, NUL-free. The
# cap only exists to reject abuse (multi-megabyte ids, log-forging newlines, NULs that
# truncate in C consumers), not to constrain any id the indexer or hook can produce.
MAX_PARCEL_ID_LENGTH = 512


def _max_leases_per_agent() -> int:
    """Per-agent active-lease cap, read from the env each call (test-friendly);
    an unset/garbage value falls back to the generous default."""
    raw = os.environ.get(MAX_LEASES_PER_AGENT_ENV)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_MAX_LEASES_PER_AGENT


def _parcel_id_problem(parcel_id: str) -> Optional[str]:
    """Return a human-readable reason to reject `parcel_id`, or None if it is sane.

    Only used on the `ensure_parcel` path: a non-ensure acquire on a bogus id already
    fails on the parcels FK. All ids the classifier/hook legitimately produce
    (relative paths + `::<symbol>`) pass; this rejects only the abuse shapes."""
    if not parcel_id:
        return "parcel_id is empty"
    if len(parcel_id) > MAX_PARCEL_ID_LENGTH:
        return (
            f"parcel_id is {len(parcel_id)} chars long "
            f"(max {MAX_PARCEL_ID_LENGTH})"
        )
    if "\x00" in parcel_id or "\n" in parcel_id or "\r" in parcel_id:
        return "parcel_id contains a NUL or newline character"
    return None

# Epoch seconds from SQLite's OWN clock, evaluated when the statement is serialized
# rather than when Python bound its parameters. Sub-second precision, and it agrees
# with `time.time()` to well under a millisecond, so it is directly comparable to the
# `ttl_expires_at` values Python writes. (`unixepoch('now','subsec')` would be the
# modern spelling but needs SQLite >= 3.42; this expression works on every version we
# support -- 3.37 ships with Ubuntu 22.04.) Used by any predicate whose correctness
# depends on "is this lease alive NOW" rather than "was it alive when I looked".
_NOW_SQL = "((julianday('now') - 2440587.5) * 86400.0)"


def _ensure_parcel(conn: sqlite3.Connection, parcel_id: str) -> None:
    """Create a coarse whole-file parcel row for `parcel_id` if none exists.

    `leases.parcel_id` is an FK to `parcels(id)` with `foreign_keys=ON`, so leasing
    a parcel the classifier never emitted raises IntegrityError. The classifier only
    indexes `*.py`, which left EVERY non-Python file (`.ts`, `.yaml`, `package.json`,
    `Dockerfile`) and every newly-created file unleasable -- and on the hook path
    that surfaced as a 500 the adapter's fail-open umbrella swallowed, so those files
    were silently ungated while the docs promised they were coordinated. Since hook
    subagents share ONE working tree, the lease is the only collision protection
    there and its absence is the absence of the product.

    Callers opt in (`ensure_parcel=True`) rather than getting this for free: the
    broker resolves parcel ids from a real index, so an unknown id there is a bug
    worth surfacing, not a row to conjure. The hook path is the opposite -- it is
    handed arbitrary real files by a human's agent and must coordinate whatever it
    gets.

    The row is written to match what `POST /index` produces for the SAME id, field by
    field, because a later re-index must heal it rather than collide with it, and
    every reader validates it through the same model:

    - `kind='module'`: this mints exactly the indexer's whole-file interstitial id
      (`<path>::<module>`), which `indexer.parse_file` records as `kind="module"`.
      `ParcelKind` is `Literal["function","method","class","module"]` and
      `broker.load_scheduling_graph` `Parcel.model_validate`s EVERY row it selects, so
      an out-of-enum kind (an earlier version wrote `'file'`) raised ValidationError
      and stopped the broker scheduling ANY task, for any file.
    - `symbol=NULL`: the indexer leaves the interstitial's symbol NULL, per
      `schema.sql`'s own "NULL for interstitial (module-glue) parcels". Writing the
      literal `'<module>'` here made the row disagree with both.

    `content_hash` and the byte span are deliberately left NULL: this parcel exists to
    be LEASED, and nothing has parsed the file. A real `POST /index` fills them in.
    """
    path, _symbol = _split_parcel_id(parcel_id)
    conn.execute(
        """
        INSERT OR IGNORE INTO parcels (id, path, kind, symbol, updated_at)
        VALUES (:id, :path, 'module', NULL, :now)
        """,
        {
            "id": parcel_id,
            "path": path,
            "now": time.time(),
        },
    )


def acquire(
    conn: sqlite3.Connection,
    parcel_id: str,
    agent_id: str,
    mode: LeaseMode = "write",
    ttl: float = DEFAULT_TTL_SECONDS,
    intent: Optional[str] = None,
    ensure_parcel: bool = False,
) -> LeaseResult:
    """Atomic CAS acquire. Returns LeaseResult(granted, lease_id, reason).

    `ensure_parcel=True` auto-creates a coarse whole-file parcel row when the id is
    unknown, so any file can be coordinated even if the classifier never parsed it.
    See `_ensure_parcel` for why this is opt-in.
    """
    if mode not in ("read", "write", "exclusive"):
        raise ValueError(f"unrecognized lease mode: {mode!r}")

    now = time.time()
    ttl_expires_at = now + ttl

    # WP3.3 A2: shape-validate the id BEFORE the auto-create can mint a row for it.
    # Scoped to `ensure_parcel` -- without it, an unknown id fails on the parcels FK
    # and a KNOWN id was already validated by whoever created it. Policy denials
    # (this and the quota below) deliberately do NOT emit a `lease_denied` event:
    # one event row per spam request would hand the flood we are bounding an
    # unbounded write path into the events table instead.
    if ensure_parcel:
        problem = _parcel_id_problem(parcel_id)
        if problem is not None:
            return LeaseResult(granted=False, reason=f"invalid parcel_id: {problem}")

    # WP3.3 C1: true idempotency -- FIRST try refreshing the caller's own existing
    # active same-mode lease, so a repeat same-(parcel, agent, mode) acquire returns
    # the EXISTING lease id instead of inserting a duplicate row per call. Liveness is
    # the same SQLite-clock predicate as `heartbeat` (`_NOW_SQL`, not the Python `now`
    # bound above): a stale Python clock here would refresh -- i.e. revive -- a lease
    # that lapsed while the statement waited to serialize, exactly the C13 bug class
    # heartbeat already guards against.
    #
    # Two statements (this UPDATE, then the CAS INSERT below) are TOCTOU-safe: if the
    # UPDATE misses and ANOTHER agent's conflicting lease lands in the gap before our
    # INSERT, the CAS's own NOT EXISTS sees it and cleanly DENIES us -- never a
    # double-grant, because the grant decision itself is still single-statement-atomic.
    # A same-agent thread racing us through the same gap can at worst duplicate-insert
    # via the CAS's self-exemption (the pre-existing batched-race residue, now confined
    # to genuinely simultaneous requests); every SEQUENTIAL re-acquire lands here and
    # converges on one row.
    refreshed = conn.execute(
        f"""
        UPDATE leases
        SET heartbeat_at = {_NOW_SQL},
            ttl_expires_at = {_NOW_SQL} + :ttl,
            intent = COALESCE(:intent, intent)
        WHERE parcel_id = :parcel_id AND agent_id = :agent_id AND mode = :mode
          AND status = 'active' AND ttl_expires_at > {_NOW_SQL}
        RETURNING id
        """,  # noqa: S608 - _NOW_SQL is a module constant, never caller input
        {
            "ttl": ttl,
            "intent": intent,
            "parcel_id": parcel_id,
            "agent_id": agent_id,
            "mode": mode,
        },
    ).fetchall()
    if refreshed:
        # Should legacy/batched-race duplicates exist, all were refreshed above (none
        # left to lapse into a spurious `reaped` event); report the first id.
        lease_id = refreshed[0]["id"]
        _emit(
            conn,
            "lease_granted",
            agent_id,
            {"parcel_id": parcel_id, "lease_id": lease_id, "mode": mode},
            ts=now,
        )
        return LeaseResult(granted=True, lease_id=lease_id)

    # WP3.3 A1: per-agent active-lease cap. Checked AFTER the refresh path (renewing a
    # lease you already hold adds no rows, so the cap must never block it) and BEFORE
    # `_ensure_parcel` (a capped agent must not keep minting parcels rows either). The
    # count and the INSERT below are two statements, not one atomic step: N requests
    # racing at the cap can each pass the count and over-admit by a few. That is
    # acceptable -- this is a DoS bound on unbounded resource minting, not an exact
    # admission quota, and the overshoot is bounded by the number of in-flight racers.
    max_leases = _max_leases_per_agent()
    active_count = conn.execute(
        f"""
        SELECT COUNT(*) FROM leases
        WHERE agent_id = :agent_id AND status = 'active'
          AND ttl_expires_at > {_NOW_SQL}
        """,  # noqa: S608 - _NOW_SQL is a module constant, never caller input
        {"agent_id": agent_id},
    ).fetchone()[0]
    if active_count >= max_leases:
        # No single holder explains a quota denial, so the holder fields stay None.
        return LeaseResult(
            granted=False,
            reason=(
                f"per-agent lease cap reached: {agent_id!r} already holds "
                f"{active_count} active leases (cap {max_leases}; raise "
                f"{MAX_LEASES_PER_AGENT_ENV} if this is legitimate)"
            ),
        )

    if ensure_parcel:
        _ensure_parcel(conn, parcel_id)
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
              AND NOT (l.agent_id = :agent_id AND l.mode = :mode)
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

    # Name the holder that best explains the denial so the caller (the hook adapter)
    # need not make a second /leases round-trip. This mirrors the CAS's own conflict
    # predicate exactly -- same parcel, same active/liveness/conflict/self-exemption
    # clauses -- but tests liveness against SQLITE'S clock (`_NOW_SQL`), NOT the Python
    # `:now` bound before execute, matching `heartbeat`. A stale Python `now` would name
    # a holder whose lease has since lapsed (the reverse of a fresh clock, which surfaces
    # None -> "no single holder identifiable", the honest answer). This lookup is purely
    # descriptive; the deny itself was already decided atomically by the CAS above.
    #
    # Tie-break when several leases block (e.g. multiple readers vs. an incoming write):
    #   1. a write/exclusive holder first -- it is the exclusive owner and the one whose
    #      release actually frees the parcel;
    #   2. otherwise the SOONEST-to-expire (smallest ttl_expires_at) -- the reader that
    #      frees the parcel first, i.e. the earliest moment the caller could retry.
    holder_row = conn.execute(
        f"""
        SELECT l.agent_id AS holder, l.mode AS holder_mode,
               l.ttl_expires_at AS holder_ttl_expires_at
        FROM leases l
        WHERE l.parcel_id = :parcel_id
          AND l.status = 'active'
          AND l.ttl_expires_at > {_NOW_SQL}
          AND (l.mode IN ('write', 'exclusive') OR :mode IN ('write', 'exclusive'))
          AND NOT (l.agent_id = :agent_id AND l.mode = :mode)
        ORDER BY (l.mode IN ('write', 'exclusive')) DESC, l.ttl_expires_at ASC
        LIMIT 1
        """,  # noqa: S608 - _NOW_SQL is a module constant, never caller input
        {"parcel_id": parcel_id, "mode": mode, "agent_id": agent_id},
    ).fetchone()

    holder: Optional[str] = None
    holder_ttl_expires_at: Optional[float] = None
    reason = f"conflicting active lease on {parcel_id!r}"
    if holder_row is not None:
        holder = holder_row["holder"]
        holder_ttl_expires_at = holder_row["holder_ttl_expires_at"]
        if holder == agent_id:
            # WP3.3 C2: the best-explaining holder IS the requester (a cross-mode
            # self-conflict, e.g. holding 'write' while requesting 'exclusive'). The
            # deny is correct -- the modes really conflict -- but "held by 'you'"
            # phrased as a third party tells the agent to wait for itself, which never
            # resolves. Say it is their own lease; holder fields stay truthful.
            reason = (
                f"your own {holder_row['holder_mode']!r} lease on {parcel_id!r} "
                f"conflicts with the requested {mode!r} (release it first)"
            )
        else:
            reason = f"conflicting active lease on {parcel_id!r} held by {holder!r}"

    return LeaseResult(
        granted=False,
        reason=reason,
        holder=holder,
        holder_ttl_expires_at=holder_ttl_expires_at,
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
    # The liveness predicate is load-bearing, not belt-and-braces: acquire() uses lazy
    # expiry (an expired holder does not block a new acquire), so between the moment a
    # lease expires and the moment the reaper flips its status, the row is still
    # `status='active'` while another agent can lawfully take the parcel. Without it a
    # blind heartbeat from the original holder pushes that row back into the future and
    # BOTH rows are then active+unexpired+write on one parcel -- the exact double-lease
    # this module exists to prevent. It keeps correctness independent of the reaper
    # having run, as this module's docstring promises.
    #
    # It compares against SQLITE'S clock (`_NOW_SQL`), not a Python `time.time()` bound
    # before `execute`. A Python-side timestamp answers "was this lease alive when I
    # read the clock?", and the statement may serialize arbitrarily later (GIL
    # preemption, a busy_timeout wait of up to 5s, threadpool queueing, an event-loop
    # stall). If the lease lapses inside that gap while another agent lawfully acquires
    # the parcel, a stale `:now` still satisfies the predicate and revives the dead
    # lease -- reopening the double-lease with extra steps.
    #
    # heartbeat is the ONLY one of the four predicates where clock staleness points the
    # unsafe way: a stale `now` in acquire's `l.ttl_expires_at > :now` sees a conflict
    # as MORE live (denies -> fails safe), and a stale `now` in reap_once's
    # `ttl_expires_at <= :now` reaps FEWER rows (fails safe). Here it revives. `ttl` is
    # a caller-supplied, unvalidated knob (POST /lease {"ttl":...}, run_agent(lease_ttl=)),
    # so a deployment that shortens it toward request latency would reopen this in full.
    cur = conn.execute(
        f"""
        UPDATE leases
        SET heartbeat_at = {_NOW_SQL}, ttl_expires_at = {_NOW_SQL} + :ttl
        WHERE id = :lease_id AND agent_id = :agent_id AND status = 'active'
          AND ttl_expires_at > {_NOW_SQL}
        """,  # noqa: S608 - _NOW_SQL is a module constant, never caller input
        {
            "ttl": ttl,
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
