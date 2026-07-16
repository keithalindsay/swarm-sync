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
