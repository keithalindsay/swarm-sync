"""Standalone helper process for demo test case #4 (crash mid-edit recovery).

Not a test and not imported by anything else -- `demo/run_demo.py` launches this
file as a REAL, separate OS process (`subprocess.Popen`), lets it run the normal
agent lifecycle (`agent.runner.run_agent`) far enough to hold a live write-lease
against the (already running) blackboard server, then SIGKILLs the process while
it is deliberately hanging inside `agent.mutators.slow_edit`.

Only a genuinely separate OS process makes this a real test of DESIGN.md §6's
"agent crash mid-edit" line: killing an in-process thread would also take the
coordinator/blackboard connection down with it, which is not what a real agent
crash looks like. This script's only job is to run `run_agent` against
whatever live server URL it's given on the command line and then hang -- the
parent (`run_demo.py`) does all the observation (waiting for `lease_granted`,
killing this process, waiting for `reaped`, reassigning the task).
"""
from __future__ import annotations

import argparse

from swarmsync.agent import mutators
from swarmsync.agent.client import BlackboardClient
from swarmsync.agent.runner import run_agent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="blackboard server base URL")
    parser.add_argument("--repo", required=True, help="filesystem path to the git repo")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument(
        "--parcel", required=True, help='parcel id this process leases, e.g. "formats.py::percent"'
    )
    parser.add_argument("--path", required=True, help="file path relative to repo")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--ttl", type=float, required=True, help="lease TTL in seconds")
    args = parser.parse_args()

    client = BlackboardClient(args.base_url)
    # This call never returns under normal operation: `mutators.slow_edit(hang=True)`
    # writes its edit to disk (real, uncommitted work in the worktree) and then
    # spins forever -- the parent process's SIGKILL is the only way this exits.
    run_agent(
        agent_id=args.agent_id,
        client=client,
        repo=args.repo,
        task=args.task,
        target_parcels=[args.parcel],
        mutator=mutators.slow_edit,
        mutator_kwargs={
            "path": args.path,
            "symbol": args.symbol,
            "new_body": 'return "CRASH-AGENT-PARTIAL-EDIT-SHOULD-NEVER-REACH-TRUNK"',
            "hang": True,
        },
        base_commit=args.base_commit,
        lease_ttl=args.ttl,
    )


if __name__ == "__main__":
    main()
