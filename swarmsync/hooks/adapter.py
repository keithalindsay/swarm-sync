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
                the file syntactically invalid; or, WP3.5, the DELETED-tombstone
                sentinel hash + marker if the edit removed the file from disk --
                never a stale last-good hash for a file that is gone). Refreshes
                the lease TTL (S5). Never releases the lease here -- the agent
                keeps it until it stops (SubagentStop/Stop -> `release`).
  release       SubagentStop/Stop. Releases every active lease this agent_id
                holds (GET /leases, filter, POST /release each). The ONE
                subcommand exempt from the opt-in gate -- see `_UNGATED_RELEASE`
                in `_dispatch` and requirement 1's exception below.
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

   ONE EXCEPTION -- `release`. Every signal the gate consults is derived from a
   PATH (the env var, a marker at the repo root, a marker at the payload cwd),
   and a SubagentStop payload carries NO edit target, so `_repo_root` falls back
   to the session cwd -- which a Claude Code subagent inherits from its parent
   and which is routinely OUTSIDE the repo being coordinated. The gate is
   therefore not merely under-informed on this path, it is unanswerable: the
   finished agent's leases were never released, and every other agent stayed
   blocked on them until the 300s TTL expired. That is not fail-OPEN (letting
   work through), it is fail-STUCK (holding a lock), so it is the one place
   where the gate's silence does the harm the gate exists to prevent. `release`
   consequently runs regardless of the gate; it is safe to because it is the one
   subcommand that cannot act on a repo -- it deletes only rows whose agent_id
   is this very caller's (`cmd_release` filters, and `POST /release` refuses a
   foreign lease), so gating it could only ever cause a leak, never prevent one.
   The ungated path is quiet and stateless by construction: no stderr, no
   `.swarmsync-last-contact` stamp. `session-start` has the same payload shape
   and is deliberately NOT exempted: it WRITES (`POST /index` of a cwd-derived
   root guess), so ungating it would mint root-relative parcel ids into a
   blackboard whose repo never opted in -- and its miss costs nothing anyway,
   because `cmd_precheck` passes `ensure_parcel=True` and so leases a parcel
   that was never indexed.

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

from swarmsync import config
from swarmsync.agent.client import BlackboardClient
from swarmsync.blackboard.db import BUSY_TIMEOUT_SECONDS
from swarmsync.blackboard.models import (
    LEASE_TTL_FLOOR_SECONDS,
    LEASE_TTL_MAX_SECONDS,
)
from swarmsync.blackboard.parcel_id import module_id
from swarmsync.classifier.indexer import parse_file

# --- config ------------------------------------------------------------------

# WP4.2: the URL default (and every env read below) lives in `swarmsync.config`;
# this name survives as an alias for existing importers.
DEFAULT_SWARMSYNC_URL = config.DEFAULT_URL

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
    raw = config.lease_ttl()
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
    token = config.token()
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


def _is_active(
    env: Optional[Mapping[str, str]], repo_root: Path, cwd: Optional[Path] = None
) -> bool:
    """DESIGN-adjacent opt-in gate (see module docstring, requirement 1).

    `SWARMSYNC_ACTIVE=1` wins outright (e.g. CI/demo runs that always want
    enforcement without touching the filesystem); otherwise a
    `.swarmsync-active` marker file activates coordination. The marker is
    honored at EITHER the resolved repo root (the git toplevel, where parcel
    ids are keyed) OR the payload `cwd` (the Claude Code project dir, where
    `swarmsync-hook-guard` looks via $CLAUDE_PROJECT_DIR). Checking both closes
    an activation split: with a project dir that is a SUBDIR of a larger git
    repo, a marker at the project dir launched the guard but the adapter
    (checking only the toplevel) no-opped -- no single marker location could
    activate both halves.

    `env=None` (every real call site, WP4.2) reads the process environment via
    `config.active()`; an explicit mapping is the test seam for driving the gate
    with a synthetic environment.
    """
    env_active = (
        env.get(config.ACTIVE_ENV) == "1" if env is not None else config.active()
    )
    if env_active:
        return True
    if (repo_root / ACTIVE_MARKER_FILENAME).exists():
        return True
    return cwd is not None and (cwd / ACTIVE_MARKER_FILENAME).exists()


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
    id `indexer.parse_file` already emits for every file (built by
    `blackboard.parcel_id.module_id`, the id scheme's single home, so the
    two stay in lockstep). That is the safe default granularity named in
    this unit's brief: "whole-file parcel is the safe default granularity."
    """
    return module_id(relpath)


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
    precheck/postupdate while its holder stays active. The old "retry shortly" wording
    was removed because it sent agents into a busy-wait against a renewing lease; but
    the caveat deliberately stops short of "it will never lapse" -- a CRASHED holder
    stops renewing and its TTL does expire, so retrying after the remaining TTL is a
    legitimate strategy. Wording also avoids claiming the blocking lease's mode (the
    tie-broken blocker can be a reader; the acquire response does not carry its mode).
    The actionable pointer lets the agent inspect the live holder set instead: the
    `swarmsync holds` CLI (WP5.1 -- Bash-callable from the same shell the hook runs
    in), with `GET <url>/leases` named as the fallback for when the CLI isn't on PATH.
    All fields come from the acquire response already in hand -- no second round-trip
    (WP2.4 consumes `LeaseResult.holder{,_ttl_expires_at}`)."""
    url = config.url()
    if holder_ttl_expires_at is not None:
        remaining = max(0.0, holder_ttl_expires_at - time.time())
        ttl_note = f"~{remaining:.0f}s left on the current hold; "
    else:
        ttl_note = ""
    reason = (
        f"swarm-sync: {relpath} is leased by {owner} "
        f"({ttl_note}the hold renews while its holder stays active). Pick different "
        f"work -- run `swarmsync holds` to see who holds what (or GET {url}/leases)."
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

    # ONE acquire, no read-first. The acquire is the whole decision: the server
    # refreshes the caller's OWN active same-mode lease rather than inserting a
    # duplicate (`leases.acquire`, WP3.3 C1), so a repeat edit of a file this agent
    # already holds comes back granted and keeps its TTL alive across think time --
    # which is exactly what the old read-then-heartbeat branch was doing by hand.
    #
    # Reading `GET /leases` first and returning a deny from what it said made
    # hook-path denials INVISIBLE. A read emits nothing, so `swarmsync events` showed
    # 0 denials across a three-agent run that hit 8 of them, and "how much did my
    # swarm contend, and on which files" -- the first question anyone asks of a
    # coordination tool -- was unanswerable on the primary integration path. Losing
    # the acquire emits `lease_denied`; deciding not to attempt it emits nothing.
    #
    # It is also cheaper: one POST here against a GET plus a heartbeat POST before.
    #
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

    # WP2.4: the server's deny `LeaseResult` already carries `holder` and
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


# WP3.5 (C6-interim): the sentinel content_hash a deleted file's tombstone carries.
# `sha256(b"deleted")` -- a CONSTANT, like the DIRTY path's raw-byte hash it is
# shaped after, chosen over hashing empty bytes because sha256(b"") is a famous
# value real empty files legitimately hash to; this one can never collide with any
# genuine on-disk content (parcel hashes are digests of bytes that exist) and is
# trivially recognizable/greppable. Deterministic by design, matching the module's
# "same input -> identical row" rule for summaries.
DELETED_SENTINEL_HASH = hashlib.sha256(b"deleted").hexdigest()


def _deleted_summary(relpath: str, agent_id: str) -> str:
    """DETERMINISTIC tombstone marker for a file the edit removed from disk,
    naming the deleting agent (same shape as `_dirty_summary`)."""
    return f"swarm-sync hook: {relpath} DELETED by {agent_id}"


def cmd_postupdate(
    tool_name: Optional[str],
    tool_input: Mapping[str, Any],
    client: BlackboardClient,
    repo_root: Path,
    agent_id: str,
    err: TextIO = sys.stderr,
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

    WP3.5 (C6-interim): if the edit DELETED the file outright, the same logic
    applies -- the old early return left the blackboard advertising the last-good
    hash of a file that is gone. Now a tombstone is posted instead: the constant
    `DELETED_SENTINEL_HASH` + a state_summary with a DELETED marker naming this
    agent. Since WP1.4 `/parcel/update` requires the caller's write lease; the
    deleting agent holds it from its own precheck, so the tombstone lands. If the
    lease lapsed (edge), the refusal stays FAIL-OPEN: a one-line stderr note, no
    crash, no deny. Note the tombstone matters most on THIS hook path, where no
    integrator ever runs: on the broker path the integrator's authoritative
    post-land re-index (WP3.5 task A) retires the parcel row itself and
    supersedes any tombstone.
    """
    if tool_name not in EDIT_TOOLS:
        return
    relpath = _relpath(_tool_file_path(tool_input), repo_root)
    if relpath is None:
        return
    abs_path = repo_root / relpath
    parcel_id = _parcel_id(relpath)

    # KEEPALIVE (S5): refresh this agent's lease on the edited parcel before
    # anything that could raise, so a long edit doesn't let the lease expire.
    # Runs for the deleted-file tombstone path too -- the parcel row (and this
    # agent's lease on it) outlives the file until an integrator retires it.
    lease = _find_lease(client.leases(), parcel_id)
    if lease is not None and lease.get("agent_id") == agent_id:
        _keepalive(client, agent_id, lease)

    if not abs_path.exists():
        # The edit deleted the file. Post an honest tombstone instead of the old
        # early return that kept advertising a stale hash for a gone file.
        # A refusal is FAIL-OPEN either way it arrives: the server reports a
        # missing lease as a soft 200 `{"ok": false, "reason": ...}` (checked
        # below), while transport/4xx failures raise (caught below) -- both end
        # in a stderr note, never a crash or a deny.
        try:
            result = client.parcel_update(
                agent_id,
                parcel_id,
                DELETED_SENTINEL_HASH,
                _deleted_summary(relpath, agent_id),
            )
            if not result.get("ok", True):
                err.write(
                    f"swarmsync-hook: postupdate: DELETED tombstone for {relpath} was "
                    f"refused ({result.get('reason')!r}); failing open -- the "
                    "integrator's re-index is the authoritative backstop\n"
                )
        except Exception as exc:  # noqa: BLE001 -- fail-open by contract (see docstring)
            err.write(
                f"swarmsync-hook: postupdate: DELETED tombstone for {relpath} "
                f"failed ({exc!r}); failing open -- the integrator's re-index "
                "is the authoritative backstop\n"
            )
        return

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

    So we walk UP to the git toplevel (the dir containing `.git`, a directory for a normal
    checkout or a file for a worktree/submodule) and resolve against THAT.

    **We walk up from the EDIT TARGET, not from cwd.** Keying on cwd assumes the session
    is running inside the repo it is editing, and a Claude Code subagent inherits its
    parent session's cwd -- which is routinely a workspace root, a parent directory, or an
    entirely different project. When cwd sits outside the coordinated repo, walking up from
    it finds either no `.git` at all or the WRONG repo's toplevel; `_is_active` then finds
    no marker there and `_dispatch` returns a silent, zero-network-call ALLOW. Every edit
    goes through ungated, with a live lease held by another agent, and nothing says so:
    stderr stays empty and `swarmsync doctor` reports every check green, because from the
    server's side nothing IS misconfigured.

    Measured before the fix, holding the file, the lease and everything else constant and
    varying only cwd:

        process=repo     payload=repo     -> DENY  (correct)
        process=outside  payload=repo     -> DENY  (correct)
        process=repo     payload=outside  -> silent ALLOW
        process=outside  payload=outside  -> silent ALLOW

    The payload `cwd` was the sole determinant; the process cwd never mattered. Three
    concurrent agents then edited a coordinated repo with 0 leases and 0 events recorded.

    The edit target is the only input that actually identifies the tree being mutated, so
    it is the one to key on. `scripts/swarmsync-hook-guard` already walks up from the
    target for its marker check; this makes the adapter agree with the guard that launched
    it. cwd remains the fallback for payloads with no edit target at all (`release`,
    `session-start`), and for a target outside any checkout -- there is no root to
    discover there, so cwd is the best (and unchanged) answer.
    """
    target = _tool_file_path(payload.get("tool_input") or {})
    if target:
        target_root = _git_toplevel(Path(target).resolve().parent)
        if target_root is not None:
            return target_root

    cwd = payload.get("cwd") or os.getcwd()
    start = Path(cwd).resolve()
    return _git_toplevel(start) or start


def _git_toplevel(start: Path) -> Optional[Path]:
    """The nearest enclosing dir containing `.git`, or None.

    `.git` is a directory in a normal checkout and a FILE in a worktree or submodule, so
    this tests existence rather than is_dir -- swarm-sync runs agents in worktrees, where
    an is_dir check would silently fail to find the root it created.
    """
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


# --- C10 two-tier fail policy: last-successful-contact marker ---------------------


class _ContactRecordingHttp:
    """Wraps the http object so `_dispatch` gets POSITIVE evidence of blackboard
    contact: `contacted` flips True only when a get/post RETURNED (any status --
    even a 500 proves the server is alive, which is the question the two-tier
    fail policy asks). A call that raises propagates unchanged and never counts.

    This exists because "the subcommand finished without raising" is NOT contact:
    precheck/postupdate with an out-of-repo target, a non-edit tool_name, or an
    unrecognized subcommand all complete without a single network call, and an
    unconditional post-dispatch stamp on those paths flipped a NEVER-STARTED
    server into fail-closed denials (a scratchpad Write would stamp, then the
    next in-repo Edit found a "recent contact" and denied) -- the exact broken
    setup the policy promises stays fail-open, and the perpetual re-stamping
    also defeated the 60s self-heal after a deliberate server stop."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.contacted = False

    def get(self, url: str, **kwargs: Any) -> Any:
        response = self._inner.get(url, **kwargs)
        self.contacted = True
        return response

    def post(self, url: str, **kwargs: Any) -> Any:
        response = self._inner.post(url, **kwargs)
        self.contacted = True
        return response


def _record_contact(repo_root: Path) -> None:
    """Stamp `repo_root/.swarmsync-last-contact` with the current wall-clock time.

    Called ONLY after the blackboard positively answered an HTTP call this
    invocation (see `_ContactRecordingHttp`), so a later unreachable-blackboard
    failure can tell contention (recent stamp) from absence (no/stale stamp).
    Best-effort: a read-only tree or any write error is swallowed -- a missing
    stamp only costs us the fail-CLOSED tier (we fall back to fail-open),
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

    Scope note: this tier engages on ANY exception the umbrella caught during an
    actively-coordinating precheck -- unreachability, a timeout, a 5xx, or a bug in
    the precheck path itself. That widening is deliberate: once a recent POSITIVE
    contact is on record (see `_ContactRecordingHttp` -- the stamp is written only
    when the server actually answered), an error mid-coordination means the gate
    could not be consulted, and un-gating the shared tree is the worse failure.
    """
    if subcommand != "precheck":
        return None
    try:
        repo_root = _repo_root(payload)
        payload_cwd = Path(payload.get("cwd") or os.getcwd()).resolve()
        if not _is_active(None, repo_root, payload_cwd):
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
    url = config.url()
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
    payload_cwd = Path(payload.get("cwd") or os.getcwd()).resolve()

    # Requirement 1 (OPT-IN): checked before ANY blackboard I/O. Inactive means a
    # silent, zero-network-call ALLOW -- for every subcommand EXCEPT `release`, which
    # is deliberately ungated (see `_UNGATED_RELEASE`).
    active = _is_active(None, repo_root, payload_cwd)
    if not active and subcommand != "release":
        return 0

    base_url = config.url()
    owns_http = http_factory is None
    factory: HttpFactory = http_factory or _default_http_factory
    http = factory(base_url)
    try:
        recorder = _ContactRecordingHttp(http)
        client = BlackboardClient(recorder)

        if not active:
            # `_UNGATED_RELEASE` -- a SubagentStop whose repo we cannot identify.
            # Deliberately quiet and stateless: NO degraded-identity warning (a
            # payload with no identity holds no lease, so there is nothing to
            # release and no reason to speak), NO `.swarmsync-last-contact` stamp
            # (that file belongs to a repo that opted in, and `repo_root` here is a
            # cwd-derived guess we already know is the wrong question), and NO
            # stderr note when the blackboard is simply absent -- which is the
            # common case for anyone who wired these hooks globally and never ran a
            # server. Identity precedence mirrors `_agent_id`, minus its warning.
            identity = payload.get("agent_id") or payload.get("session_id")
            if identity:
                try:
                    cmd_release(client, identity)
                except Exception:  # noqa: BLE001 -- best-effort by construction
                    pass
            return 0

        agent_id = _agent_id(payload, err)
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input") or {}

        if subcommand == "precheck":
            decision = cmd_precheck(tool_name, tool_input, client, repo_root, agent_id)
            if decision is not None:
                out.write(json.dumps(decision))
                out.write("\n")
        elif subcommand == "postupdate":
            cmd_postupdate(tool_name, tool_input, client, repo_root, agent_id, err=err)
        elif subcommand == "release":
            cmd_release(client, agent_id)
        elif subcommand == "session-start":
            cmd_session_start(recorder, repo_root)
        # else: unrecognized subcommand -> no-op ALLOW, same as an inactive repo.
        # C10: stamp ONLY on positive contact. Merely "reaching here" is not evidence
        # the blackboard answered -- several paths above complete with zero network
        # calls (out-of-repo target, non-edit tool, unrecognized subcommand), and
        # stamping on those flipped never-started setups into fail-closed denials.
        if recorder.contacted:
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
