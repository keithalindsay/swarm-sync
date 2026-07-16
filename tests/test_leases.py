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
