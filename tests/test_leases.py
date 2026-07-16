"""U5 — Lease manager (atomic CAS). DESIGN.md §5.2.

Done when:
  - two acquire() calls on the same parcel in write mode yield exactly one
    `granted` and one `denied`
  - a read+read pair both grant
  - after `release`, the parcel is acquirable again
"""
from __future__ import annotations

import time

import pytest

from swarmsync.blackboard import db
from swarmsync.server import leases


@pytest.fixture()
def conn(tmp_path):
    c = db.init_db(tmp_path / "blackboard.db")
    yield c
    c.close()


def _make_parcel(conn, parcel_id="a.py::foo"):
    now = time.time()
    conn.execute(
        "INSERT INTO parcels (id, path, symbol, kind, blast_radius, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (parcel_id, "a.py", "foo", "function", 0, now),
    )
    return parcel_id


def _events(conn, type_=None):
    if type_ is None:
        return conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
    return conn.execute(
        "SELECT * FROM events WHERE type = ? ORDER BY seq", (type_,)
    ).fetchall()


# --- done-when: write/write contention -> exactly one granted, one denied --------


def test_two_write_acquires_yield_one_granted_one_denied(conn):
    parcel_id = _make_parcel(conn)

    r1 = leases.acquire(conn, parcel_id, "agent-1", mode="write")
    r2 = leases.acquire(conn, parcel_id, "agent-2", mode="write")

    results = [r1, r2]
    granted = [r for r in results if r.granted]
    denied = [r for r in results if not r.granted]
    assert len(granted) == 1
    assert len(denied) == 1
    assert granted[0].lease_id is not None
    assert denied[0].lease_id is None
    assert denied[0].reason is not None

    # exactly one lease row inserted (the denied path must not insert)
    rows = conn.execute(
        "SELECT * FROM leases WHERE parcel_id = ?", (parcel_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "active"

    # events: one lease_granted, one lease_denied
    assert len(_events(conn, "lease_granted")) == 1
    assert len(_events(conn, "lease_denied")) == 1


# --- done-when: read+read both grant ----------------------------------------------


def test_read_read_pair_both_grant(conn):
    parcel_id = _make_parcel(conn)

    r1 = leases.acquire(conn, parcel_id, "agent-1", mode="read")
    r2 = leases.acquire(conn, parcel_id, "agent-2", mode="read")

    assert r1.granted
    assert r2.granted
    assert r1.lease_id != r2.lease_id

    rows = conn.execute(
        "SELECT * FROM leases WHERE parcel_id = ? AND status='active'", (parcel_id,)
    ).fetchall()
    assert len(rows) == 2
    assert len(_events(conn, "lease_granted")) == 2
    assert len(_events(conn, "lease_denied")) == 0


# --- done-when: after release, parcel is acquirable again -------------------------


def test_release_frees_the_parcel_for_a_new_write_acquire(conn):
    parcel_id = _make_parcel(conn)

    r1 = leases.acquire(conn, parcel_id, "agent-1", mode="write")
    assert r1.granted

    # a second writer is denied while the first holds it
    r2 = leases.acquire(conn, parcel_id, "agent-2", mode="write")
    assert not r2.granted

    released = leases.release(conn, r1.lease_id, "agent-1")
    assert released is True

    row = conn.execute(
        "SELECT status FROM leases WHERE id = ?", (r1.lease_id,)
    ).fetchone()
    assert row["status"] == "released"

    # now a new write acquire succeeds
    r3 = leases.acquire(conn, parcel_id, "agent-2", mode="write")
    assert r3.granted
    assert len(_events(conn, "released")) == 1


# --- additional coverage: read vs write conflicts, both directions ---------------


def test_read_then_write_is_denied(conn):
    parcel_id = _make_parcel(conn)
    r1 = leases.acquire(conn, parcel_id, "agent-1", mode="read")
    r2 = leases.acquire(conn, parcel_id, "agent-2", mode="write")
    assert r1.granted
    assert not r2.granted


def test_write_then_read_is_denied(conn):
    parcel_id = _make_parcel(conn)
    r1 = leases.acquire(conn, parcel_id, "agent-1", mode="write")
    r2 = leases.acquire(conn, parcel_id, "agent-2", mode="read")
    assert r1.granted
    assert not r2.granted


def test_exclusive_conflicts_with_everything(conn):
    parcel_id = _make_parcel(conn)
    r1 = leases.acquire(conn, parcel_id, "agent-1", mode="exclusive")
    assert r1.granted
    for mode in ("read", "write", "exclusive"):
        r = leases.acquire(conn, parcel_id, "agent-2", mode=mode)
        assert not r.granted


def test_different_parcels_do_not_conflict(conn):
    p1 = _make_parcel(conn, "a.py::foo")
    p2 = _make_parcel(conn, "b.py::bar")
    r1 = leases.acquire(conn, p1, "agent-1", mode="write")
    r2 = leases.acquire(conn, p2, "agent-2", mode="write")
    assert r1.granted
    assert r2.granted


# --- expiry: an expired lease no longer blocks a new acquire (lazy expiry) -------


def test_expired_lease_does_not_block_new_acquire(conn):
    parcel_id = _make_parcel(conn)
    r1 = leases.acquire(conn, parcel_id, "agent-1", mode="write", ttl=-1.0)
    assert r1.granted  # granted at the time, but ttl_expires_at is already in the past

    r2 = leases.acquire(conn, parcel_id, "agent-2", mode="write")
    assert r2.granted


# --- heartbeat -------------------------------------------------------------------


def test_heartbeat_bumps_ttl_for_owner(conn):
    parcel_id = _make_parcel(conn)
    r1 = leases.acquire(conn, parcel_id, "agent-1", mode="write", ttl=5.0)
    before = conn.execute(
        "SELECT ttl_expires_at, heartbeat_at FROM leases WHERE id = ?", (r1.lease_id,)
    ).fetchone()

    time.sleep(0.01)
    ok = leases.heartbeat(conn, r1.lease_id, "agent-1", ttl=5.0)
    assert ok is True

    after = conn.execute(
        "SELECT ttl_expires_at, heartbeat_at FROM leases WHERE id = ?", (r1.lease_id,)
    ).fetchone()
    assert after["heartbeat_at"] > before["heartbeat_at"]
    assert after["ttl_expires_at"] > before["ttl_expires_at"]
    assert len(_events(conn, "heartbeat")) == 1


def test_heartbeat_by_non_owner_is_a_noop(conn):
    parcel_id = _make_parcel(conn)
    r1 = leases.acquire(conn, parcel_id, "agent-1", mode="write")
    ok = leases.heartbeat(conn, r1.lease_id, "agent-2")
    assert ok is False
    assert len(_events(conn, "heartbeat")) == 0


def test_heartbeat_on_unknown_lease_is_a_noop(conn):
    ok = leases.heartbeat(conn, 9999, "agent-1")
    assert ok is False


def test_heartbeat_on_released_lease_is_a_noop(conn):
    parcel_id = _make_parcel(conn)
    r1 = leases.acquire(conn, parcel_id, "agent-1", mode="write")
    leases.release(conn, r1.lease_id, "agent-1")
    ok = leases.heartbeat(conn, r1.lease_id, "agent-1")
    assert ok is False


# --- release edge cases -----------------------------------------------------------


def test_release_by_non_owner_is_a_noop(conn):
    parcel_id = _make_parcel(conn)
    r1 = leases.acquire(conn, parcel_id, "agent-1", mode="write")
    ok = leases.release(conn, r1.lease_id, "agent-2")
    assert ok is False
    row = conn.execute(
        "SELECT status FROM leases WHERE id = ?", (r1.lease_id,)
    ).fetchone()
    assert row["status"] == "active"


def test_release_twice_is_idempotent_noop_second_time(conn):
    parcel_id = _make_parcel(conn)
    r1 = leases.acquire(conn, parcel_id, "agent-1", mode="write")
    assert leases.release(conn, r1.lease_id, "agent-1") is True
    assert leases.release(conn, r1.lease_id, "agent-1") is False


def test_release_unknown_lease_returns_false(conn):
    assert leases.release(conn, 9999, "agent-1") is False


# --- acquire input validation ------------------------------------------------------


def test_acquire_rejects_unknown_mode(conn):
    parcel_id = _make_parcel(conn)
    with pytest.raises(ValueError):
        leases.acquire(conn, parcel_id, "agent-1", mode="bogus")


def test_acquire_on_nonexistent_parcel_raises_integrity_error(conn):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        leases.acquire(conn, "does.not.exist", "agent-1", mode="write")


# --- concurrency: barrier-gated CAS is exactly-once, and returns each grant's
#     own lease id (pins the `sqlite3_last_insert_rowid()` race, DESIGN §5.2) -------


def test_barrier_gated_write_acquires_on_one_parcel_grant_exactly_once(conn):
    """N threads race a write-acquire on the SAME parcel, released together by a
    barrier to maximize contention. The CAS must grant EXACTLY ONE of them and
    leave EXACTLY ONE active lease row -- the load-bearing mutual-exclusion
    guarantee (DESIGN §5.2). The granted lease id must be the id of that one
    active row (not some sibling's, which the old `cur.lastrowid` read could
    have handed back)."""
    import threading

    parcel_id = _make_parcel(conn)
    n_threads = 24
    barrier = threading.Barrier(n_threads)
    results: list[object] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()  # release all contenders at once
        r = leases.acquire(conn, parcel_id, f"agent-{i}", mode="write")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    granted = [r for r in results if r.granted]
    denied = [r for r in results if not r.granted]
    assert len(granted) == 1, f"expected exactly 1 grant, got {len(granted)}"
    assert len(denied) == n_threads - 1

    active = conn.execute(
        "SELECT id FROM leases WHERE parcel_id = ? AND status = 'active'", (parcel_id,)
    ).fetchall()
    assert len(active) == 1, "exactly one active lease must exist on a contended parcel"
    # the grant reported ITS OWN row's id, not a racing sibling insert's.
    assert granted[0].lease_id == active[0]["id"]


def test_barrier_gated_acquires_on_distinct_parcels_return_distinct_lease_ids(conn):
    """N threads each write-acquire their OWN distinct parcel behind a barrier.
    Distinct parcels never conflict, so ALL must grant -- and each returned
    `lease_id` must be that acquire's OWN inserted row (a clean bijection).

    This pins the `emit`/`acquire` lastrowid regression: `cur.lastrowid` reads
    the per-CONNECTION `sqlite3_last_insert_rowid()` after the GIL is re-acquired
    post-step, so a sibling INSERT on this one shared connection lands in that
    window and hands the call back a DIFFERENT lease's id. `INSERT ... RETURNING
    id` reads each statement's own result and is immune. Empirically the old
    form makes distinct grants collide on the same id here."""
    import threading

    n_threads, per_thread = 8, 12
    total = n_threads * per_thread
    # id -> the parcel that acquire actually created a row for.
    for k in range(total):
        _make_parcel(conn, parcel_id=f"p{k}.py::foo")

    barrier = threading.Barrier(n_threads)
    observed: list[tuple[str, int]] = []  # (requested_parcel_id, returned_lease_id)
    lock = threading.Lock()

    def worker(t_idx: int) -> None:
        my_parcels = [f"p{t_idx * per_thread + j}.py::foo" for j in range(per_thread)]
        barrier.wait()
        local = []
        for pid in my_parcels:
            r = leases.acquire(conn, pid, f"agent-{t_idx}", mode="write")
            assert r.granted, f"distinct parcel {pid} should never be denied"
            local.append((pid, r.lease_id))
        with lock:
            observed.extend(local)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(observed) == total
    returned_ids = [lid for _pid, lid in observed]
    assert len(set(returned_ids)) == total, "concurrent acquires returned duplicate lease ids"

    # Every returned id truly belongs to the parcel that acquire was called for
    # (the id was not clobbered by a sibling insert on the shared connection).
    row_parcel = {
        row["id"]: row["parcel_id"]
        for row in conn.execute("SELECT id, parcel_id FROM leases").fetchall()
    }
    for requested_pid, returned_id in observed:
        assert row_parcel[returned_id] == requested_pid


# --- R3 P1-1: expired leases must not be resurrected by a blind heartbeat ----------


def test_heartbeat_on_expired_lease_is_a_noop(conn):
    """A heartbeat may not revive a lease whose TTL already lapsed.

    `leases.py`'s docstring promises "a stale/foreign/EXPIRED heartbeat is a silent
    no-op ... rather than reviving a lease", and the reaper note promises
    "correctness here does not depend on the reaper having run first". Both are only
    true if `heartbeat` carries the same `ttl_expires_at > now` predicate `acquire`
    uses. Deleting that clause makes this test fail.
    """
    parcel_id = _make_parcel(conn)
    r = leases.acquire(conn, parcel_id, "agent-A", mode="write", ttl=0.05)
    assert r.granted is True
    time.sleep(0.1)

    assert leases.heartbeat(conn, r.lease_id, "agent-A", ttl=10_000.0) is False

    row = conn.execute(
        "SELECT ttl_expires_at FROM leases WHERE id = ?", (r.lease_id,)
    ).fetchone()
    assert row["ttl_expires_at"] <= time.time(), "expired lease was pushed into the future"


def test_expired_holders_heartbeat_cannot_create_a_second_live_write_lease(conn):
    """The double-lease invariant, stated as the thing it actually protects.

    NO reaper runs here -- deliberately. `acquire` uses lazy expiry, so between a
    lease lapsing and the reaper flipping its status there is a window in which the
    row is still `status='active'` while another agent can lawfully take the parcel.
    If the original holder's heartbeat can push that row's TTL forward in that
    window, two agents hold the same parcel in write mode at once -- the one thing
    this module exists to prevent. `create_app(reaper_interval=None)` is a supported
    config, and the reaper can die, so this must hold with no reaper at all.
    """
    parcel_id = _make_parcel(conn)
    a = leases.acquire(conn, parcel_id, "agent-A", mode="write", ttl=0.05)
    assert a.granted is True
    time.sleep(0.1)

    # B lawfully takes the parcel: A's lease has lapsed (lazy expiry).
    b = leases.acquire(conn, parcel_id, "agent-B", mode="write", ttl=30.0)
    assert b.granted is True

    # A is oblivious and keeps beating (runner's _Heartbeater beats blindly on a
    # timer and swallows every exception -- exactly this client).
    leases.heartbeat(conn, a.lease_id, "agent-A", ttl=10_000.0)

    live = conn.execute(
        "SELECT id, agent_id FROM leases "
        "WHERE parcel_id = ? AND status = 'active' AND ttl_expires_at > ?",
        (parcel_id, time.time()),
    ).fetchall()
    assert len(live) == 1, (
        f"DOUBLE LEASE: {[(r['id'], r['agent_id']) for r in live]} "
        f"hold {parcel_id!r} in write mode simultaneously"
    )
    assert live[0]["agent_id"] == "agent-B"


def test_ensure_parcel_row_is_indistinguishable_from_an_indexed_module_parcel(conn):
    """An auto-created parcel must be a row the rest of the system can READ.

    R4 caught this: `_ensure_parcel` originally wrote `kind='file'`, which is not in
    `ParcelKind` (`Literal["function","method","class","module"]`). Every read path
    validates through that model, and `broker.load_scheduling_graph` does
    `SELECT * FROM parcels` + `Parcel.model_validate` on EVERY row -- so one hook
    edit to one `package.json` raised ValidationError and stopped the broker from
    scheduling ANY task, for any file. The hook's own tests never saw it because
    nothing on the hook path reads parcels back through the model.

    The id minted here is exactly the indexer's whole-file interstitial
    (`<path>::<module>`), so the row must match what a later POST /index produces.
    """
    from swarmsync.blackboard.models import Parcel
    from swarmsync.classifier.indexer import MODULE_SYMBOL

    r = leases.acquire(
        conn, f"package.json::{MODULE_SYMBOL}", "agent-1", mode="write", ensure_parcel=True
    )
    assert r.granted is True

    row = conn.execute(
        "SELECT * FROM parcels WHERE id = ?", (f"package.json::{MODULE_SYMBOL}",)
    ).fetchone()
    assert row is not None

    # The row must survive the model every reader validates through.
    parcel = Parcel.model_validate(dict(row))
    assert parcel.kind == "module", (
        f"auto-created parcel kind {parcel.kind!r} is not what the indexer emits for "
        f"a <module> interstitial; readers that validate through ParcelKind will break"
    )
    assert parcel.path == "package.json"
    assert parcel.symbol == MODULE_SYMBOL


def test_hook_leasing_an_unindexed_file_does_not_brick_the_broker(conn, tmp_path):
    """The consequence, end to end: the broker must still schedule after a hook
    lease on a non-.py file. `load_scheduling_graph` validates every parcel row, so
    a single bad row is not a local defect -- it is total loss of dispatch."""
    from swarmsync.classifier.store import run_index
    from swarmsync.coordinator import broker

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    run_index(conn, repo)
    assert broker.load_scheduling_graph(conn, repo) is not None

    # Exactly what the hook does for an unindexed file.
    leases.acquire(conn, "package.json::<module>", "agent-1", mode="write", ensure_parcel=True)

    graph, frozen = broker.load_scheduling_graph(conn, repo)
    assert graph is not None, "one hook-leased non-.py file broke ALL broker dispatch"


def test_heartbeat_predicate_uses_sqlites_clock_not_a_stale_python_one(conn):
    """The liveness check must be evaluated at WRITE time, not at bind time.

    R4 found R3's P1-1 fix agreed with `acquire` textually but not semantically: it
    compared against a Python `time.time()` read before `conn.execute`, so the
    predicate answered "was this lease alive when I read the clock?". The statement
    can serialize arbitrarily later (GIL preemption, a busy_timeout wait of up to 5s,
    an event-loop stall). If the lease lapses in that gap while another agent lawfully
    takes the parcel, the stale timestamp still satisfies the predicate and revives
    the dead lease -- the double-lease again.

    The delay here is injected between the clock read and SQLite serializing the
    UPDATE. It schedules an interleaving; it changes no predicate and no value.
    """
    parcel_id = _make_parcel(conn)
    a = leases.acquire(conn, parcel_id, "agent-A", mode="write", ttl=0.30)
    assert a.granted is True

    class _SlowConn:
        """Delays A's heartbeat UPDATE so its lease lapses before it serializes."""

        def __init__(self, real):
            self._real = real
            self._fired = False

        def __getattr__(self, name):
            return getattr(self._real, name)

        def execute(self, sql, *args, **kwargs):
            low = " ".join(sql.split()).lower()
            if not self._fired and low.startswith("update leases") and "heartbeat_at" in low:
                self._fired = True
                time.sleep(0.40)  # A's 0.30s TTL lapses inside this window...
                # ...and B lawfully takes the parcel via acquire()'s lazy expiry.
                b = leases.acquire(self._real, parcel_id, "agent-B", mode="write", ttl=5.0)
                assert b.granted is True, "setup: B should win the lapsed parcel"
            return self._real.execute(sql, *args, **kwargs)

    revived = leases.heartbeat(_SlowConn(conn), a.lease_id, "agent-A", ttl=1.0)
    assert revived is False, (
        "a heartbeat bound before the lease lapsed still revived it: the predicate is "
        "reading a stale Python clock instead of SQLite's write-time clock"
    )

    live = conn.execute(
        "SELECT id, agent_id FROM leases "
        "WHERE parcel_id = ? AND status = 'active' AND ttl_expires_at > ?",
        (parcel_id, time.time()),
    ).fetchall()
    assert len(live) == 1, (
        f"DOUBLE LEASE via stale-clock revival: "
        f"{[(r['id'], r['agent_id']) for r in live]} both hold {parcel_id!r}"
    )
    assert live[0]["agent_id"] == "agent-B"
