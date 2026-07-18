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

   C10 TWO-TIER refinement: fail-open stays the DEFAULT, but it is no longer
   unconditional for `precheck`. If a coordinated session is active AND a
   successful blackboard contact was recorded recently (`.swarmsync-last-contact`),
   an unreachable blackboard is read as CONTENTION, not absence -- so instead of
   silently un-gating the shared tree the umbrella emits a retry-deny (see
   `_maybe_fail_closed`). Absent any recent contact (a broken/never-started
   setup), it still fails OPEN and never bricks the session.

3. BALANCED TIMEOUTS (C10). The default `httpx.Client` this module builds for
   the real console-script path uses `_DEFAULT_TIMEOUT_SECONDS`, deliberately set
   ABOVE the server's SQLite `busy_timeout` so a merely-BUSY (contended) blackboard
   is waited out rather than mistaken for a dead one -- a truly unreachable/hung
   server still fails within a few seconds. Which way that failure resolves is no
   longer unconditionally "open": see FAIL-OPEN's two-tier note above.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, TextIO

import httpx

from swarmsync.agent.client import BlackboardClient
from swarmsync.blackboard.db import BUSY_TIMEOUT_SECONDS
from swarmsync.blackboard.models import (
    LEASE_TTL_FLOOR_SECONDS,
    LEASE_TTL_MAX_SECONDS,
)
from swarmsync.classifier.indexer import MODULE_SYMBOL, parse_file

# --- config ------------------------------------------------------------------

DEFAULT_SWARMSYNC_URL = "http://127.0.0.1:8787"

# HTTP client timeout for the real console-script path. C10: this MUST sit comfortably
# above the server's SQLite `busy_timeout` (blackboard/db.BUSY_TIMEOUT_SECONDS, 5s).
# The old 2s value INVERTED that: under write contention the server can legitimately
# spend up to its whole busy_timeout waiting on a lock before answering, so a 2s client
# timeout fired FIRST and the fail-open umbrella turned it into a silent ALLOW -- exactly
# when the tree was busiest. Deriving it from the busy_timeout (rather than a bare number)
# keeps the two in lockstep if the server's is ever retuned.
_DEFAULT_TIMEOUT_SECONDS = BUSY_TIMEOUT_SECONDS + 3.0  # 8s: > the 5s server busy_timeout

ACTIVE_MARKER_FILENAME = ".swarmsync-active"

# C10 two-tier fail policy. A successful blackboard contact stamps this file at the repo
# root with the wall-clock time. When the blackboard is later unreachable DURING an active
# coordinated session AND that stamp is recent, the silence is contention (a busy server),
# not absence (a broken/never-started setup): fail CLOSED with a retry-deny instead of
# silently un-gating the shared tree. With no recent stamp we still fail OPEN, so a
# genuinely-broken setup never bricks a real editing session.
LAST_CONTACT_FILENAME = ".swarmsync-last-contact"

# How recent a successful contact must be for an unreachable-blackboard failure to be
# read as contention (fail closed) rather than absence (fail open). A few multiples of
# the client timeout: long enough to bridge a burst of lock contention, short enough that
# a server that truly went away stops gating edits soon after.
RECENT_CONTACT_WINDOW_SECONDS = 60.0

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


def _deny_response(
    relpath: str, owner: str, holder_ttl_expires_at: Optional[float] = None
) -> dict:
    """Claude Code's PreToolUse structured-deny JSON shape (U3: a deny that actually
    informs).

    The reason names the file, the holding agent, and -- when known -- roughly how much
    TTL is left on the hold, plus the crucial caveat that a hook lease RENEWS on every
    precheck/postupdate while its holder is active, so it does NOT lapse on its own. The
    old "retry shortly" wording was removed precisely because it was misleading: telling
    an agent to wait for a lease that keeps renewing sends it into a busy-wait that never
    clears. The actionable pointer (`GET <url>/leases`) lets the agent inspect the live
    holder set instead. All fields come from the acquire/leases response already in hand
    -- no second round-trip (WP2.4 consumes `LeaseResult.holder{,_ttl_expires_at}`)."""
    url = os.environ.get("SWARMSYNC_URL", DEFAULT_SWARMSYNC_URL)
    if holder_ttl_expires_at is not None:
        remaining = max(0.0, holder_ttl_expires_at - time.time())
        ttl_note = f"~{remaining:.0f}s left on the current hold, "
    else:
        ttl_note = ""
    reason = (
        f"swarm-sync: {relpath} holds a write lease owned by {owner} "
        f"({ttl_note}but the hold renews while its holder is active, so it will not "
        f"lapse on its own). Pick different work; inspect current holders with "
        f"GET {url}/leases."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


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
        return _deny_response(relpath, owner, lease.get("ttl_expires_at"))

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

    # Lost the acquire race against another agent between our read above and this
    # acquire. WP2.4: the server's deny `LeaseResult` already carries `holder` and
    # `holder_ttl_expires_at`, so name the winner (and its TTL) straight from the
    # acquire response -- no second `GET /leases` round-trip. Fall back to a generic
    # phrase only if the server somehow reported no single holder (e.g. a race that
    # already resolved).
    owner = result.get("holder") or "another agent"
    return _deny_response(relpath, owner, result.get("holder_ttl_expires_at"))


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


def _agent_id(payload: Mapping[str, Any], err: TextIO = sys.stderr) -> str:
    """The coordination identity for this hook invocation (C2).

    Precedence, matching the VERIFIED Claude Code payload shapes:
      1. `agent_id` -- present and UNIQUE per subagent (Task-tool call). This is the
         identity that distinguishes the parallel subagents of one session, and it is
         what makes per-subagent leasing work on current Claude Code.
      2. `session_id` -- a main-thread payload has no `agent_id`; the whole session is
         one editor, so its session id is the right lease identity there. (All subagents
         of a session SHARE this id, which is exactly why `agent_id` must win above it --
         see the version-dependency note in ARCHITECTURE.md.)
      3. Neither present (a malformed/unrecognized payload): DO NOT collapse to a shared
         constant. The old `"main"` fallback silently gave any two such invocations the
         SAME lease identity, and for a lock shared identity means UNDER-protection -- two
         distinct agents treated as one holder, both allowed onto one file. Instead we
         mint a per-invocation-unique id and warn on stderr that coordination is degraded.
         The edit still isn't wrongly blocked (fail-open spirit), but two different agents
         can never be SILENTLY fused into one holder.
    """
    identity = payload.get("agent_id") or payload.get("session_id")
    if identity:
        return identity
    degraded = f"swarmsync-unidentified-{uuid.uuid4().hex}"
    err.write(
        "swarmsync-hook: payload has neither agent_id nor session_id; coordination "
        f"identity is DEGRADED -- using a per-invocation id ({degraded}) so two agents "
        "are never silently treated as one holder (this invocation is effectively "
        "uncoordinated: it will not keepalive or release a prior lease)\n"
    )
    return degraded


def _repo_root(payload: Mapping[str, Any]) -> Path:
    """The repo root to resolve parcel ids against (C12).

    Parcel ids are ROOT-relative on the server (indexer walks from the git toplevel), so
    the hook must key on that SAME root or it mints a divergent id for the same physical
    file. `payload["cwd"]` is wherever the session happens to be running -- if that is a
    SUBDIR of the repo, keying on it yields `subdir/a.py::<module>` where the server holds
    `a.py::<module>`, so `ensure_parcel=True` auto-creates a GHOST row and the one file
    ends up with TWO independent write leases -- the exact collision the lease prevents.

    So we walk UP from cwd to the git toplevel (the dir containing `.git`, a directory for
    a normal checkout or a file for a worktree/submodule) and resolve against THAT. With no
    `.git` anywhere up the tree we keep the old cwd-based behavior -- there is no root to
    discover, so cwd is the best (and unchanged) answer.
    """
    cwd = payload.get("cwd") or os.getcwd()
    start = Path(cwd).resolve()
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return start


# --- C10 two-tier fail policy: last-successful-contact marker ---------------------


def _record_contact(repo_root: Path) -> None:
    """Stamp `repo_root/.swarmsync-last-contact` with the current wall-clock time.

    Called after a subcommand talked to the blackboard WITHOUT error, so a later
    unreachable-blackboard failure can tell contention (recent stamp) from absence
    (no/stale stamp). Best-effort: a read-only tree or any write error is swallowed --
    a missing stamp only costs us the fail-CLOSED tier (we fall back to fail-open),
    which is the safe direction."""
    try:
        (repo_root / LAST_CONTACT_FILENAME).write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _recent_contact(repo_root: Path) -> bool:
    """True iff a successful blackboard contact was recorded within the recency window.

    A parse failure / missing file / stale timestamp all read as 'no recent contact'
    (fail-open direction)."""
    try:
        raw = (repo_root / LAST_CONTACT_FILENAME).read_text(encoding="utf-8")
        stamped = float(raw.strip())
    except (OSError, ValueError):
        return False
    return (time.time() - stamped) <= RECENT_CONTACT_WINDOW_SECONDS


def _maybe_fail_closed(
    subcommand: str, payload: Mapping[str, Any], err: TextIO
) -> Optional[dict]:
    """Decide whether an umbrella-caught failure should fail CLOSED instead of open (C10).

    Only `precheck` can gate an edit, so only it is ever a candidate. We fail closed
    (return a retry-deny) ONLY when ALL of these hold, else return None (fail open):
      - coordination is active (env/marker) for this payload's repo root, AND
      - a successful blackboard contact was recorded recently (server is real and was
        up moments ago -> this silence is contention, not a broken setup), AND
      - the payload actually names an in-repo edit target to gate.
    """
    if subcommand != "precheck":
        return None
    try:
        repo_root = _repo_root(payload)
        if not _is_active(os.environ, repo_root):
            return None
        if not _recent_contact(repo_root):
            return None
        tool_name = payload.get("tool_name")
        if tool_name not in EDIT_TOOLS:
            return None
        relpath = _relpath(_tool_file_path(payload.get("tool_input") or {}), repo_root)
        if relpath is None:
            return None
    except Exception:  # noqa: BLE001 -- the decision path itself must never brick a session
        return None
    err.write(
        f"swarmsync-hook: precheck: blackboard unreachable DURING active coordination "
        f"(recent contact on record) -- failing CLOSED for {relpath} rather than "
        "un-gating the shared tree under contention\n"
    )
    url = os.environ.get("SWARMSYNC_URL", DEFAULT_SWARMSYNC_URL)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"swarm-sync: coordination is active but the blackboard is momentarily "
                f"unreachable (contention, not absence) -- retry {relpath} in a moment. "
                f"If this persists, check the blackboard at {url}."
            ),
        }
    }


def _dispatch(
    subcommand: str,
    payload: Mapping[str, Any],
    http_factory: Optional[HttpFactory],
    out: TextIO,
    err: TextIO,
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
        agent_id = _agent_id(payload, err)
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
        # C10: reaching here means the blackboard answered without error, so record a
        # successful-contact stamp the two-tier fail policy can consult later.
        _record_contact(repo_root)
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

    payload: dict = {}
    try:
        raw = stdin.read()
        parsed = json.loads(raw) if raw.strip() else {}
        if isinstance(parsed, dict):
            payload = parsed
        return _dispatch(subcommand, payload, http_factory, out, err)
    except Exception as exc:  # noqa: BLE001 -- deliberate: see FAIL-OPEN above
        err.write(f"swarmsync-hook: {subcommand}: failing open ({exc!r})\n")
        # C10 two-tier: default is still open, but a precheck failure DURING active
        # coordination with recent successful contact fails CLOSED (contention, not a
        # dead setup) -- emit a retry-deny instead of silently un-gating. `payload` is
        # whatever we managed to parse ({} if parsing itself failed -> stays fail-open).
        deny = _maybe_fail_closed(subcommand, payload, err)
        if deny is not None:
            out.write(json.dumps(deny))
            out.write("\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
