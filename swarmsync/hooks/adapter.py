"""Claude Code HOOKS adapter for swarm-sync's lease coordination.

Console entry point: `swarmsync-hook` (see `[project.scripts]` in pyproject.toml),
invoked by Claude Code's hook runner as `swarmsync-hook <subcommand>` with a JSON
hook-event payload on stdin. This module is deliberately a thin translation layer:
it never reimplements lease semantics -- every read/write against the blackboard
goes through `agent.client.BlackboardClient` (§4.2/§4.3) or, for `POST /index`
(no `BlackboardClient` wrapper exists for that one endpoint -- `demo/run_demo.py`
calls it the same direct way), straight through the same http-like object.

Subcommands (argv[0] after `sys.argv[1:]` slicing -- i.e. "argv[1]" of the raw
process invocation):

  precheck      PreToolUse.  Edit/Write/MultiEdit/NotebookEdit only; every other
                tool is an immediate ALLOW. Maps the tool's target file to its
                whole-file parcel id, checks the blackboard's active leases, and
                either allows (free, or already held by this same agent_id) or
                acquires a fresh write-lease (free) or denies (held by another
                agent, or this agent lost the acquire race). Refreshes the TTL of
                a lease this agent already holds (S5 keepalive).
  postupdate    PostToolUse. Re-parses the edited file with
                `classifier.indexer.parse_file` and POSTs the freshly re-derived
                content_hash + a deterministic state_summary to /parcel/update
                (or a raw-byte hash + 'dirty/unparseable' marker if the edit left
                the file syntactically invalid). Refreshes the lease TTL (S5).
                Never releases the lease here -- the agent keeps it until it
                stops (SubagentStop/Stop -> `release`).
  release       SubagentStop/Stop. Releases every active lease this agent_id
                holds (GET /leases, filter, POST /release each).
  session-start SessionStart. Best-effort POST /index of the repo root if the
                blackboard is reachable; otherwise a silent no-op (the same
                fail-open umbrella below turns "unreachable" into "no-op").

--- SAFETY (read this before touching the logic below) -----------------------

This adapter gates real Edit/Write/MultiEdit tool calls in the user's actual
coding session, so it is held to a much higher bar than an ordinary swarm-sync
unit: it must NEVER be the reason a normal (non-swarm-sync) Claude Code session
gets stuck.

1. OPT-IN. `_is_active()` is the FIRST thing every subcommand checks (right
   after parsing the payload, before any blackboard I/O). If neither
   `SWARMSYNC_ACTIVE=1` nor a `.swarmsync-active` marker file at the resolved
   repo root is present, every subcommand returns 0 immediately having made
   zero network calls and printed nothing. A user who has never heard of
   swarm-sync is completely unaffected even if this hook is wired into their
   settings.json.

2. FAIL-OPEN. Everything from "parse the JSON on stdin" through "talk to the
   blackboard" is wrapped in one broad `try/except Exception` in `main()`
   (see `_run`). ANY failure -- malformed/absent stdin, the blackboard being
   down, a timeout, a KeyError, anything -- prints a one-line note to stderr
   (for a curious operator; Claude Code does not surface stderr to the model)
   and returns exit code 0 with NO deny JSON on stdout. `precheck` only ever
   emits a `deny` when it has a *positive, confirmed* conflicting lease held
   by a *different* agent_id read back from the blackboard -- never as a
   side effect of an internal error. Getting this backwards (denying on
   error) would be the one bug in this file capable of bricking a real
   editing session, so every blackboard call below is one that a raised
   exception simply falls through to that outer handler.

3. SHORT TIMEOUTS. The default `httpx.Client` this module builds for the real
   console-script path uses a 2s timeout (`_DEFAULT_TIMEOUT_SECONDS`), so an
   unreachable/hung blackboard server fails fast into the fail-open path
   instead of stalling the tool call.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, TextIO

import httpx

from swarmsync.agent.client import BlackboardClient
from swarmsync.blackboard.models import (
    LEASE_TTL_FLOOR_SECONDS,
    LEASE_TTL_MAX_SECONDS,
)
from swarmsync.classifier.indexer import MODULE_SYMBOL, parse_file

# --- config ------------------------------------------------------------------

DEFAULT_SWARMSYNC_URL = "http://127.0.0.1:8787"
_DEFAULT_TIMEOUT_SECONDS = 2.0
ACTIVE_MARKER_FILENAME = ".swarmsync-active"

# S5 keepalive: the TTL the hook acquires/renews a lease with. The server's own
# default lease TTL is 30s (`server.leases.DEFAULT_TTL_SECONDS`) -- far too short
# to survive normal agent think time between a precheck and its postupdate (a
# single big edit can take longer than that to generate), which would silently
# expire the "one-agent-per-file" lease mid-session and let the reaper hand the
# file to someone else. So the hook acquires with a MUCH longer TTL and renews it
# on every precheck AND postupdate (see `cmd_precheck`/`cmd_postupdate`). Override
# with `SWARMSYNC_LEASE_TTL` (seconds); the tests use a short value to exercise
# renewal across a window quickly.
DEFAULT_HOOK_LEASE_TTL_SECONDS = 300.0

# Tool names `precheck`/`postupdate` care about; every other `tool_name` is an
# immediate ALLOW / no-op. Matches Claude Code's Edit-family tool set.
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# HTTP-like factory: given a base URL, return something exposing .get/.post
# (a real httpx.Client, or -- in tests -- a fastapi.testclient.TestClient).
# Matches the duck-typed `_HttpLike` protocol `BlackboardClient` itself
# accepts (agent/client.py).
HttpFactory = Callable[[str], Any]


def _hook_lease_ttl(err: TextIO = sys.stderr) -> float:
    """The lease TTL (seconds) the hook acquires/renews with, from `SWARMSYNC_LEASE_TTL`.

    C9: a nonsensical env value must NOT silently disable protection. The old version
    accepted ANY float, so a single config typo (`SWARMSYNC_LEASE_TTL=0`) parsed fine
    and every hook-path lease was born expired -- granted AND dead, so a second agent
    was also granted while prechecks kept saying "allow". Now a value that is not a
    finite float, is `<= 0`, or exceeds the ceiling is REFUSED: we log a one-line note
    to stderr (Claude Code doesn't surface stderr to the model, but a curious operator
    sees it) and fall back to the safe DEFAULT rather than the caller's poison value.

    C13 (defense-in-depth): a value below `LEASE_TTL_FLOOR_SECONDS` (2x the SQLite
    busy_timeout) is still USED -- the hook's own keepalive tests legitimately run
    sub-second TTLs -- but warned about, since in a real deployment a TTL that small
    shrinks the live window toward request latency and can reopen the heartbeat race.
    """
    raw = os.environ.get("SWARMSYNC_LEASE_TTL")
    if raw is None:
        return DEFAULT_HOOK_LEASE_TTL_SECONDS
    try:
        ttl = float(raw)
    except (ValueError, TypeError):
        err.write(
            f"swarmsync-hook: SWARMSYNC_LEASE_TTL={raw!r} is not a number; "
            f"falling back to default {DEFAULT_HOOK_LEASE_TTL_SECONDS}s\n"
        )
        return DEFAULT_HOOK_LEASE_TTL_SECONDS
    # `float('nan')`/`float('inf')` parse but are not usable TTLs; the `not > 0` /
    # `> max` checks below reject inf, and nan fails every comparison so catch it too.
    if ttl != ttl or ttl <= 0 or ttl > LEASE_TTL_MAX_SECONDS:
        err.write(
            f"swarmsync-hook: SWARMSYNC_LEASE_TTL={raw!r} is out of bounds "
            f"(0 < ttl <= {LEASE_TTL_MAX_SECONDS}); falling back to default "
            f"{DEFAULT_HOOK_LEASE_TTL_SECONDS}s rather than disabling lease protection\n"
        )
        return DEFAULT_HOOK_LEASE_TTL_SECONDS
    if ttl < LEASE_TTL_FLOOR_SECONDS:
        err.write(
            f"swarmsync-hook: SWARMSYNC_LEASE_TTL={ttl}s is below the recommended "
            f"floor of {LEASE_TTL_FLOOR_SECONDS}s (2x the SQLite busy_timeout); a TTL "
            "this small can race the heartbeat liveness check under load\n"
        )
    return ttl


def _default_http_factory(base_url: str) -> httpx.Client:
    # S3 security: if the operator gated the blackboard with SWARMSYNC_TOKEN, send it
    # as a bearer token on every request so the hook can still reach the (now
    # authenticated) mutating routes. Unset -> no header, open blackboard, unchanged
    # behavior. The header is a default on the client, so it rides along on both the
    # BlackboardClient calls and cmd_session_start's direct http.post.
    headers: dict[str, str] = {}
    token = os.environ.get("SWARMSYNC_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        base_url=base_url, timeout=_DEFAULT_TIMEOUT_SECONDS, headers=headers
    )


_USAGE = (
    "swarmsync-hook <subcommand>\n"
    "\n"
    "Claude Code hooks adapter for swarm-sync lease coordination. Reads a JSON\n"
    "hook-event payload on stdin; the subcommand is the first argument.\n"
    "\n"
    "Subcommands:\n"
    "  precheck       PreToolUse  -- lease-gate an Edit/Write/MultiEdit/NotebookEdit\n"
    "  postupdate     PostToolUse -- re-hash the edited file, POST /parcel/update\n"
    "  release        Stop/SubagentStop -- release this agent's active leases\n"
    "  session-start  SessionStart -- best-effort POST /index of the repo root\n"
    "\n"
    "Environment:\n"
    "  SWARMSYNC_ACTIVE=1        enable enforcement (else every subcommand no-ops)\n"
    "  SWARMSYNC_URL            blackboard base URL (default http://127.0.0.1:8787)\n"
    "  SWARMSYNC_TOKEN          bearer token sent when the blackboard requires auth\n"
    "  SWARMSYNC_LEASE_TTL      lease TTL seconds the hook acquires/renews with\n"
)


# --- activation ----------------------------------------------------------------


def _is_active(env: Mapping[str, str], repo_root: Path) -> bool:
    """DESIGN-adjacent opt-in gate (see module docstring, requirement 1).

    `SWARMSYNC_ACTIVE=1` wins outright (e.g. CI/demo runs that always want
    enforcement without touching the filesystem); otherwise a
    `.swarmsync-active` marker file at the resolved repo root is the durable,
    per-repo way to turn this on without exporting an env var in every shell.
    """
    if env.get("SWARMSYNC_ACTIVE") == "1":
        return True
    return (repo_root / ACTIVE_MARKER_FILENAME).exists()


# --- file_path -> parcel id ------------------------------------------------------


def _relpath(file_path: Optional[str], repo_root: Path) -> Optional[str]:
    """Resolve a hook's (possibly relative, possibly absolute) `file_path`
    against `repo_root` and return a POSIX-style repo-relative path, or
    `None` if `file_path` is missing or resolves outside `repo_root`
    (nothing to lease -- treated as ALLOW/no-op by every caller).

    Symlink policy -- it depends on where the link POINTS, and both halves matter:

    * Leaf symlink to a file INSIDE this repo (an alias): map to the CANONICAL target,
      so editing `link.py` and editing `real.py` take the SAME lease. Two paths naming
      one inode are one file, and one file is one writer. Keying on the unresolved leaf
      gave them two different parcel ids, hence two independent write leases on one
      physical file in the ONE working tree hook subagents share -- a silently lost
      edit. `indexer.index_repo` skips in-repo aliases for the same reason, so the two
      agree that the canonical file is the parcel.
    * Leaf symlink to a file OUTSIDE the repo (S5): KEEP the leaf name. The indexer
      records it under its own in-repo name -- that is the only name this repo has for
      it -- so the hook must too. Resolving it here would land outside the repo, return
      None, and let the edit through with no lease at all: the exact pre-S5 bug.

    The parent directory is always resolved (canonicalizing `..` and symlinked parent
    dirs), so a genuine escape out of the repo still returns None.
    """
    if not file_path:
        return None
    p = Path(file_path)
    abs_p = p if p.is_absolute() else (repo_root / p)
    root = repo_root.resolve()
    try:
        resolved = abs_p.parent.resolve() / abs_p.name
        # An in-repo alias collapses onto its target; anything else keeps its own name.
        if resolved.is_symlink():
            target = resolved.resolve()
            if target.is_relative_to(root):
                resolved = target
        rel = resolved.relative_to(root)
    except (ValueError, OSError):
        return None
    return rel.as_posix()


def _parcel_id(relpath: str) -> str:
    """The whole-file parcel id for `relpath`: `"<relpath>::<module>"`.

    DESIGN §2's de-risking decision is that the *enforced* lease granularity
    defaults to file, not symbol -- so regardless of how finely
    `classifier.indexer` actually parsed a file into symbol-level parcels,
    this hook always leases the one synthetic per-file interstitial parcel
    id `indexer.parse_file` already emits for every file (`MODULE_SYMBOL`,
    imported rather than re-hardcoded so the two stay in lockstep). That is
    the safe default granularity named in this unit's brief: "whole-file
    parcel is the safe default granularity."
    """
    return f"{relpath}::{MODULE_SYMBOL}"


def _tool_file_path(tool_input: Mapping[str, Any]) -> Optional[str]:
    """`file_path` for Edit/Write/MultiEdit; NotebookEdit's hook payload uses
    `notebook_path` instead (the brief only guarantees `file_path` for the
    first three) -- fall back to it so NotebookEdit isn't silently ungated,
    but treat either being absent as "nothing to check" (ALLOW), never as
    an error.
    """
    return tool_input.get("file_path") or tool_input.get("notebook_path")


# --- precheck (PreToolUse) -------------------------------------------------------


def _deny_response(relpath: str, owner: str) -> dict:
    """Claude Code's PreToolUse structured-deny JSON shape."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"swarm-sync: {relpath} is leased by {owner}; "
                "pick different work or retry shortly."
            ),
        }
    }


def _find_holder(leases: list[dict], parcel_id: str) -> Optional[str]:
    for lease in leases:
        if lease.get("parcel_id") == parcel_id:
            return lease.get("agent_id")
    return None


def _find_lease(leases: list[dict], parcel_id: str) -> Optional[dict]:
    """The active lease dict for `parcel_id` (with its `id`/`agent_id`), or None."""
    for lease in leases:
        if lease.get("parcel_id") == parcel_id:
            return lease
    return None


def _keepalive(client: BlackboardClient, agent_id: str, lease: dict) -> None:
    """Refresh (heartbeat) `agent_id`'s own lease's TTL so it doesn't silently
    expire during think time (S5). No-op if the lease has no usable id."""
    lease_id = lease.get("id")
    if lease_id is not None:
        client.heartbeat(agent_id, lease_id, ttl=_hook_lease_ttl())


def cmd_precheck(
    tool_name: Optional[str],
    tool_input: Mapping[str, Any],
    client: BlackboardClient,
    repo_root: Path,
    agent_id: str,
) -> Optional[dict]:
    """Return a deny-JSON dict to print, or `None` for ALLOW (print nothing).

    Non-edit tools and files outside the repo are an immediate `None` (ALLOW)
    without touching the blackboard at all. For an in-repo edit target:
      - already leased by THIS agent_id -> refresh the TTL (S5 keepalive) and
        ALLOW (re-editing your own leased file must never self-deny);
      - leased by ANOTHER agent_id -> DENY;
      - free -> acquire a write lease (with the hook's long, renewing TTL);
        ALLOW on grant, DENY if the acquire itself loses the race (another
        agent's acquire won between our `GET /leases` read and our `POST /lease`).
    """
    if tool_name not in EDIT_TOOLS:
        return None

    relpath = _relpath(_tool_file_path(tool_input), repo_root)
    if relpath is None:
        return None
    parcel_id = _parcel_id(relpath)

    lease = _find_lease(client.leases(), parcel_id)
    if lease is not None:
        owner = lease.get("agent_id") or "another agent"
        if owner == agent_id:
            # KEEPALIVE: the hook is stateless across invocations, so the lease
            # id is re-read here rather than remembered; bumping its TTL on every
            # precheck keeps the "one-agent-per-file" lease alive across the think
            # time between successive edits instead of letting the 30s server TTL
            # expire it out from under a still-active agent.
            _keepalive(client, agent_id, lease)
            return None
        return _deny_response(relpath, owner)

    # `ensure_parcel=True`: this hook is handed whatever real file the agent is
    # editing -- `.ts`, `.yaml`, `package.json`, or a `.py` created since the last
    # index -- none of which the classifier (which only walks `*.py`) has emitted a
    # parcel for. Without this the acquire hits the parcels FK, 500s, and gets
    # swallowed by main()'s fail-open umbrella, leaving the file silently ungated
    # in a working tree that hook subagents SHARE. See `server.leases._ensure_parcel`.
    result = client.lease(
        agent_id, parcel_id, mode="write", ttl=_hook_lease_ttl(), ensure_parcel=True
    )
    if result.get("granted"):
        return None

    # Lost the acquire race against another agent between our read above and
    # this acquire -- re-read to name the winner in the deny message, falling
    # back to a generic phrase if the race is somehow already resolved.
    owner = _find_holder(client.leases(), parcel_id) or "another agent"
    return _deny_response(relpath, owner)


# --- postupdate (PostToolUse) -----------------------------------------------------


def _state_summary(relpath: str, agent_id: str, parcels: list, module_parcel) -> str:
    """A short, DETERMINISTIC note (DESIGN §2's `state_summary` heuristic
    style): built entirely from re-parsed file content + the acting
    agent_id, no wall-clock time or randomness, so re-running this on
    unchanged content always produces the identical string.
    """
    kind_counts = Counter(p.kind for p in parcels)
    kinds = ",".join(f"{kind}={count}" for kind, count in sorted(kind_counts.items()))
    return (
        f"swarm-sync hook: {relpath} edited by {agent_id}; "
        f"{len(parcels)} parcels ({kinds}); "
        f"module_hash={module_parcel.content_hash[:12]}"
    )


def _dirty_summary(relpath: str, agent_id: str, exc: Exception) -> str:
    """DETERMINISTIC marker for a syntactically-invalid edited file (no
    wall-clock/randomness): names the failure class, not its message (line
    numbers / offsets in the message would make it non-reproducible)."""
    return (
        f"swarm-sync hook: {relpath} edited by {agent_id}; "
        f"DIRTY/UNPARSEABLE ({type(exc).__name__})"
    )


def cmd_postupdate(
    tool_name: Optional[str],
    tool_input: Mapping[str, Any],
    client: BlackboardClient,
    repo_root: Path,
    agent_id: str,
) -> None:
    """Re-parse the just-edited file and POST its fresh content_hash (never
    the agent's self-reported one -- DESIGN §5.4/§6 "lying blackboard" rule)
    plus a deterministic state_summary to `/parcel/update`. Also refreshes the
    agent's lease TTL (S5 keepalive). Never releases the lease (the agent keeps
    it until SubagentStop/Stop -> `release`).

    If the edit left the file syntactically INVALID (SyntaxError) or unreadable
    as UTF-8 (UnicodeDecodeError), we do NOT silently no-out: that would leave
    the blackboard advertising the STALE last-good content_hash, hiding that the
    file is now dirty. Instead we push a raw whole-file byte hash + a
    'DIRTY/UNPARSEABLE' state_summary, so the parcel's content_hash genuinely
    changes and the summary flags that it can't be parsed until the agent fixes
    it (the integrator's re-index/test gate is the eventual backstop).
    """
    if tool_name not in EDIT_TOOLS:
        return
    relpath = _relpath(_tool_file_path(tool_input), repo_root)
    if relpath is None:
        return
    abs_path = repo_root / relpath
    if not abs_path.exists():
        return  # e.g. the edit deleted the file; nothing left to re-hash
    parcel_id = _parcel_id(relpath)

    # KEEPALIVE (S5): refresh this agent's lease on the edited parcel before
    # anything that could raise, so a long edit doesn't let the lease expire.
    lease = _find_lease(client.leases(), parcel_id)
    if lease is not None and lease.get("agent_id") == agent_id:
        _keepalive(client, agent_id, lease)

    try:
        parcels = parse_file(abs_path, rel_path=relpath)
    except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
        # Unparseable edit: push a raw-byte content_hash + dirty marker instead
        # of a silent no-op (SyntaxError / a NUL byte -> ValueError from
        # ast.parse / invalid UTF-8 -> UnicodeDecodeError).
        raw_hash = hashlib.sha256(abs_path.read_bytes()).hexdigest()
        client.parcel_update(
            agent_id, parcel_id, raw_hash, _dirty_summary(relpath, agent_id, exc)
        )
        return

    module_parcel = next((p for p in parcels if p.id == parcel_id), None)
    if module_parcel is None:
        return  # should be unreachable (parse_file always emits the module parcel)

    # `parse_file` always populates content_hash; the model types it Optional
    # (schema allows NULL), so pin the invariant for the str-typed update API.
    assert module_parcel.content_hash is not None
    client.parcel_update(
        agent_id,
        parcel_id,
        module_parcel.content_hash,
        _state_summary(relpath, agent_id, parcels, module_parcel),
    )


# --- release (SubagentStop/Stop) --------------------------------------------------


def cmd_release(client: BlackboardClient, agent_id: str) -> None:
    """Release every ACTIVE lease `agent_id` currently holds."""
    for lease in client.leases():
        if lease.get("agent_id") == agent_id:
            lease_id = lease.get("id")
            if lease_id is not None:
                client.release(agent_id, lease_id)


# --- session-start (SessionStart) -------------------------------------------------


def cmd_session_start(http: Any, repo_root: Path) -> None:
    """Best-effort `POST /index` of the repo root.

    No `BlackboardClient` wrapper exists for `/index` (the agent's own sync
    protocol, §4.3, never calls it -- only `demo/run_demo.py` does, the same
    direct way, straight against the http-like object). "Otherwise no-op" if
    the blackboard isn't reachable falls out of `main()`'s fail-open umbrella
    for free: a connection error/timeout here is just another exception that
    umbrella swallows.
    """
    http.post("/index", json={"root": str(repo_root)})


# --- payload parsing + dispatch ---------------------------------------------------


def _agent_id(payload: Mapping[str, Any]) -> str:
    """agent_id if present (inside a subagent), else session_id, else "main"
    (a top-level/"main" agent session has neither in Claude Code's payload
    shape) -- per this unit's brief.
    """
    return payload.get("agent_id") or payload.get("session_id") or "main"


def _repo_root(payload: Mapping[str, Any]) -> Path:
    cwd = payload.get("cwd") or os.getcwd()
    return Path(cwd).resolve()


def _dispatch(
    subcommand: str,
    payload: Mapping[str, Any],
    http_factory: Optional[HttpFactory],
    out: TextIO,
) -> int:
    repo_root = _repo_root(payload)

    # Requirement 1 (OPT-IN): checked before ANY blackboard I/O, for every
    # subcommand alike. Inactive means silent, zero-network-call ALLOW.
    if not _is_active(os.environ, repo_root):
        return 0

    base_url = os.environ.get("SWARMSYNC_URL", DEFAULT_SWARMSYNC_URL)
    owns_http = http_factory is None
    factory: HttpFactory = http_factory or _default_http_factory
    http = factory(base_url)
    try:
        client = BlackboardClient(http)
        agent_id = _agent_id(payload)
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input") or {}

        if subcommand == "precheck":
            decision = cmd_precheck(tool_name, tool_input, client, repo_root, agent_id)
            if decision is not None:
                out.write(json.dumps(decision))
                out.write("\n")
        elif subcommand == "postupdate":
            cmd_postupdate(tool_name, tool_input, client, repo_root, agent_id)
        elif subcommand == "release":
            cmd_release(client, agent_id)
        elif subcommand == "session-start":
            cmd_session_start(http, repo_root)
        # else: unrecognized subcommand -> no-op ALLOW, same as an inactive repo.
        return 0
    finally:
        if owns_http:
            http.close()


def main(
    argv: Optional[list[str]] = None,
    stdin: Optional[TextIO] = None,
    http_factory: Optional[HttpFactory] = None,
    out: Optional[TextIO] = None,
    err: Optional[TextIO] = None,
) -> int:
    """Entry point for the `swarmsync-hook` console script.

    `argv` defaults to `sys.argv[1:]` (so `argv[0]` here is the subcommand --
    "argv[1]" of the raw process invocation, per this unit's brief); `stdin`
    defaults to `sys.stdin`. `http_factory`/`out`/`err` are test-only
    injection points (a real invocation always uses the real defaults) --
    see tests/test_hook_adapter.py, which passes a `TestClient`-backed
    factory so no real network/server is needed.

    Requirement 2 (FAIL-OPEN): this function is the one place the "anything
    goes wrong -> ALLOW" umbrella lives. Everything from stdin parsing
    through the full dispatch is inside the single `try/except Exception`
    below; the only paths that ever print a `deny` JSON are inside
    `cmd_precheck`, called from *within* that same try, so a `deny` only ever
    reaches stdout after a real, confirmed conflicting lease read back from
    the blackboard -- never as a side effect of this except clause.
    """
    argv = list(argv if argv is not None else sys.argv[1:])
    stdin = stdin if stdin is not None else sys.stdin
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    if not argv:
        return 0  # no subcommand given -- nothing to enforce, ALLOW
    subcommand = argv[0]

    # `--help`/`-h`: print usage and exit 0. Only ever invoked by a human at a
    # shell (Claude Code's hook runner passes a real subcommand), so writing to
    # stdout here never interferes with the deny-JSON protocol.
    if subcommand in ("-h", "--help"):
        out.write(_USAGE)
        return 0

    try:
        raw = stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        return _dispatch(subcommand, payload, http_factory, out)
    except Exception as exc:  # noqa: BLE001 -- deliberate: see FAIL-OPEN above
        err.write(f"swarmsync-hook: {subcommand}: failing open ({exc!r})\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
