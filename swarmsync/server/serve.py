"""Standalone launcher for the swarm-sync blackboard server.

Runs the FastAPI blackboard (swarmsync.server.app.create_app) under uvicorn so a
coordinated multi-agent session has a shared blackboard to lease against.

    swarmsync-serve --db /tmp/swarmsync.db --port 8787

See DESIGN.md for the coordination model.
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from swarmsync.server.app import MultiRootError, check_single_root, create_app


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
    args = parser.parse_args()

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

    app = create_app(args.db)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
