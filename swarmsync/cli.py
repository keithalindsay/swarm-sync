"""`swarmsync` -- the operator/agent CLI (WP5.1, U2/U5).

One read-only view onto a running blackboard, over the same HTTP surface an
agent's hook uses (default `$SWARMSYNC_URL`). Every command answers a question an
operator or a *denied* agent needs from a plain shell:

  * `status`  -- is the server up, bound to which repo, how busy? (U2)
  * `holds`   -- what parcels are held right now, by whom, for how long?
  * `free`    -- of THESE paths, which can I take?  (U5: pick different work)
  * `events`  -- what just happened (optionally `--follow`)?

`holds`/`free` are the work-discovery surface the deny message points denied
agents at: all reads hit unauthenticated GETs, so no token is needed here.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Optional, TextIO

import httpx

from swarmsync import config
from swarmsync.agent.client import BlackboardClient
from swarmsync.blackboard import parcel_id

# Poll cadence for `events --follow`. Deliberately not a knob: the loop is a
# convenience tail, not a low-latency stream (the blackboard is not a bus).
_FOLLOW_INTERVAL_SECONDS = 1.0
_DEFAULT_EVENT_COUNT = 20


def _fmt_ttl(expires_at: float, now: float) -> str:
    """Human 'time left on the hold' from a server-clock expiry. The launch-time
    clock-agreement assert (C13) keeps server and CLI clocks close enough that a
    client-side `now` is a fair reading."""
    remaining = expires_at - now
    if remaining <= 0:
        return "expired"
    if remaining < 90:
        return f"~{remaining:.0f}s"
    return f"~{remaining / 60:.0f}m"


def _fmt_payload(payload: Optional[str]) -> str:
    """Compact one-line rendering of an event's JSON payload (or '' if none)."""
    if not payload:
        return ""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return payload
    if not isinstance(data, dict):
        return str(data)
    return " ".join(f"{k}={v}" for k, v in data.items())


def _held_paths(leases: list[dict]) -> dict[str, list[str]]:
    """Map each held file path -> the agent ids holding a parcel in it. A file is
    held if ANY active lease targets a parcel inside it (whole-file or a symbol),
    so path-granularity `free` reflects symbol-granularity holds too."""
    held: dict[str, list[str]] = {}
    for lease in leases:
        path, _symbol = parcel_id.split(lease["parcel_id"])
        held.setdefault(path, []).append(lease["agent_id"])
    return held


def cmd_status(client: BlackboardClient, args: argparse.Namespace, out: TextIO) -> int:
    health = client.health()
    leases = client.leases()
    now = time.time()

    print(f"swarm-sync {health['version']}  —  UP", file=out)
    print(f"  root:   {health['root']}", file=out)
    print(f"  db:     {health['db_path']}", file=out)
    print(f"  leases: {health['active_leases']} active", file=out)
    print(f"  events: last seq {health['last_event_seq']}", file=out)

    if leases:
        print("\nactive holds:", file=out)
        for lease in leases:
            print(
                f"  {lease['parcel_id']:<40} {lease['agent_id']:<16} "
                f"{lease['mode']:<10} {_fmt_ttl(lease['ttl_expires_at'], now)}",
                file=out,
            )

    recent = client.events(tail=_DEFAULT_EVENT_COUNT)
    if recent:
        print("\nrecent events:", file=out)
        for ev in recent:
            _print_event(ev, out)
    return 0


def cmd_holds(client: BlackboardClient, args: argparse.Namespace, out: TextIO) -> int:
    leases = client.leases()
    if not leases:
        print("no active holds", file=out)
        return 0
    now = time.time()
    for lease in leases:
        print(
            f"{lease['parcel_id']:<40} {lease['agent_id']:<16} "
            f"{lease['mode']:<10} {_fmt_ttl(lease['ttl_expires_at'], now)}",
            file=out,
        )
    return 0


def cmd_free(client: BlackboardClient, args: argparse.Namespace, out: TextIO) -> int:
    """For each requested path: FREE, or HELD (by whom). Exit non-zero if ANY is
    held, so an agent can gate work in one line: `swarmsync free foo.py && edit`."""
    held = _held_paths(client.leases())
    any_held = False
    for path in args.paths:
        holders = held.get(path)
        if holders:
            any_held = True
            print(f"HELD  {path}  by {', '.join(sorted(set(holders)))}", file=out)
        else:
            print(f"FREE  {path}", file=out)
    return 1 if any_held else 0


def _print_event(ev: dict, out: TextIO) -> None:
    payload = _fmt_payload(ev.get("payload"))
    agent = ev.get("agent_id") or "-"
    line = f"  #{ev['seq']:<5} {ev['type']:<20} {agent:<16}"
    if payload:
        line += f" {payload}"
    print(line, file=out)


def cmd_events(client: BlackboardClient, args: argparse.Namespace, out: TextIO) -> int:
    recent = client.events(tail=args.n)
    for ev in recent:
        _print_event(ev, out)
    if not args.follow:
        return 0

    # --follow: page forward from the last seq we printed, forever, until Ctrl-C.
    since = recent[-1]["seq"] if recent else 0
    try:
        while True:
            time.sleep(_FOLLOW_INTERVAL_SECONDS)
            fresh = client.events(since=since)
            for ev in fresh:
                _print_event(ev, out)
                since = ev["seq"]
    except KeyboardInterrupt:
        return 0


_COMMANDS = {
    "status": cmd_status,
    "holds": cmd_holds,
    "free": cmd_free,
    "events": cmd_events,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swarmsync",
        description="Operator/agent view onto a running swarm-sync blackboard.",
    )
    parser.add_argument(
        "--url",
        default=config.url(),
        help="blackboard base URL (default: $SWARMSYNC_URL or http://127.0.0.1:8787)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=8.0,
        help="per-request timeout in seconds (default: 8.0)",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    sub.add_parser("status", help="is the server up, bound to which repo, how busy?")
    sub.add_parser("holds", help="list every active hold: parcel, holder, mode, TTL")

    free = sub.add_parser(
        "free", help="of THESE paths, which are free to take? (exit 1 if any held)"
    )
    free.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="repo-root-relative POSIX file paths to check",
    )

    events = sub.add_parser("events", help="recent events (optionally --follow)")
    events.add_argument(
        "-n",
        type=int,
        default=_DEFAULT_EVENT_COUNT,
        help=f"how many recent events to show (default: {_DEFAULT_EVENT_COUNT})",
    )
    events.add_argument(
        "--follow",
        action="store_true",
        help="keep polling for new events until interrupted",
    )
    return parser


def run(args: argparse.Namespace, client: BlackboardClient, out: TextIO) -> int:
    """Dispatch a parsed command against an already-built client. Split out from
    `main` so tests can drive it with a TestClient-backed client and a buffer."""
    return _COMMANDS[args.command](client, args, out)


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        with BlackboardClient(args.url, timeout=args.timeout) as client:
            return run(args, client, sys.stdout)
    except httpx.HTTPError as exc:
        # Server down / unreachable / a 5xx: the whole point of this CLI is to be
        # usable when coordination is misbehaving, so fail with a readable line
        # naming the URL, not an httpx traceback.
        print(
            f"swarm-sync: cannot reach the blackboard at {args.url} ({exc}). "
            f"Is `swarmsync-serve` running? Set --url or $SWARMSYNC_URL.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
