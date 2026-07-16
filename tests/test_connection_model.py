"""S4 connection model -- per-request connections give WAL its concurrency back.

Before S4 every request handler shared one process-wide `app.state.conn`. That
(a) serialized all DB work on a single SQLite handle, throwing away WAL's
concurrent readers, and (b) folded any single-statement writer into whatever
explicit transaction another handler had open on that shared connection -- SQLite
has exactly ONE transaction per connection -- so a `store.run_index` rollback could
silently swallow an unrelated committed write.

The fix: each request opens its OWN connection (`server.app.get_conn` ->
`db.connect`) and closes it on the way out; `store` runs its batch upserts through
`db.transaction` (BEGIN IMMEDIATE, no nesting) on that private connection.

These tests pin both halves and are written to FAIL on the pre-S4 shared-connection
app:

  * `test_concurrent_requests_use_distinct_connections` -- two requests that are
    provably in flight at the same time must receive DIFFERENT connection objects
    (old: both got the one `app.state.conn` -> identical id -> fails).
  * `test_reader_does_not_see_a_writers_uncommitted_row` -- the requested
    readers-while-writing test: while one request holds an OPEN write transaction
    with an uncommitted parcel, a concurrent reader request must NOT see that row
    (WAL snapshot isolation across separate connections). Old: the reader shared
    the writer's connection and so read *inside* its open transaction, seeing the
    uncommitted row -> fails.
  * `test_transaction_*` -- `db.transaction`'s isolation + no-nesting contract.
"""
from __future__ import annotations

import concurrent.futures
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from swarmsync.blackboard import db
from swarmsync.blackboard.models import LeaseResult
from swarmsync.server import app as app_mod
from swarmsync.server import leases as leases_mod
from swarmsync.server.app import create_app


def test_concurrent_requests_use_distinct_connections(tmp_path, monkeypatch):
    """Two simultaneously-in-flight requests must get separate connections.

    A `threading.Barrier(2)` inside the two monkeypatched handler bodies proves
    both requests are genuinely executing at the same instant; each records the
    `id()` of the connection its handler received. On the pre-S4 app both handlers
    read the single shared `app.state.conn`, so the two ids are equal and the final
    assert fails; with per-request connections they differ.
    """
    app = create_app(tmp_path / "blackboard.db", reaper_interval=None)

    seen: dict[str, int] = {}
    rendezvous = threading.Barrier(2, timeout=10)

    def fake_acquire(conn, *args, **kwargs):
        seen["lease"] = id(conn)
        rendezvous.wait()  # hold the request open until the sibling is here too
        return LeaseResult(granted=True, lease_id=1)

    def fake_heartbeat(conn, *args, **kwargs):
        seen["heartbeat"] = id(conn)
        rendezvous.wait()
        return True

    monkeypatch.setattr(leases_mod, "acquire", fake_acquire)
    monkeypatch.setattr(leases_mod, "heartbeat", fake_heartbeat)

    with TestClient(app) as client:

        def do_lease():
            return client.post(
                "/lease",
                json={"agent_id": "a", "parcel_id": "p::x", "mode": "write"},
            )

        def do_heartbeat():
            return client.post(
                "/heartbeat", json={"agent_id": "a", "lease_id": 1}
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_lease = ex.submit(do_lease)
            f_hb = ex.submit(do_heartbeat)
            r_lease = f_lease.result(timeout=15)
            r_hb = f_hb.result(timeout=15)

    assert r_lease.status_code == 200, r_lease.text
    assert r_hb.status_code == 200, r_hb.text
    assert "lease" in seen and "heartbeat" in seen
    # The load-bearing assert: two concurrently-in-flight handlers held two
    # DIFFERENT connections. Equal ids == the pre-S4 shared-connection defect.
    assert seen["lease"] != seen["heartbeat"], (
        "both handlers shared one connection -- WAL concurrency is gone and a "
        "store rollback could swallow a sibling writer"
    )


def test_reader_does_not_see_a_writers_uncommitted_row(tmp_path, monkeypatch):
    """Concurrent readers-while-writing: a reader must not observe another
    request's uncommitted write (WAL snapshot isolation across connections).

    The writer request enters a stubbed `run_index` that opens a real write
    transaction on ITS connection, inserts a sentinel parcel, and parks -- holding
    the transaction open (uncommitted) -- while a concurrent reader request hits
    `GET /parcels`. On the per-S4 app the reader is a separate connection and sees
    the last committed snapshot, WITHOUT the sentinel. On the pre-S4 shared
    connection the reader read *inside* the writer's open transaction and would see
    the uncommitted sentinel; the writer then rolls back, so that row must never
    have been visible -- this assert catches it.
    """
    app = create_app(tmp_path / "blackboard.db", reaper_interval=None)

    sentinel_id = "sentinel.py::ghost"
    writer_holding = threading.Event()
    reader_done = threading.Event()

    def blocking_run_index(conn, root, threshold=None):
        # A real, OPEN write transaction on this request's own connection, holding
        # an uncommitted sentinel parcel. We manage BEGIN/ROLLBACK by hand (rather
        # than raising) so /index still returns 200 -- the point under test is the
        # reader's snapshot, not the writer's status code.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO parcels (id, path, updated_at) VALUES (?, ?, ?)",
            (sentinel_id, "sentinel.py", time.time()),
        )
        writer_holding.set()
        # Park with the transaction still open until the reader has taken its
        # snapshot, then ROLLBACK -- the sentinel must never become visible.
        reader_done.wait(timeout=10)
        conn.execute("ROLLBACK")
        return SimpleNamespace(parcels=[], contracts=[])

    monkeypatch.setattr(app_mod, "run_index", blocking_run_index)

    with TestClient(app) as client:

        def writer():
            return client.post("/index", json={"root": str(tmp_path)})

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            f_writer = ex.submit(writer)
            assert writer_holding.wait(timeout=10), "writer never opened its txn"

            # Reader, on its OWN connection, while the writer's txn is open.
            resp = client.get("/parcels")
            assert resp.status_code == 200, resp.text
            ids = {row["id"] for row in resp.json()}

            reader_done.set()
            f_writer.result(timeout=15)

    assert sentinel_id not in ids, (
        "reader observed the writer's UNCOMMITTED parcel -- reader and writer "
        "shared one connection (pre-S4), breaking snapshot isolation"
    )

    # And after the rollback the sentinel exists nowhere.
    with TestClient(app) as client:
        final_ids = {row["id"] for row in client.get("/parcels").json()}
    assert sentinel_id not in final_ids


# --- db.transaction contract ------------------------------------------------


def test_transaction_rollback_does_not_swallow_a_writer_on_another_connection(
    tmp_path,
):
    """A rolled-back `db.transaction` on connection A must leave a committed
    single-statement write on connection B untouched -- the isolation the
    per-request model buys, made explicit."""
    dbpath = tmp_path / "bb.db"
    conn_a = db.init_db(dbpath)
    conn_b = db.connect(dbpath)

    # conn_b commits an independent single-statement write (autocommit). On the
    # pre-S4 shared-connection model this same write would have been issued on the
    # ONE connection and thus folded into conn_a's open transaction below -- and
    # swallowed by its rollback. On separate connections it is its own committed
    # transaction, immune to conn_a's rollback.
    conn_b.execute(
        "INSERT INTO events (type, ts) VALUES ('heartbeat', ?)", (time.time(),)
    )

    with pytest.raises(RuntimeError):
        with db.transaction(conn_a):
            conn_a.execute(
                "INSERT INTO parcels (id, path, updated_at) VALUES (?, ?, ?)",
                ("a.py::doomed", "a.py", time.time()),
            )
            raise RuntimeError("boom")

    # conn_a's parcel was rolled back; conn_b's event survived (not swallowed).
    reader = db.connect(dbpath)
    assert reader.execute(
        "SELECT COUNT(*) AS n FROM parcels WHERE id = 'a.py::doomed'"
    ).fetchone()["n"] == 0
    assert reader.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type = 'heartbeat'"
    ).fetchone()["n"] == 1
    conn_a.close()
    conn_b.close()
    reader.close()


def test_transaction_refuses_to_nest(tmp_path):
    """One transaction per connection: entering `db.transaction` on a connection
    that already has an open transaction is a programming error, not a silent
    nested scope whose fate rides on the inner rollback."""
    conn = db.init_db(tmp_path / "bb.db")
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.ProgrammingError):
            with db.transaction(conn):
                pass
    finally:
        conn.execute("ROLLBACK")
        conn.close()


def test_transaction_commits_on_success(tmp_path):
    """The happy path still commits and leaves no transaction dangling."""
    conn = db.init_db(tmp_path / "bb.db")
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO parcels (id, path, updated_at) VALUES (?, ?, ?)",
            ("m.py::f", "m.py", time.time()),
        )
    assert conn.in_transaction is False
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM parcels WHERE id = 'm.py::f'"
    ).fetchone()["n"] == 1
    conn.close()
