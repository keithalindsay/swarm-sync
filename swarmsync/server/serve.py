"""Standalone launcher for the swarm-sync blackboard server.

Runs the FastAPI blackboard (swarmsync.server.app.create_app) under uvicorn so a
coordinated multi-agent session has a shared blackboard to lease against.

    swarmsync-serve --db /tmp/swarmsync.db --port 8787

See DESIGN.md for the coordination model.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn

from swarmsync.server.app import MultiRootError, check_single_root, create_app
from swarmsync.server.leases import _NOW_SQL

# C13: how far SQLite's clock and Python's clock may drift before we refuse to serve.
# A couple of seconds is generous for any real host; the failure mode we are guarding
# is a badly wrong clock (VM resumed, unsynced container, TZ-as-offset misconfig), not
# sub-second jitter.
CLOCK_SKEW_TOLERANCE_SECONDS = 2.0


def assert_clock_agreement() -> None:
    """Refuse to start if SQLite's clock disagrees with Python's (C13).

    The heartbeat liveness predicate (`server.leases.heartbeat`) is evaluated on
    SQLite's OWN clock -- `julianday('now')`, via `_NOW_SQL` -- atomically with the
    SET, precisely so a slow/preempted statement can't revive a lapsed lease. But
    `acquire` and the reaper stamp/compare `ttl_expires_at` using Python's
    `time.time()`. Those two clocks are an UNSTATED invariant: if they disagree, a
    lease can look alive to one path and expired to the other, reopening the exact
    double-lease this system exists to prevent -- and nothing else checks it. So
    check it once, loudly, at startup, and name the problem if it fails.
    """
    conn = sqlite3.connect(":memory:")
    try:
        py_before = time.time()
        sqlite_now = conn.execute(f"SELECT {_NOW_SQL}").fetchone()[0]
        py_after = time.time()
    finally:
        conn.close()

    py_mid = (py_before + py_after) / 2.0
    skew = abs(float(sqlite_now) - py_mid)
    if skew > CLOCK_SKEW_TOLERANCE_SECONDS:
        raise SystemExit(
            "swarm-sync: refusing to start -- SQLite's clock and Python's clock "
            f"disagree by {skew:.1f}s (tolerance {CLOCK_SKEW_TOLERANCE_SECONDS}s). "
            "Lease liveness is checked on SQLite's julianday('now') while leases are "
            "stamped from Python time.time(); a mismatch this large can make a lease "
            "look alive to one path and expired to the other (the C13 double-lease). "
            "Fix the host/SQLite clock before serving."
        )


def rotate_stale_db(db_path: Path) -> Optional[Path]:
    """Move an existing blackboard DB aside to a timestamped backup (WP3.6 --fresh).

    The old file is RENAMED to `<db>.stale-<YYYYmmdd-HHMMSS>` -- data is never
    deleted; an operator who `--fresh`ed the wrong DB can always move it back. Any
    WAL sidecars (`<db>-wal`/`<db>-shm`) ride along under the same stale suffix:
    they can carry un-checkpointed writes, and a stale `-wal` sitting next to a
    brand-new empty DB of the same name is a corruption hazard, not a keepsake.
    Returns the backup path, or None when there was no DB to move.
    """
    if not db_path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.stale-{stamp}")
    n = 1
    while backup.exists():
        # Two --fresh boots inside one second: never overwrite the earlier backup.
        n += 1
        backup = db_path.with_name(f"{db_path.name}.stale-{stamp}-{n}")
    db_path.rename(backup)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            sidecar.rename(backup.with_name(backup.name + suffix))
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(prog="swarmsync-serve", description=__doc__)
    parser.add_argument("--db", default="swarmsync.db", help="blackboard SQLite path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--root",
        metavar="PATH",
        help="the ONE repo path this server may index/integrate. Overrides "
        "SWARMSYNC_ROOTS, which itself defaults to the launch cwd. Not repeatable: "
        "one server coordinates one repo (parcel ids carry no repo qualifier, so two "
        "roots would collide on the same ids). Run a second server for a second repo.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="start on a fresh, empty blackboard: an existing --db file is moved "
        "aside to <db>.stale-<YYYYmmdd-HHMMSS> (a backup -- never deleted) before "
        "the schema is created. Without this flag an existing DB is reused as-is.",
    )
    args = parser.parse_args()

    # C13: verify the cross-clock invariant before doing anything else. A wrong host
    # clock silently corrupts lease liveness, so fail here with a readable message
    # rather than serving and letting a double-lease slip through later.
    assert_clock_agreement()

    if args.root:
        os.environ["SWARMSYNC_ROOTS"] = args.root

    # Say the managed roots out loud at boot. Getting them wrong does not raise --
    # it makes /index 403, which leaves the parcel map empty, which makes every hook
    # fail open, i.e. silently NO coordination at all. An operator who can see this
    # line next to the repo they meant to coordinate can spot that in one glance.
    # Fail here, with a readable message, rather than letting the same check raise out
    # of uvicorn's startup as a traceback.
    try:
        root = check_single_root()
    except MultiRootError as exc:
        raise SystemExit(f"swarm-sync: {exc}") from None

    print(f"swarm-sync: managed root: {root}", flush=True)
    print(
        "swarm-sync: /index and /integrate will 403 for any path outside that root "
        "(set SWARMSYNC_ROOTS or pass --root).",
        flush=True,
    )

    if args.fresh:
        backup = rotate_stale_db(Path(args.db))
        if backup is not None:
            print(
                f"swarm-sync: --fresh: moved existing blackboard DB aside to {backup}",
                flush=True,
            )

    app = create_app(args.db)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
