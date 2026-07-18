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


def test_ensure_parcel_row_matches_what_the_indexer_writes_for_the_same_id(conn, tmp_path):
    """An auto-created parcel must be the row the indexer would write for that id.

    R4 caught two mistakes here. `_ensure_parcel` first wrote `kind='file'`, which is
    outside `ParcelKind`, so `broker.load_scheduling_graph` (which validates EVERY
    parcel row) raised ValidationError -- one hook lease bricked all dispatch. The
    repair then wrote `symbol='<module>'` while the indexer leaves it NULL
    (`schema.sql`: "NULL for interstitial (module-glue) parcels"), and the test written
    to prove the repair asserted that wrong value -- pinning the bug.

    So this asserts against the INDEXER's real output rather than against what I
    happened to write.
    """
    from swarmsync.blackboard.models import Parcel
    from swarmsync.classifier.indexer import MODULE_SYMBOL, parse_file

    src = tmp_path / "package_like.py"
    src.write_text("import os\n\n\ndef g(x):\n    return x\n", encoding="utf-8")
    indexed = {p.id: p for p in parse_file(src, rel_path="package_like.py")}
    reference = indexed[f"package_like.py::{MODULE_SYMBOL}"]

    r = leases.acquire(
        conn, f"other.py::{MODULE_SYMBOL}", "agent-1", mode="write", ensure_parcel=True
    )
    assert r.granted is True
    row = conn.execute(
        "SELECT * FROM parcels WHERE id = ?", (f"other.py::{MODULE_SYMBOL}",)
    ).fetchone()
    parcel = Parcel.model_validate(dict(row))  # must survive the model every reader uses

    assert parcel.kind == reference.kind == "module"
    assert parcel.symbol == reference.symbol, (
        f"auto-created symbol {parcel.symbol!r} != the indexer's {reference.symbol!r} "
        f"for the same interstitial id"
    )
    assert parcel.path == "other.py"


def test_hook_leasing_an_unindexed_file_does_not_brick_the_broker(conn, tmp_path):
    """The consequence, end to end: the broker must still schedule after a hook lease.

    `load_scheduling_graph` validates every parcel row AND re-parses every file from
    disk, so a single bad or missing row is not a local defect -- it is total loss of
    dispatch, for every task, on every file.

    Parametrised over BOTH shapes deliberately. The first version of this test used
    only `package.json` -- the one case that passed -- and gave false confidence while
    a new `.py` file still bricked the broker with `KeyError` one frame lower. A new
    `.py` file with a top-level def is the case the hook hits constantly.
    """
    from swarmsync.classifier.store import run_index
    from swarmsync.coordinator import broker

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    run_index(conn, repo)
    assert broker.load_scheduling_graph(conn, repo) is not None

    # (a) a non-.py file -- no parser involvement
    leases.acquire(conn, "package.json::<module>", "agent-1", mode="write", ensure_parcel=True)
    assert broker.load_scheduling_graph(conn, repo) is not None, "non-.py lease bricked dispatch"

    # (b) a brand-new .py file with a top-level def, created since the last index.
    #     Its <module> parcel exists (we just minted it); its `new_feature` parcel does
    #     NOT. build_graph parses the file off disk and must tolerate that.
    (repo / "feature.py").write_text(
        "def new_feature(x):\n    return x * 2\n", encoding="utf-8"
    )
    leases.acquire(conn, "feature.py::<module>", "agent-2", mode="write", ensure_parcel=True)
    graph, _frozen = broker.load_scheduling_graph(conn, repo)
    assert graph is not None, "a new .py file with a top-level def bricked ALL broker dispatch"

    # (c) same for a top-level class
    (repo / "thing.py").write_text("class Thing:\n    pass\n", encoding="utf-8")
    leases.acquire(conn, "thing.py::<module>", "agent-3", mode="write", ensure_parcel=True)
    assert broker.load_scheduling_graph(conn, repo) is not None, "a new class bricked dispatch"


# --- C8 (P2): same-agent same-mode reacquire is idempotent, never a self-deny ------


def test_same_agent_write_reacquire_is_granted_not_self_denied(conn):
    """Claude Code batches parallel Edit calls from ONE agent; two prechecks both
    see no lease and both POST /lease. The second acquire from the SAME agent in the
    SAME mode must be GRANTED (the agent already holds the parcel), never denied --
    otherwise the agent is blocked from a file it just locked, with itself named as
    the blocker. Pre-fix (CAS matches ANY active write lease with no same-agent
    exemption) the second acquire is DENIED and this fails."""
    parcel_id = _make_parcel(conn)

    r1 = leases.acquire(conn, parcel_id, "agent-A", mode="write")
    r2 = leases.acquire(conn, parcel_id, "agent-A", mode="write")

    assert r1.granted is True
    assert r2.granted is True, "same-agent same-mode reacquire must not self-deny"
    assert r2.lease_id is not None


def test_same_agent_reacquire_still_blocks_a_different_agent(conn):
    """The same-agent exemption must NOT weaken real mutual exclusion: while agent-A
    holds the write lease (even after reacquiring it), a DIFFERENT agent must still be
    denied, and the two different agents must never both hold a live write lease."""
    parcel_id = _make_parcel(conn)

    assert leases.acquire(conn, parcel_id, "agent-A", mode="write").granted is True
    assert leases.acquire(conn, parcel_id, "agent-A", mode="write").granted is True

    rb = leases.acquire(conn, parcel_id, "agent-B", mode="write")
    assert rb.granted is False, "a different agent must still be denied"

    live = conn.execute(
        "SELECT DISTINCT agent_id FROM leases "
        "WHERE parcel_id = ? AND status = 'active' AND ttl_expires_at > ? "
        "AND mode IN ('write', 'exclusive')",
        (parcel_id, time.time()),
    ).fetchall()
    assert [r["agent_id"] for r in live] == ["agent-A"], (
        "exactly one agent may hold a live write lease"
    )


def test_same_agent_different_mode_reacquire_still_conflicts(conn):
    """The idempotency is scoped to the SAME (parcel, agent, mode). A same-agent
    request in a CONFLICTING mode (read while it holds write) is still denied, so the
    exemption cannot be abused to smuggle in an incompatible lease."""
    parcel_id = _make_parcel(conn)
    assert leases.acquire(conn, parcel_id, "agent-A", mode="write").granted is True
    assert leases.acquire(conn, parcel_id, "agent-A", mode="read").granted is False


def test_barrier_gated_same_agent_batched_prechecks_all_grant(conn):
    """N threads race a write-acquire on the SAME parcel as the SAME agent behind a
    barrier -- the batched-parallel-Edit pattern the lock must support. Every one must
    be granted (no self-deny), and no OTHER agent may sneak a live write lease in."""
    import threading

    parcel_id = _make_parcel(conn)
    n_threads = 16
    barrier = threading.Barrier(n_threads)
    results: list[object] = []
    lock = threading.Lock()

    def worker(_i: int) -> None:
        barrier.wait()
        r = leases.acquire(conn, parcel_id, "agent-solo", mode="write")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r.granted for r in results), "same-agent batched acquires must all grant"

    holders = conn.execute(
        "SELECT DISTINCT agent_id FROM leases "
        "WHERE parcel_id = ? AND status = 'active' AND ttl_expires_at > ?",
        (parcel_id, time.time()),
    ).fetchall()
    assert [h["agent_id"] for h in holders] == ["agent-solo"]


def test_build_graph_tolerates_a_symbol_on_disk_with_no_parcel_row(tmp_path):
    """The class-level fix, stated directly: the parcel map is only ever as fresh as
    the last POST /index (there is no incremental indexing), so a symbol on disk with
    no parcel row is ORDINARY. `build_graph` must skip it, not raise.

    R3 logged this KeyError as a P2 reachable via a stale index; the hook's
    auto-created coarse parcels made it persistent and turned it into a P0.
    """
    from swarmsync.classifier.graph import build_graph
    from swarmsync.classifier.indexer import parse_file

    src = tmp_path / "m.py"
    src.write_text("def known(x):\n    return x\n", encoding="utf-8")
    parcels = list(parse_file(src, rel_path="m.py"))

    # The file grows a symbol the blackboard has never seen (an agent's edit).
    src.write_text(
        "def known(x):\n    return x\n\n\ndef added_since_index(y):\n    return y\n"
        "\n\nclass AlsoAdded:\n    pass\n",
        encoding="utf-8",
    )

    graph = build_graph(parcels, tmp_path)  # must not raise KeyError
    assert "m.py::known" in graph.signatures
    assert "m.py::added_since_index" not in graph.signatures  # no parcel -> no signature


# --- WP2.S: a denied acquire surfaces WHO holds the parcel and WHEN it expires ----
# U3/A6: pre-fix, LeaseResult.reason was a bare string and the holder's identity/ttl
# were never returned, forcing the hook adapter into a second /leases round-trip.


def test_denied_write_names_the_conflicting_holder_and_ttl(conn):
    """A DIFFERENT agent's denied write must name the current write holder and carry
    that lease's ttl_expires_at as STRUCTURED fields (not just prose), so the caller
    learns who blocks and when it frees without a second round-trip. Mutating out the
    holder-population line drops these back to None and this fails."""
    parcel_id = _make_parcel(conn)

    r1 = leases.acquire(conn, parcel_id, "agent-holder", mode="write", ttl=30.0)
    assert r1.granted
    held = conn.execute(
        "SELECT ttl_expires_at FROM leases WHERE id = ?", (r1.lease_id,)
    ).fetchone()

    r2 = leases.acquire(conn, parcel_id, "agent-other", mode="write")
    assert r2.granted is False
    assert r2.holder == "agent-holder"
    assert r2.holder_ttl_expires_at == pytest.approx(held["ttl_expires_at"])
    assert "agent-holder" in r2.reason  # human string enriched too


def test_denied_read_against_write_holder_names_that_holder(conn):
    """An incoming READ denied by a write holder still identifies that holder."""
    parcel_id = _make_parcel(conn)
    leases.acquire(conn, parcel_id, "agent-w", mode="write")
    r = leases.acquire(conn, parcel_id, "agent-r", mode="read")
    assert r.granted is False
    assert r.holder == "agent-w"
    assert r.holder_ttl_expires_at is not None


def test_granted_acquire_leaves_holder_fields_none(conn):
    """The granted path must be unchanged: no holder, no holder ttl."""
    parcel_id = _make_parcel(conn)
    r = leases.acquire(conn, parcel_id, "agent-1", mode="write")
    assert r.granted is True
    assert r.holder is None
    assert r.holder_ttl_expires_at is None


def test_same_agent_idempotent_reacquire_is_granted_with_no_holder(conn):
    """Phase-1 WP1.5: a same-(parcel, agent, mode) re-acquire is GRANTED, so it is
    NEVER a deny and must not populate holder fields (it would otherwise name the
    agent as blocking itself)."""
    parcel_id = _make_parcel(conn)
    assert leases.acquire(conn, parcel_id, "agent-A", mode="write").granted is True
    r2 = leases.acquire(conn, parcel_id, "agent-A", mode="write")
    assert r2.granted is True
    assert r2.holder is None
    assert r2.holder_ttl_expires_at is None


def test_denied_write_prefers_write_holder_over_blocking_readers(conn):
    """Tie-break: multiple readers hold the parcel AND a write/exclusive holder also
    does; the denied incoming write must name the WRITE/exclusive holder (the exclusive
    owner whose release frees the parcel), not one of the readers."""
    parcel_id = _make_parcel(conn)
    # A live write holder and a live reader cannot BOTH be produced via acquire() on one
    # parcel (the write would be denied), so seed the mixed set directly to exercise the
    # ORDER BY tie-break: one active reader + one active write holder on the same parcel.
    now = time.time()
    conn.execute(
        "INSERT INTO leases (parcel_id, agent_id, mode, acquired_at, ttl_expires_at, "
        "heartbeat_at, status) VALUES (?, ?, 'read', ?, ?, ?, 'active')",
        (parcel_id, "agent-reader", now, now + 5.0, now),
    )
    conn.execute(
        "INSERT INTO leases (parcel_id, agent_id, mode, acquired_at, ttl_expires_at, "
        "heartbeat_at, status) VALUES (?, ?, 'write', ?, ?, ?, 'active')",
        (parcel_id, "agent-writer", now, now + 60.0, now),
    )
    r = leases.acquire(conn, parcel_id, "agent-new", mode="write")
    assert r.granted is False
    assert r.holder == "agent-writer"  # write holder wins the tie-break over the reader


def test_denied_write_among_only_readers_picks_soonest_to_expire(conn):
    """Tie-break fallback: when only readers block an incoming write, name the reader
    that frees the parcel SOONEST (smallest ttl_expires_at), i.e. the earliest retry."""
    parcel_id = _make_parcel(conn)
    now = time.time()
    conn.execute(
        "INSERT INTO leases (parcel_id, agent_id, mode, acquired_at, ttl_expires_at, "
        "heartbeat_at, status) VALUES (?, ?, 'read', ?, ?, ?, 'active')",
        (parcel_id, "agent-late", now, now + 100.0, now),
    )
    conn.execute(
        "INSERT INTO leases (parcel_id, agent_id, mode, acquired_at, ttl_expires_at, "
        "heartbeat_at, status) VALUES (?, ?, 'read', ?, ?, ?, 'active')",
        (parcel_id, "agent-soon", now, now + 5.0, now),
    )
    r = leases.acquire(conn, parcel_id, "agent-new", mode="write")
    assert r.granted is False
    assert r.holder == "agent-soon"
    assert r.holder_ttl_expires_at == pytest.approx(now + 5.0)
