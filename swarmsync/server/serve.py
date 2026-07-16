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

from swarmsync.server.app import _managed_roots, create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="swarmsync-serve", description=__doc__)
    parser.add_argument("--db", default="swarmsync.db", help="blackboard SQLite path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--root",
        action="append",
        dest="roots",
        metavar="PATH",
        help="repo path this server may index/integrate (repeatable). Overrides "
        "SWARMSYNC_ROOTS, which itself defaults to the launch cwd.",
    )
    args = parser.parse_args()

    if args.roots:
        os.environ["SWARMSYNC_ROOTS"] = os.pathsep.join(args.roots)

    # Say the managed roots out loud at boot. Getting them wrong does not raise --
    # it makes /index 403, which leaves the parcel map empty, which makes every hook
    # fail open, i.e. silently NO coordination at all. An operator who can see this
    # line next to the repo they meant to coordinate can spot that in one glance.
    roots = _managed_roots()
    print(f"swarm-sync: managed roots: {os.pathsep.join(roots)}", flush=True)
    print(
        "swarm-sync: /index and /integrate will 403 for any path outside those roots "
        "(set SWARMSYNC_ROOTS or pass --root).",
        flush=True,
    )

    app = create_app(args.db)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
