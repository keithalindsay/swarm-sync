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
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, TextIO

import httpx

import swarmsync
from swarmsync import __version__, config
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


# --- hook wiring (init-hooks / doctor share this) ----------------------------------

# The four Claude Code hook events swarm-sync wires, mirroring README's Setup block:
# (event, matcher-or-None, adapter subcommand, timeout seconds).
_HOOK_SPECS = [
    ("PreToolUse", "Edit|Write|MultiEdit|NotebookEdit", "precheck", 10),
    ("PostToolUse", "Edit|Write|MultiEdit|NotebookEdit", "postupdate", 10),
    ("SubagentStop", None, "release", 10),
    ("SessionStart", None, "session-start", 15),
]

ACTIVE_MARKER_FILENAME = ".swarmsync-active"


def _git_toplevel(cwd: Path) -> Optional[Path]:
    """`git rev-parse --show-toplevel` for `cwd`, or None if not in a git repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def _resolve_hook_command() -> str:
    """The command `init-hooks` wires. Prefer the guard shim shipped in a
    source/editable checkout (`<repo>/scripts/swarmsync-hook-guard` -- it makes
    inactive editing pay zero Python startup); fall back to the `swarmsync-hook`
    console script name for installs that don't ship `scripts/`."""
    guard = Path(swarmsync.__file__).resolve().parent.parent / "scripts" / "swarmsync-hook-guard"
    if guard.is_file():
        return str(guard)
    return "swarmsync-hook"


def _is_swarmsync_entry(entry: dict) -> bool:
    """Whether a settings.json hook entry is one of ours (guard shim or the
    console script) -- so we can replace rather than duplicate on re-run, and
    detect wiring without matching a specific absolute path."""
    for hook in entry.get("hooks", []):
        if "swarmsync-hook" in hook.get("command", ""):
            return True
    return False


def _hook_entry(command_base: str, subcmd: str, matcher: Optional[str], timeout: int) -> dict:
    entry: dict = {
        "hooks": [{"type": "command", "command": f"{command_base} {subcmd}", "timeout": timeout}]
    }
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def _merge_hooks(settings: dict, command_base: str) -> dict:
    """Return `settings` with swarm-sync's four hook entries present exactly once
    each -- preserving any non-swarm-sync hooks, replacing any prior swarm-sync
    ones (idempotent, and upgrades a stale command path)."""
    hooks = settings.setdefault("hooks", {})
    for event, matcher, subcmd, timeout in _HOOK_SPECS:
        entries = hooks.setdefault(event, [])
        entries[:] = [e for e in entries if not _is_swarmsync_entry(e)]
        entries.append(_hook_entry(command_base, subcmd, matcher, timeout))
    return settings


def _hooks_wired(toplevel: Path) -> Optional[str]:
    """Whether swarm-sync hooks are wired in the global or project settings.json;
    returns a human location string, or None if neither has them."""
    candidates = [
        ("global", Path.home() / ".claude" / "settings.json"),
        ("project", toplevel / ".claude" / "settings.json"),
    ]
    for label, path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entries in data.get("hooks", {}).values():
            if any(_is_swarmsync_entry(e) for e in entries):
                return f"{label} ({path})"
    return None


def _db_writable(db_path: str) -> tuple[bool, str]:
    p = Path(db_path)
    if p.exists():
        if os.access(p, os.W_OK):
            return True, f"{p} (writable)"
        return False, f"{p} exists but is not writable — fix its permissions"
    parent = p.parent if str(p.parent) not in ("", ".") else Path.cwd()
    if os.access(parent, os.W_OK):
        return True, f"{p} (will be created under a writable dir)"
    return False, f"{p}: directory {parent} is not writable — pick another --db/$SWARMSYNC_DB"


def cmd_init_hooks(client: BlackboardClient, args: argparse.Namespace, out: TextIO) -> int:
    """Write swarm-sync's hook block into settings.json (idempotent) and drop the
    `.swarmsync-active` marker so coordination is on for this repo."""
    settings_path = (
        Path.home() / ".claude" / "settings.json"
        if args.use_global
        else Path.cwd() / ".claude" / "settings.json"
    )
    try:
        existing = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}

    command_base = args.command_base or _resolve_hook_command()
    merged = _merge_hooks(copy.deepcopy(existing), command_base)

    toplevel = _git_toplevel(Path.cwd()) or Path.cwd()
    marker = toplevel / ACTIVE_MARKER_FILENAME

    if args.dry_run:
        print(f"# --dry-run: would write {settings_path}", file=out)
        print(json.dumps(merged, indent=2), file=out)
        print(f"# --dry-run: would drop marker {marker}", file=out)
        return 0

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    marker.touch()
    print(f"wired swarm-sync hooks into {settings_path}", file=out)
    print(f"  command: {command_base}", file=out)
    print(f"coordination ON: dropped marker {marker}", file=out)
    print("start the server with `swarmsync-serve --root <repo>` and run `swarmsync doctor`.", file=out)
    return 0


def cmd_doctor(client: BlackboardClient, args: argparse.Namespace, out: TextIO) -> int:
    """Diagnose a swarm-sync setup: each check prints pass/fail + a remedy, and
    the exit code is non-zero iff any check failed."""
    checks: list[tuple[str, bool, str]] = []

    health: Optional[dict] = None
    try:
        health = client.health()
        checks.append(("server reachable", True, args.url))
    except httpx.HTTPError as exc:
        checks.append(
            (
                "server reachable",
                False,
                f"cannot reach {args.url} ({exc}) — start `swarmsync-serve` or set $SWARMSYNC_URL",
            )
        )

    if health is not None:
        match = health["version"] == __version__
        checks.append(
            (
                "version match",
                match,
                __version__
                if match
                else f"CLI {__version__} != server {health['version']} — reinstall so both match",
            )
        )

    toplevel = _git_toplevel(Path.cwd())
    if toplevel is None:
        checks.append(
            ("in a git repo", False, "cwd is not inside a git repository — run from your repo")
        )
    else:
        checks.append(("in a git repo", True, str(toplevel)))
        if health is not None:
            same = Path(health["root"]).resolve() == toplevel.resolve()
            checks.append(
                (
                    "managed root == repo",
                    same,
                    str(toplevel)
                    if same
                    else f"server root {health['root']} is not this repo — restart "
                    f"`swarmsync-serve --root {toplevel}`",
                )
            )
        active = config.active() or (toplevel / ACTIVE_MARKER_FILENAME).exists()
        checks.append(
            (
                "coordination active",
                active,
                "marker/env present"
                if active
                else f"coordination is OFF — `touch {toplevel / ACTIVE_MARKER_FILENAME}` "
                "(or set SWARMSYNC_ACTIVE=1)",
            )
        )
        wired = _hooks_wired(toplevel)
        checks.append(
            (
                "hooks wired",
                wired is not None,
                wired
                if wired is not None
                else "no swarm-sync hooks in global/project settings.json — run `swarmsync init-hooks`",
            )
        )

    checks.append(("db writable", *_db_writable(config.db_path())))

    failed = 0
    for label, ok, detail in checks:
        print(f"[{'ok  ' if ok else 'FAIL'}] {label}: {detail}", file=out)
        if not ok:
            failed += 1
    print(
        f"\n{failed} check(s) failed — fix the remedy on each FAIL line."
        if failed
        else "\nall checks passed.",
        file=out,
    )
    return 1 if failed else 0


_COMMANDS = {
    "status": cmd_status,
    "holds": cmd_holds,
    "free": cmd_free,
    "events": cmd_events,
    "doctor": cmd_doctor,
    "init-hooks": cmd_init_hooks,
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

    sub.add_parser(
        "doctor", help="diagnose the setup (server/root/marker/hooks/DB/version)"
    )

    init = sub.add_parser(
        "init-hooks", help="wire the Claude Code hook block into settings.json + activate"
    )
    init.add_argument(
        "--global",
        dest="use_global",
        action="store_true",
        help="write ~/.claude/settings.json instead of <cwd>/.claude/settings.json",
    )
    init.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be written; touch nothing",
    )
    init.add_argument(
        "--command",
        dest="command_base",  # NOT "command" -- that dest belongs to the subparser
        default=None,
        help="override the hook command base (default: the guard shim, or "
        "`swarmsync-hook` when scripts/ isn't shipped)",
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
