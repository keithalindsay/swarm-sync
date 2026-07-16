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
                agent, or this agent lost the acquire race).
  postupdate    PostToolUse. Re-parses the edited file with
                `classifier.indexer.parse_file` and POSTs the freshly re-derived
                content_hash + a deterministic state_summary to /parcel/update.
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

import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, TextIO

import httpx

from swarmsync.agent.client import BlackboardClient
from swarmsync.classifier.indexer import MODULE_SYMBOL, parse_file

# --- config ------------------------------------------------------------------

DEFAULT_SWARMSYNC_URL = "http://127.0.0.1:8787"
_DEFAULT_TIMEOUT_SECONDS = 2.0
ACTIVE_MARKER_FILENAME = ".swarmsync-active"

# Tool names `precheck`/`postupdate` care about; every other `tool_name` is an
# immediate ALLOW / no-op. Matches Claude Code's Edit-family tool set.
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# HTTP-like factory: given a base URL, return something exposing .get/.post
# (a real httpx.Client, or -- in tests -- a fastapi.testclient.TestClient).
# Matches the duck-typed `_HttpLike` protocol `BlackboardClient` itself
# accepts (agent/client.py).
HttpFactory = Callable[[str], Any]


def _default_http_factory(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=_DEFAULT_TIMEOUT_SECONDS)


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
    """
    if not file_path:
        return None
    p = Path(file_path)
    abs_p = p if p.is_absolute() else (repo_root / p)
    try:
        rel = abs_p.resolve().relative_to(repo_root.resolve())
    except ValueError:
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
      - already leased by THIS agent_id -> ALLOW (re-editing your own leased
        file must never self-deny);
      - leased by ANOTHER agent_id -> DENY;
      - free -> acquire a write lease; ALLOW on grant, DENY if the acquire
        itself loses the race (another agent's acquire won between our
        `GET /leases` read and our `POST /lease`).
    """
    if tool_name not in EDIT_TOOLS:
        return None

    relpath = _relpath(_tool_file_path(tool_input), repo_root)
    if relpath is None:
        return None
    parcel_id = _parcel_id(relpath)

    holder = _find_holder(client.leases(), parcel_id)
    if holder is not None:
        if holder == agent_id:
            return None
        return _deny_response(relpath, holder)

    result = client.lease(agent_id, parcel_id, mode="write")
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


def cmd_postupdate(
    tool_name: Optional[str],
    tool_input: Mapping[str, Any],
    client: BlackboardClient,
    repo_root: Path,
    agent_id: str,
) -> None:
    """Re-parse the just-edited file and POST its fresh content_hash (never
    the agent's self-reported one -- DESIGN §5.4/§6 "lying blackboard" rule)
    plus a deterministic state_summary to `/parcel/update`. Never releases
    the lease (the agent keeps it until SubagentStop/Stop -> `release`).
    """
    if tool_name not in EDIT_TOOLS:
        return
    relpath = _relpath(_tool_file_path(tool_input), repo_root)
    if relpath is None:
        return
    abs_path = repo_root / relpath
    if not abs_path.exists():
        return  # e.g. the edit deleted the file; nothing left to re-hash

    parcels = parse_file(abs_path, rel_path=relpath)
    parcel_id = _parcel_id(relpath)
    module_parcel = next((p for p in parcels if p.id == parcel_id), None)
    if module_parcel is None:
        return  # should be unreachable (parse_file always emits the module parcel)

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
    factory = http_factory or _default_http_factory
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
