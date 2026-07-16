"""U1 — Blackboard DB + schema. DESIGN.md §4.1.

Done when:
  - init_db(tmp) creates all 6 tables
  - PRAGMA journal_mode returns 'wal'
  - a second init_db on the same file is a no-op (idempotent, no errors, no data loss)
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from swarmsync.blackboard import db
from swarmsync.blackboard.models import Event, Lease, Parcel


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r["name"] for r in rows}


def test_init_db_creates_all_six_tables(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        names = _table_names(conn)
        assert set(db.EXPECTED_TABLES) <= names
        assert len(db.EXPECTED_TABLES) == 6
    finally:
        conn.close()


def test_journal_mode_is_wal(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_foreign_keys_enabled(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_second_init_db_is_idempotent_no_op(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn1 = db.init_db(dbfile)
    now = time.time()
    conn1.execute(
        "INSERT INTO parcels (id, path, symbol, kind, blast_radius, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("a.py::foo", "a.py", "foo", "function", 0, now),
    )
    conn1.close()

    # Re-running init_db against the same file must not error and must not wipe
    # or duplicate existing data / schema objects.
    conn2 = db.init_db(dbfile)
    try:
        names = _table_names(conn2)
        assert set(db.EXPECTED_TABLES) <= names

        rows = conn2.execute("SELECT * FROM parcels").fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == "a.py::foo"

        # calling init_db a third time still doesn't error or duplicate
        conn3 = db.init_db(dbfile)
        try:
            rows3 = conn3.execute("SELECT * FROM parcels").fetchall()
            assert len(rows3) == 1
        finally:
            conn3.close()
    finally:
        conn2.close()


def test_connect_without_init_on_existing_db_reuses_schema(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    conn.close()

    conn2 = db.connect(dbfile)
    try:
        names = _table_names(conn2)
        assert set(db.EXPECTED_TABLES) <= names
        assert conn2.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn2.close()


def test_row_factory_is_sqlite_row(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        conn.execute(
            "INSERT INTO events (agent_id, type, payload, ts) VALUES (?, ?, ?, ?)",
            ("agent-1", "planned", "{}", time.time()),
        )
        row = conn.execute("SELECT * FROM events").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["type"] == "planned"
        assert row["agent_id"] == "agent-1"
    finally:
        conn.close()


def test_reset_removes_db_file(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    conn.close()
    assert dbfile.exists()

    db.reset(dbfile)
    assert not dbfile.exists()

    # reset on an already-absent file must not raise
    db.reset(dbfile)


def test_events_seq_autoincrements(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        cur1 = conn.execute(
            "INSERT INTO events (agent_id, type, payload, ts) VALUES (?, ?, ?, ?)",
            ("a1", "planned", None, time.time()),
        )
        cur2 = conn.execute(
            "INSERT INTO events (agent_id, type, payload, ts) VALUES (?, ?, ?, ?)",
            ("a1", "lease_granted", None, time.time()),
        )
        assert cur2.lastrowid > cur1.lastrowid
    finally:
        conn.close()


def test_leases_reference_parcels_foreign_key_enforced(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        now = time.time()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO leases "
                "(parcel_id, agent_id, mode, acquired_at, ttl_expires_at, heartbeat_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("does.not.exist", "agent-1", "write", now, now + 30, now, "active"),
            )
    finally:
        conn.close()


def test_models_validate_from_sqlite_rows(tmp_path):
    dbfile = tmp_path / "blackboard.db"
    conn = db.init_db(dbfile)
    try:
        now = time.time()
        conn.execute(
            "INSERT INTO parcels (id, path, symbol, kind, blast_radius, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("a.py::foo", "a.py", "foo", "function", 2, now),
        )
        row = conn.execute("SELECT * FROM parcels WHERE id='a.py::foo'").fetchone()
        parcel = Parcel.model_validate(dict(row))
        assert parcel.id == "a.py::foo"
        assert parcel.kind == "function"
        assert parcel.blast_radius == 2

        conn.execute(
            "INSERT INTO leases "
            "(parcel_id, agent_id, mode, acquired_at, ttl_expires_at, heartbeat_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("a.py::foo", "agent-1", "write", now, now + 30, now, "active"),
        )
        lrow = conn.execute("SELECT * FROM leases").fetchone()
        lease = Lease.model_validate(dict(lrow))
        assert lease.mode == "write"
        assert lease.status == "active"

        conn.execute(
            "INSERT INTO events (agent_id, type, payload, ts) VALUES (?, ?, ?, ?)",
            ("agent-1", "lease_granted", "{}", now),
        )
        erow = conn.execute("SELECT * FROM events").fetchone()
        event = Event.model_validate(dict(erow))
        assert event.type == "lease_granted"
    finally:
        conn.close()
