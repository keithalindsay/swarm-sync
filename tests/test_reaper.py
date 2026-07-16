"""U11 — Reaper + pheromone decay loop. DESIGN.md §6.

Done when (BUILD_PLAN.md): a lease with `ttl_expires_at` in the past is marked
`reaped`, emits a `reaped` event, and its parcel becomes acquirable by a new
agent; decay runs without error.
"""
from __future__ import annotations

import time

import pytest

from swarmsync.blackboard import db
from swarmsync.coordinator import reaper
from swarmsync.server import events, leases
from swarmsync.server.app import create_app


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


# --- done-when: expired lease -> reaped + event + parcel re-acquirable ------------


def test_reap_once_marks_expired_lease_reaped(conn):
    parcel_id = _make_parcel(conn)
    # ttl=-10 -> ttl_expires_at is already 10s in the past the instant it's
    # granted, simulating an agent that crashed long enough ago that its
    # heartbeats stopped and the TTL lapsed.
    result = leases.acquire(conn, parcel_id, "agent-dead", mode="write", ttl=-10.0)
    assert result.granted is True

    reaped_ids = reaper.reap_once(conn, now=time.time())

    assert reaped_ids == [result.lease_id]
    row = conn.execute(
        "SELECT status FROM leases WHERE id = ?", (result.lease_id,)
    ).fetchone()
    assert row["status"] == "reaped"


def test_reap_once_emits_reaped_event_with_expected_payload(conn):
    import json

    parcel_id = _make_parcel(conn)
    result = leases.acquire(conn, parcel_id, "agent-dead", mode="write", ttl=-5.0)

    reaper.reap_once(conn, now=time.time())

    tail = events.tail(conn, since_seq=0)
    reaped_events = [e for e in tail if e.type == "reaped"]
    assert len(reaped_events) == 1
    ev = reaped_events[0]
    assert ev.agent_id == "agent-dead"
    payload = json.loads(ev.payload)
    assert payload["lease_id"] == result.lease_id
    assert payload["parcel_id"] == parcel_id
    assert payload["agent_id"] == "agent-dead"


def test_reap_once_frees_parcel_for_a_new_agent(conn):
    parcel_id = _make_parcel(conn)
    leases.acquire(conn, parcel_id, "agent-dead", mode="write", ttl=-1.0)

    reaper.reap_once(conn, now=time.time())

    fresh = leases.acquire(conn, parcel_id, "agent-fresh", mode="write")
    assert fresh.granted is True


# --- reap_once should NOT touch active/live leases --------------------------------


def test_reap_once_leaves_unexpired_lease_active(conn):
    parcel_id = _make_parcel(conn)
    result = leases.acquire(conn, parcel_id, "agent-alive", mode="write", ttl=300.0)

    reaped_ids = reaper.reap_once(conn, now=time.time())

    assert reaped_ids == []
    row = conn.execute(
        "SELECT status FROM leases WHERE id = ?", (result.lease_id,)
    ).fetchone()
    assert row["status"] == "active"


def test_reap_once_ignores_already_released_lease(conn):
    parcel_id = _make_parcel(conn)
    result = leases.acquire(conn, parcel_id, "agent-a", mode="write", ttl=-1.0)
    assert leases.release(conn, result.lease_id, "agent-a") is True

    reaped_ids = reaper.reap_once(conn, now=time.time())

    assert reaped_ids == []
    row = conn.execute(
        "SELECT status FROM leases WHERE id = ?", (result.lease_id,)
    ).fetchone()
    assert row["status"] == "released"


def test_reap_once_is_idempotent(conn):
    parcel_id = _make_parcel(conn)
    result = leases.acquire(conn, parcel_id, "agent-dead", mode="write", ttl=-1.0)

    first = reaper.reap_once(conn, now=time.time())
    second = reaper.reap_once(conn, now=time.time())

    assert first == [result.lease_id]
    assert second == []  # already reaped, not re-emitted / re-selected

    reaped_events = [e for e in events.tail(conn, since_seq=0) if e.type == "reaped"]
    assert len(reaped_events) == 1


def test_reap_once_returns_empty_list_on_empty_leases_table(conn):
    assert reaper.reap_once(conn, now=time.time()) == []


def test_reap_once_reaps_multiple_expired_leases_in_id_order(conn):
    p1 = _make_parcel(conn, "a.py::foo")
    p2 = _make_parcel(conn, "b.py::bar")
    r1 = leases.acquire(conn, p1, "agent-1", mode="write", ttl=-5.0)
    r2 = leases.acquire(conn, p2, "agent-2", mode="write", ttl=-5.0)

    reaped_ids = reaper.reap_once(conn, now=time.time())

    assert reaped_ids == sorted([r1.lease_id, r2.lease_id])


def test_reap_once_default_now_is_real_time(conn):
    parcel_id = _make_parcel(conn)
    leases.acquire(conn, parcel_id, "agent-dead", mode="write", ttl=-1.0)

    # no `now` passed -> should default to time.time() and still catch it
    reaped_ids = reaper.reap_once(conn)
    assert len(reaped_ids) == 1


# --- S2 regression: reaper vs a heartbeat that renews ttl (TOCTOU) ----------------


class _RaceConn:
    """Connection proxy that fires `on_reap_read` in the reaper's critical window.

    Models a heartbeat that renews a lease's ttl concurrently with `reap_once`,
    landing at exactly the moment that distinguishes the atomic fix from the old
    two-step (SELECT-then-UPDATE) form:

    - NEW (one atomic `UPDATE ... WHERE ttl_expires_at<=now RETURNING`): the
      renewal is applied just BEFORE that statement runs, so its own WHERE now
      sees a future ttl and skips the row.
    - OLD (SELECT expired ids, then per-row `UPDATE ... WHERE status='active'`):
      the renewal lands AFTER the SELECT snapshot but BEFORE the UPDATE, which
      re-checked only `status` and reaped the freshly-renewed lease anyway.

    Either way the invariant under test is the same: a lease renewed in that
    window must be left `active`, never reaped.
    """

    def __init__(self, real, on_reap_read):
        self._real = real
        self._on_reap_read = on_reap_read
        self._fired = False

    def __getattr__(self, name):
        return getattr(self._real, name)

    def execute(self, sql, *args, **kwargs):
        low = " ".join(sql.split()).lower()
        is_atomic_reap = (
            "update leases" in low and "reaped" in low and "ttl_expires_at" in low
        )
        is_reap_select = (
            low.startswith("select") and "from leases" in low and "ttl_expires_at" in low
        )
        if not self._fired and is_atomic_reap:
            # NEW code path: renew immediately BEFORE the atomic reap statement.
            self._fired = True
            self._on_reap_read()
            return self._real.execute(sql, *args, **kwargs)
        if not self._fired and is_reap_select:
            # OLD code path: let the candidate read happen, THEN renew, before
            # the per-row UPDATE that follows.
            cur = self._real.execute(sql, *args, **kwargs)
            self._fired = True
            self._on_reap_read()
            return cur
        return self._real.execute(sql, *args, **kwargs)


def test_reap_once_excludes_lease_renewed_by_heartbeat_in_the_race_window(conn):
    parcel_id = _make_parcel(conn)
    # A live agent whose lease is still valid in real time...
    r = leases.acquire(conn, parcel_id, "agent-live", mode="write", ttl=5.0)
    assert r.granted is True

    def renew():
        # ...renews far into the future inside the reaper's race window.
        assert leases.heartbeat(conn, r.lease_id, "agent-live", ttl=10_000.0) is True

    race = _RaceConn(conn, renew)
    # Candidacy is driven by the reaper's `now`, not by expiring the lease in real
    # time. The previous version of this test set ttl=-1.0 and asserted that
    # heartbeat() revived an ALREADY-EXPIRED lease -- which is the double-lease bug
    # (`leases.heartbeat` had no `ttl_expires_at > now` predicate) written down as a
    # requirement. A heartbeat may only renew a lease its owner still holds, so the
    # scenario is now built the way it actually occurs: a lease that is live when the
    # heartbeat lands, and a candidate from the reaper's point of view.
    reaped = reaper.reap_once(race, now=time.time() + 10.0)

    # The renewed lease must NOT be reaped (fails on the old SELECT-then-UPDATE).
    assert r.lease_id not in reaped
    row = conn.execute(
        "SELECT status FROM leases WHERE id = ?", (r.lease_id,)
    ).fetchone()
    assert row["status"] == "active"
    reaped_events = [e for e in events.tail(conn, since_seq=0) if e.type == "reaped"]
    assert reaped_events == []


# --- decay_once --------------------------------------------------------------------


def test_decay_once_reduces_strength(conn):
    parcel_id = _make_parcel(conn)
    t0 = time.time()
    events.drop_pheromone(conn, parcel_id, "agent-1", "planned", 1.0, ts=t0)

    touched = reaper.decay_once(conn, half_life=10.0, ts=t0 + 10.0)

    assert touched == 1
    row = conn.execute(
        "SELECT strength FROM pheromone WHERE parcel_id = ? AND agent_id = ? "
        "AND kind = ?",
        (parcel_id, "agent-1", "planned"),
    ).fetchone()
    assert row["strength"] == pytest.approx(0.5, rel=1e-6)


def test_decay_once_never_goes_below_zero(conn):
    parcel_id = _make_parcel(conn)
    t0 = time.time()
    events.drop_pheromone(conn, parcel_id, "agent-1", "planned", 1.0, ts=t0)

    reaper.decay_once(conn, half_life=1.0, ts=t0 + 10_000.0)

    row = conn.execute(
        "SELECT strength FROM pheromone WHERE parcel_id = ? AND agent_id = ? "
        "AND kind = ?",
        (parcel_id, "agent-1", "planned"),
    ).fetchone()
    assert row["strength"] >= 0.0


def test_decay_once_runs_without_error_on_empty_pheromone_table(conn):
    assert reaper.decay_once(conn, half_life=60.0) == 0


def test_decay_once_propagates_value_error_on_bad_half_life(conn):
    with pytest.raises(ValueError):
        reaper.decay_once(conn, half_life=0.0)


# --- run() background loop ----------------------------------------------------------


@pytest.mark.asyncio
async def test_run_reaps_and_decays_over_bounded_iterations(conn):
    parcel_id = _make_parcel(conn)
    leases.acquire(conn, parcel_id, "agent-dead", mode="write", ttl=-1.0)
    events.drop_pheromone(conn, parcel_id, "agent-1", "planned", 1.0)

    await reaper.run(conn, interval=0.0, half_life=60.0, iterations=2)

    lease_row = conn.execute(
        "SELECT status FROM leases WHERE agent_id = 'agent-dead'"
    ).fetchone()
    assert lease_row["status"] == "reaped"

    reaped_events = [e for e in events.tail(conn, since_seq=0) if e.type == "reaped"]
    assert len(reaped_events) == 1  # not double-reaped across the two iterations

    pher_row = conn.execute(
        "SELECT strength FROM pheromone WHERE parcel_id = ? AND agent_id = 'agent-1'",
        (parcel_id,),
    ).fetchone()
    assert pher_row["strength"] < 1.0  # decay actually ran


@pytest.mark.asyncio
async def test_run_zero_iterations_is_a_noop(conn):
    # iterations=0 -> the loop body never executes, returns immediately.
    await reaper.run(conn, interval=0.0, iterations=0)
    assert conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_run_can_be_cancelled_cleanly(conn):
    import asyncio

    task = asyncio.create_task(reaper.run(conn, interval=60.0))
    await asyncio.sleep(0)  # let it start and do its first immediate pass
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- wired into server/app.py's lifespan (DESIGN §4.2, §6) -------------------------


def test_reaper_is_wired_into_app_lifespan_and_reaps_expired_leases(tmp_path):
    """End-to-end through the real FastAPI app (not just the bare function):
    create_app's background reaper loop, running at a fast interval, must
    reap an expired lease on its own without any test code calling
    `reaper.reap_once` directly.
    """
    from fastapi.testclient import TestClient

    app = create_app(tmp_path / "blackboard.db", reaper_interval=0.05)
    with TestClient(app):
        conn = app.state.conn
        parcel_id = _make_parcel(conn)
        r = leases.acquire(conn, parcel_id, "agent-dead", mode="write", ttl=-1.0)
        assert r.granted is True

        # Poll on the `reaped` EVENT, not the intermediate lease `status`: the
        # reaper marks the row `reaped` and THEN emits the event (two steps on
        # its own connection), so waiting on `status=='reaped'` via this separate
        # inspection connection can observe the status before the event lands --
        # a test race. The event is the end-state we actually assert on.
        deadline = time.time() + 2.0
        reaped_events: list = []
        while time.time() < deadline:
            reaped_events = [
                e for e in events.tail(conn, since_seq=0) if e.type == "reaped"
            ]
            if reaped_events:
                break
            time.sleep(0.05)
        else:
            pytest.fail("background reaper never reaped the expired lease in time")

        assert conn.execute(
            "SELECT status FROM leases WHERE id = ?", (r.lease_id,)
        ).fetchone()["status"] == "reaped"
        assert len(reaped_events) == 1


def test_reaper_interval_none_disables_background_loop(tmp_path):
    from fastapi.testclient import TestClient

    app = create_app(tmp_path / "blackboard.db", reaper_interval=None)
    with TestClient(app):
        conn = app.state.conn
        parcel_id = _make_parcel(conn)
        r = leases.acquire(conn, parcel_id, "agent-dead", mode="write", ttl=-1.0)
        time.sleep(0.2)
        row = conn.execute(
            "SELECT status FROM leases WHERE id = ?", (r.lease_id,)
        ).fetchone()
        # nothing is running the loop -> stays active (merely expired-but-unmarked)
        assert row["status"] == "active"
