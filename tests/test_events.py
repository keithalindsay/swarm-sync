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
from swarmsync.blackboard import events, leases


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


# --- WP4.5 (prep C17): tail_newest -- the newest n, still ascending ----------------


def test_tail_newest_returns_newest_n_in_ascending_order(conn):
    for i in range(5):
        events.emit(conn, "planned", f"agent-{i}", ts=float(i))
    everything = events.tail(conn)
    assert len(everything) == 5

    newest = events.tail_newest(conn, 2)
    assert [e.seq for e in newest] == [e.seq for e in everything[-2:]]
    assert [e.seq for e in newest] == sorted(e.seq for e in newest)
    assert [e.agent_id for e in newest] == ["agent-3", "agent-4"]


def test_tail_newest_more_than_available_returns_all(conn):
    events.emit(conn, "planned", "agent-1")
    events.emit(conn, "done", "agent-1")
    newest = events.tail_newest(conn, 100)
    assert [e.seq for e in newest] == [e.seq for e in events.tail(conn)]


def test_tail_newest_empty_log_returns_empty(conn):
    assert events.tail_newest(conn, 10) == []


def test_tail_newest_rejects_nonpositive_n(conn):
    with pytest.raises(ValueError):
        events.tail_newest(conn, 0)
    with pytest.raises(ValueError):
        events.tail_newest(conn, -3)


def test_tail_newest_serves_registry_external_marker_rows(conn):
    """Same decoding as `tail`: an `events_compacted` maintenance row (outside
    the frozen EventType registry) is served, not dropped or 500'd."""
    events.emit(conn, "heartbeat", "a1", ts=time.time() - 8000)
    assert events.compact_events(conn) == 1
    newest = events.tail_newest(conn, 10)
    assert [e.type for e in newest] == [events.EVENTS_COMPACTED]


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


# --- C15 (WP4.5): decay must be one atomic UPDATE, no read-modify-write -----------
# The old shape SELECTed strengths, computed decay in Python, and executemany-
# UPDATEd by PK -- a drop_pheromone landing between the read and the write (the
# reaper decays every 1s on its own connection) was overwritten with a stale
# decayed value. A true in-process race is not deterministically forceable here,
# so the fix gets (a) a SEMANTIC test -- decay derives from the CURRENT committed
# strength at execution time, i.e. a re-drop is never resurrected stale -- and
# (b) a code-SHAPE test asserting the single-UPDATE implementation directly.


def test_decay_pheromone_decays_from_the_current_committed_strength(conn):
    parcel_id = _make_parcel(conn)
    events.drop_pheromone(conn, parcel_id, "agent-1", "touched", 1.0, ts=0.0)
    # A later drop REPLACES the row (strength 0.8 at t=10) -- the C15 stand-in
    # for "a drop landed before the decay executed". The decay at t=20 must
    # start from 0.8/updated_at=10, never from any earlier snapshot of the row.
    events.drop_pheromone(conn, parcel_id, "agent-1", "touched", 0.8, ts=10.0)

    events.decay_pheromone(conn, half_life=10.0, ts=20.0)

    row = conn.execute("SELECT * FROM pheromone").fetchone()
    assert row["strength"] == pytest.approx(0.4, abs=1e-9)  # 0.8 * 0.5 ** (10/10)
    assert row["updated_at"] == 20.0


def test_decay_pheromone_dropped_row_decays_from_its_committed_value(conn):
    # The prompt's semantic anchor: dropped at 1.0, decayed with a now far in
    # the future -> decays from the committed 1.0 (to ~0), never goes negative.
    parcel_id = _make_parcel(conn)
    events.drop_pheromone(conn, parcel_id, "agent-1", "planned", 1.0, ts=0.0)

    touched = events.decay_pheromone(conn, half_life=5.0, ts=1_000_000.0)

    assert touched == 1
    row = conn.execute("SELECT * FROM pheromone").fetchone()
    assert 0.0 <= row["strength"] == pytest.approx(0.0, abs=1e-9)
    assert row["updated_at"] == 1_000_000.0


class _TracingConn:
    """Wraps a real connection, recording every SQL statement text. Duck-typed:
    decay_pheromone only needs `execute`/`executemany`."""

    def __init__(self, inner):
        self._inner = inner
        self.statements: list[str] = []

    def execute(self, sql, *args):
        self.statements.append(sql)
        return self._inner.execute(sql, *args)

    def executemany(self, sql, *args):
        self.statements.append(sql)
        return self._inner.executemany(sql, *args)


def test_decay_pheromone_works_through_the_python_pow_fallback(conn):
    """C15: on SQLite builds without SQLITE_ENABLE_MATH_FUNCTIONS,
    `db._configure` registers `db._pow_fallback` as `pow`. This box HAS the
    built-in, so force the fallback onto the connection (create_function
    overrides the built-in) and prove the decay statement still computes
    identically through it."""
    conn.create_function("pow", 2, db._pow_fallback, deterministic=True)
    parcel_id = _make_parcel(conn)
    events.drop_pheromone(conn, parcel_id, "agent-1", "touched", 1.0, ts=0.0)

    assert events.decay_pheromone(conn, half_life=10.0, ts=10.0) == 1
    row = conn.execute("SELECT * FROM pheromone").fetchone()
    assert row["strength"] == pytest.approx(0.5, abs=1e-9)


def test_decay_pheromone_is_a_single_update_statement(conn):
    """C15 code-shape guard: exactly ONE statement, an UPDATE, and no SELECT of
    strengths anywhere -- reverting to read-modify-write fails here even though
    the arithmetic (and every semantic test above) would still pass."""
    parcel_id = _make_parcel(conn)
    events.drop_pheromone(conn, parcel_id, "agent-1", "touched", 1.0, ts=0.0)
    events.drop_pheromone(conn, parcel_id, "agent-2", "planned", 0.7, ts=5.0)

    tracer = _TracingConn(conn)
    touched = events.decay_pheromone(tracer, half_life=10.0, ts=10.0)

    assert touched == 2
    assert len(tracer.statements) == 1, (
        f"decay must be one atomic statement, ran {len(tracer.statements)}: "
        f"{tracer.statements}"
    )
    only = tracer.statements[0].strip().upper()
    assert only.startswith("UPDATE PHEROMONE")
    assert "SELECT" not in only  # no read-modify-write round trip


# --- regression: leases.py now funnels through events.emit ------------------------


def test_leases_events_are_visible_via_tail(conn):
    parcel_id = _make_parcel(conn)
    leases.acquire(conn, parcel_id, "agent-1", mode="write")

    tailed = events.tail(conn)
    assert [e.type for e in tailed] == ["lease_granted"]
    assert tailed[0].agent_id == "agent-1"


# --- WP3.1 (finding S2): events retention/compaction ------------------------------
#
# Reproduced first: before `compact_events` existed, month-old heartbeat rows
# survived any number of reaper passes (the table only ever grew -- nothing in the
# codebase issued a DELETE against `events`). These tests pin the new contract.


HOUR = 3600.0
DAY = 86400.0


def _emit_at(conn, type_, age, agent_id="agent-1", payload=None):
    """Emit an event `age` seconds in the past; returns its seq."""
    return events.emit(conn, type_, agent_id, payload, ts=time.time() - age)


def _open_integration_for(conn, seq, age):
    """Mirror integrator's projection row for an `integrate_started` seq."""
    conn.execute(
        "INSERT INTO open_integrations "
        "(started_seq, repo, branch, into_branch, trunk_sha_before, ts) "
        "VALUES (?, 'r', 'b', 'integration', 'deadbeef', ?)",
        (seq, time.time() - age),
    )


def test_compact_events_prunes_only_old_heartbeats_in_short_window(conn):
    old_hb = _emit_at(conn, "heartbeat", 2 * HOUR)
    young_hb = _emit_at(conn, "heartbeat", 0.5 * HOUR)
    old_but_audit = _emit_at(conn, "merged", 2 * HOUR)  # audit value: NOT pruned

    pruned = events.compact_events(conn, heartbeat_max_age=HOUR, max_age=7 * DAY)

    assert pruned == 1
    remaining = {r["seq"] for r in conn.execute("SELECT seq FROM events").fetchall()}
    assert old_hb not in remaining
    assert young_hb in remaining
    assert old_but_audit in remaining


def test_compact_events_long_horizon_prunes_any_type(conn):
    ancient_merged = _emit_at(conn, "merged", 8 * DAY)
    ancient_orphan = _emit_at(conn, "integrate_orphaned", 8 * DAY)
    week_young_merged = _emit_at(conn, "merged", 6 * DAY)

    pruned = events.compact_events(conn, heartbeat_max_age=HOUR, max_age=7 * DAY)

    assert pruned == 2
    remaining = {r["seq"] for r in conn.execute("SELECT seq FROM events").fetchall()}
    assert ancient_merged not in remaining
    assert ancient_orphan not in remaining
    assert week_young_merged in remaining  # younger than the horizon: audit history


def test_compact_events_never_deletes_open_integration_start(conn):
    """The recovery-relevant start survives unconditionally, however old.

    (Recovery itself reads the `open_integrations` projection since WP3.2, so
    compaction can't break it either way -- this guard additionally keeps the
    audit row behind a still-open integrate.) Mutation target: dropping the
    `seq NOT IN (SELECT started_seq FROM open_integrations)` guard fails this.
    """
    started = _emit_at(conn, "integrate_started", 30 * DAY)
    _open_integration_for(conn, started, 30 * DAY)
    doomed = _emit_at(conn, "heartbeat", 30 * DAY)

    pruned = events.compact_events(conn, heartbeat_max_age=HOUR, max_age=7 * DAY)

    assert pruned == 1  # the heartbeat only
    remaining = {r["seq"] for r in conn.execute("SELECT seq FROM events").fetchall()}
    assert started in remaining
    assert doomed not in remaining


def test_compact_events_emits_one_marker_with_count_and_seq_range(conn):
    s1 = _emit_at(conn, "heartbeat", 3 * HOUR)
    s2 = _emit_at(conn, "heartbeat", 2 * HOUR)
    keep = _emit_at(conn, "heartbeat", 0.1 * HOUR)

    pruned = events.compact_events(conn, heartbeat_max_age=HOUR, max_age=7 * DAY)
    assert pruned == 2

    markers = conn.execute(
        "SELECT * FROM events WHERE type = ?", (events.EVENTS_COMPACTED,)
    ).fetchall()
    assert len(markers) == 1
    payload = json.loads(markers[0]["payload"])
    assert payload["pruned"] == 2
    assert payload["seq_min"] == min(s1, s2)
    assert payload["seq_max"] == max(s1, s2)
    assert keep > 0  # (still present; range covers only the pruned rows)


def test_compact_events_noop_pass_emits_nothing(conn):
    """The compactor must not become its own growth source: a pass that prunes
    nothing (including one right after a successful pass) inserts no marker."""
    _emit_at(conn, "heartbeat", 2 * HOUR)
    assert events.compact_events(conn, heartbeat_max_age=HOUR, max_age=7 * DAY) == 1
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    for _ in range(3):
        assert events.compact_events(conn, heartbeat_max_age=HOUR, max_age=7 * DAY) == 0

    after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert after == before  # repeated no-op passes added zero rows


def test_compact_events_windows_read_from_env(conn, monkeypatch):
    monkeypatch.setenv(events.HEARTBEAT_MAX_AGE_ENV, str(10 * 60.0))  # 10 minutes
    monkeypatch.setenv(events.EVENT_MAX_AGE_ENV, str(DAY))

    hb = _emit_at(conn, "heartbeat", 30 * 60.0)  # 30 min: past the 10-min env window
    old_planned = _emit_at(conn, "planned", 2 * DAY)  # past the 1-day env horizon

    assert events.compact_events(conn) == 2
    remaining = {r["seq"] for r in conn.execute("SELECT seq FROM events").fetchall()}
    assert hb not in remaining
    assert old_planned not in remaining


def test_compact_events_garbage_env_falls_back_to_defaults(conn, monkeypatch):
    monkeypatch.setenv(events.HEARTBEAT_MAX_AGE_ENV, "not-a-number")
    monkeypatch.setenv(events.EVENT_MAX_AGE_ENV, "-5")

    young_hb = _emit_at(conn, "heartbeat", 0.5 * HOUR)  # under the 1h default
    assert events.compact_events(conn) == 0
    assert young_hb in {r["seq"] for r in conn.execute("SELECT seq FROM events").fetchall()}


def test_tail_returns_compaction_marker_rows(conn):
    """`events_compacted` is outside the frozen EventType registry (models.py is
    owned by a parallel WP); `tail` must still surface it, not crash or drop it."""
    _emit_at(conn, "heartbeat", 2 * HOUR)
    events.compact_events(conn, heartbeat_max_age=HOUR, max_age=7 * DAY)

    tailed = events.tail(conn)
    assert [e.type for e in tailed] == [events.EVENTS_COMPACTED]
    assert json.loads(tailed[0].payload)["pruned"] == 1
