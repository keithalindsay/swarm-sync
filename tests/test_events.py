"""U6 — Event log + pheromone. DESIGN.md §4.1, §4.3.

Done when:
  - `emit` returns monotonically increasing seq
  - `tail(since=k)` returns only seq>k in order
  - `decay_pheromone` reduces strength and never below 0
"""
from __future__ import annotations

import json
import time

import pytest

from swarmsync.blackboard import db
from swarmsync.server import events, leases


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


# --- done-when: emit() monotonic seq ----------------------------------------------


def test_emit_returns_monotonically_increasing_seq(conn):
    s1 = events.emit(conn, "planned", "agent-1")
    s2 = events.emit(conn, "lease_granted", "agent-1")
    s3 = events.emit(conn, "released", "agent-1")

    assert s1 < s2 < s3
    assert (s1, s2, s3) == (1, 2, 3)


def test_emit_persists_agent_id_type_payload_ts(conn):
    seq = events.emit(conn, "done", "agent-7", payload={"parcel_id": "a.py::foo"}, ts=123.5)
    row = conn.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
    assert row["agent_id"] == "agent-7"
    assert row["type"] == "done"
    assert json.loads(row["payload"]) == {"parcel_id": "a.py::foo"}
    assert row["ts"] == 123.5


def test_emit_agent_id_and_payload_are_optional(conn):
    seq = events.emit(conn, "reindexed")
    row = conn.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
    assert row["agent_id"] is None
    assert row["payload"] is None


def test_emit_rejects_unrecognized_event_type(conn):
    with pytest.raises(ValueError):
        events.emit(conn, "not_a_real_event_type", "agent-1")
    assert conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 0


def test_emit_default_ts_is_now(conn):
    before = time.time()
    seq = events.emit(conn, "planned", "agent-1")
    after = time.time()
    row = conn.execute("SELECT ts FROM events WHERE seq = ?", (seq,)).fetchone()
    assert before <= row["ts"] <= after


# --- S2 regression: concurrent emits get distinct, monotonic seqs -----------------


def test_concurrent_emits_return_distinct_monotonic_seqs(conn):
    """Many threads emitting against the one shared connection must each get back
    their OWN row's seq -- a bijection with the rows actually written.

    Fails on the old `return cur.lastrowid`: `lastrowid` is the per-CONNECTION
    `sqlite3_last_insert_rowid()`, read after the GIL is re-acquired post-step, so
    a sibling thread's INSERT lands in that window and two emits report the same
    seq. `INSERT ... RETURNING seq` reads each statement's own result and is
    immune. (Empirically the old form produces >100 duplicate seqs at this size.)
    """
    import threading

    n_threads, per_thread = 8, 80
    barrier = threading.Barrier(n_threads)
    returned: list[int] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()  # release all threads together to maximize contention
        local = [events.emit(conn, "planned", "agent-x") for _ in range(per_thread)]
        with lock:
            returned.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = n_threads * per_thread
    # Every returned seq is distinct...
    assert len(returned) == total
    assert len(set(returned)) == total, "concurrent emits returned duplicate seqs"
    # ...and they are exactly the seqs actually persisted (a clean bijection).
    assert set(returned) == set(range(1, total + 1))
    persisted = {
        row["seq"] for row in conn.execute("SELECT seq FROM events").fetchall()
    }
    assert set(returned) == persisted


# --- done-when: tail(since=k) returns only seq>k, in order ------------------------


def test_tail_since_filters_and_orders(conn):
    seqs = [events.emit(conn, "planned", f"agent-{i}") for i in range(5)]

    tailed = events.tail(conn, since_seq=seqs[1])

    assert [e.seq for e in tailed] == seqs[2:]
    # strictly ascending
    assert all(a < b for a, b in zip([e.seq for e in tailed], [e.seq for e in tailed][1:]))


def test_tail_default_since_zero_returns_everything(conn):
    seqs = [events.emit(conn, "planned", "agent-1") for _ in range(3)]
    tailed = events.tail(conn)
    assert [e.seq for e in tailed] == seqs


def test_tail_respects_limit(conn):
    for _ in range(10):
        events.emit(conn, "planned", "agent-1")
    tailed = events.tail(conn, since_seq=0, limit=3)
    assert len(tailed) == 3
    assert [e.seq for e in tailed] == [1, 2, 3]


def test_tail_empty_since_high_watermark_returns_empty(conn):
    events.emit(conn, "planned", "agent-1")
    assert events.tail(conn, since_seq=999) == []


def test_tail_returns_event_models_with_expected_fields(conn):
    events.emit(conn, "merged", "agent-1", payload={"branch": "b1"}, ts=42.0)
    [ev] = events.tail(conn)
    assert ev.type == "merged"
    assert ev.agent_id == "agent-1"
    assert ev.ts == 42.0
    assert json.loads(ev.payload) == {"branch": "b1"}


# --- pheromone: drop is an upsert keyed on (parcel_id, agent_id, kind) ------------


def test_drop_pheromone_creates_row(conn):
    parcel_id = _make_parcel(conn)
    ph = events.drop_pheromone(conn, parcel_id, "agent-1", "planned", 1.0)
    assert ph.parcel_id == parcel_id
    assert ph.agent_id == "agent-1"
    assert ph.kind == "planned"
    assert ph.strength == 1.0

    rows = conn.execute("SELECT * FROM pheromone").fetchall()
    assert len(rows) == 1


def test_drop_pheromone_upserts_no_duplicate_rows(conn):
    parcel_id = _make_parcel(conn)
    events.drop_pheromone(conn, parcel_id, "agent-1", "planned", 1.0, ts=1.0)
    ph2 = events.drop_pheromone(conn, parcel_id, "agent-1", "planned", 0.5, ts=2.0)

    rows = conn.execute("SELECT * FROM pheromone").fetchall()
    assert len(rows) == 1
    assert ph2.strength == 0.5
    assert ph2.updated_at == 2.0


def test_drop_pheromone_distinct_kind_or_agent_is_separate_row(conn):
    parcel_id = _make_parcel(conn)
    events.drop_pheromone(conn, parcel_id, "agent-1", "planned", 1.0)
    events.drop_pheromone(conn, parcel_id, "agent-1", "touched", 1.0)
    events.drop_pheromone(conn, parcel_id, "agent-2", "planned", 1.0)

    rows = conn.execute("SELECT * FROM pheromone").fetchall()
    assert len(rows) == 3


# --- done-when: decay_pheromone reduces strength, never below 0 ------------------


def test_decay_pheromone_reduces_strength_by_half_after_one_half_life(conn):
    parcel_id = _make_parcel(conn)
    events.drop_pheromone(conn, parcel_id, "agent-1", "touched", 1.0, ts=0.0)

    touched = events.decay_pheromone(conn, half_life=10.0, ts=10.0)

    assert touched == 1
    row = conn.execute("SELECT * FROM pheromone").fetchone()
    assert row["strength"] == pytest.approx(0.5, abs=1e-9)
    assert row["updated_at"] == 10.0


def test_decay_pheromone_never_below_zero_even_after_long_elapsed(conn):
    parcel_id = _make_parcel(conn)
    events.drop_pheromone(conn, parcel_id, "agent-1", "touched", 1.0, ts=0.0)

    events.decay_pheromone(conn, half_life=1.0, ts=10_000.0)

    row = conn.execute("SELECT * FROM pheromone").fetchone()
    assert row["strength"] >= 0.0
    assert row["strength"] == pytest.approx(0.0, abs=1e-9)


def test_decay_pheromone_no_rows_is_a_noop(conn):
    touched = events.decay_pheromone(conn, half_life=10.0)
    assert touched == 0


def test_decay_pheromone_rejects_nonpositive_half_life(conn):
    parcel_id = _make_parcel(conn)
    events.drop_pheromone(conn, parcel_id, "agent-1", "touched", 1.0)
    with pytest.raises(ValueError):
        events.decay_pheromone(conn, half_life=0)
    with pytest.raises(ValueError):
        events.decay_pheromone(conn, half_life=-5.0)


def test_decay_pheromone_compounds_across_repeated_calls(conn):
    parcel_id = _make_parcel(conn)
    events.drop_pheromone(conn, parcel_id, "agent-1", "touched", 1.0, ts=0.0)

    events.decay_pheromone(conn, half_life=10.0, ts=10.0)  # -> 0.5
    events.decay_pheromone(conn, half_life=10.0, ts=20.0)  # another half-life -> 0.25

    row = conn.execute("SELECT * FROM pheromone").fetchone()
    assert row["strength"] == pytest.approx(0.25, abs=1e-9)


def test_decay_pheromone_clock_skew_guard_does_not_grow_strength(conn):
    parcel_id = _make_parcel(conn)
    events.drop_pheromone(conn, parcel_id, "agent-1", "touched", 0.5, ts=100.0)

    # ts "in the past" relative to updated_at -- must not increase strength.
    events.decay_pheromone(conn, half_life=10.0, ts=50.0)

    row = conn.execute("SELECT * FROM pheromone").fetchone()
    assert row["strength"] == pytest.approx(0.5, abs=1e-9)


# --- regression: leases.py now funnels through events.emit ------------------------


def test_leases_events_are_visible_via_tail(conn):
    parcel_id = _make_parcel(conn)
    leases.acquire(conn, parcel_id, "agent-1", mode="write")

    tailed = events.tail(conn)
    assert [e.type for e in tailed] == ["lease_granted"]
    assert tailed[0].agent_id == "agent-1"
