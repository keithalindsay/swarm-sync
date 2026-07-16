"""Standalone launcher for the swarm-sync blackboard server.

Runs the FastAPI blackboard (swarmsync.server.app.create_app) under uvicorn so a
coordinated multi-agent session has a shared blackboard to lease against.

    swarmsync-serve --db /tmp/swarmsync.db --port 8787

See DESIGN.md for the coordination model.
"""

from __future__ import annotations

import argparse

import uvicorn

from swarmsync.server.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="swarmsync-serve", description=__doc__)
    parser.add_argument("--db", default="swarmsync.db", help="blackboard SQLite path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    app = create_app(args.db)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
