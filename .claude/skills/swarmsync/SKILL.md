---
name: swarmsync
description: >-
  Coordinate multiple AI agents/subagents editing the SAME codebase at once without
  collisions, using swarm-sync (Pheromesh) — a lease fabric enforced transparently by
  Claude Code hooks. Use when running parallel/multi-agent edits on one repo, when an
  edit gets denied with a "swarm-sync: ... is leased by ..." message, or when setting up
  or operating a coordinated agent session.
---

# swarm-sync coordination

swarm-sync lets several agents edit one repository concurrently without stepping on each
other. Agents lease "parcels" of code (whole files by default) on a shared SQLite
**blackboard** before editing; a serial, test-gated integrator lands work; collisions are
prevented, not merged. **Claude Code hooks enforce this automatically** — you don't wire
each agent in by hand.

> **Paths.** `<swarm-sync>` below is wherever this repo is checked out. The `swarmsync*`
> console scripts (`swarmsync`, `swarmsync-serve`, `swarmsync-hook`) are on `PATH` inside the
> project's activated venv (`pip install -e .` provides them), so the commands are shown by
> name — no absolute paths.

## When it's active

Enforcement is **opt-in** and **fail-open**:
- It only engages when `SWARMSYNC_ACTIVE=1` is set OR a `.swarmsync-active` marker file
  exists at the repo root. Otherwise every edit is allowed and the system is dormant.
- If the blackboard is unreachable or errors, edits are **allowed** (it never blocks your
  normal work). It only ever denies on a real, confirmed lease conflict held by *another*
  agent.

## The protocol (what an agent does)

You mostly don't do anything — the hooks handle it. But know the rules:

1. **Editing is gated automatically.** Before each `Edit`/`Write`, a `PreToolUse` hook
   acquires a lease on the target file's parcel for your `agent_id`. Re-editing a file you
   already hold is fine.
2. **If you see `swarm-sync: <file> is leased by <agent> …`** — another agent owns that
   parcel. **Do not fight it**: do not loop-retry aggressively or try to route around the
   lock. Run `swarmsync holds` to see who holds what, then pick a *different* file/task in
   your scope (`swarmsync free <paths…>` tells you which candidates are open). This is the
   system working as intended.
3. **Your leases release automatically** when you stop (`SubagentStop`). No manual cleanup.
4. **Prefer disjoint work assigned up front** over discovering conflicts at edit time — if
   you're orchestrating subagents, give each a non-overlapping set of files.

## Operating a coordinated session (setup)

1. Start the blackboard server (leave it running), pointed at the repo you're coordinating:
   `swarmsync-serve --root /path/to/your/repo --db /tmp/swarmsync.db --port 8787`
2. Wire the hooks + activate, in one command from the repo: `swarmsync init-hooks`
   (writes the `settings.json` hook block and drops the `.swarmsync-active` marker;
   `--dry-run` previews, `--global` targets `~/.claude`).
3. Confirm the whole setup: `swarmsync doctor` — it checks server reachability, that the
   server's managed root is this repo, the marker, the wired hooks, DB writability, and the
   version, printing a fix for anything that fails.
4. Spawn your agents/subagents. Give each a distinct file scope. The hooks coordinate them.

**To turn it off:** `rm .swarmsync-active` (or `unset SWARMSYNC_ACTIVE`). The hooks go dormant.

## Watching / steering a running session

All read-only, over the same HTTP the hooks use (default `$SWARMSYNC_URL`):

- `swarmsync status` — is the server up, bound to which repo, how busy (active holds + recent events)?
- `swarmsync holds` — every active hold: parcel, holder, mode, TTL-remaining.
- `swarmsync free <paths…>` — which of these paths are free to take? Exits non-zero if any are
  held, so an agent can gate work: `swarmsync free foo.py && <edit foo.py>`.
- `swarmsync events [--follow]` — recent events, optionally tailed.

## Two ways to run a multi-agent job

- **Hook-enforced (simplest):** spawn subagents normally; the hooks lease/deny/release for
  them. Best with worktree isolation so each agent has a distinct `agent_id`/branch.
- **Broker-orchestrated:** use swarm-sync's broker (`<swarm-sync>/swarmsync/coordinator/broker.py`)
  to partition tasks into disjoint, co-schedulable waves and dispatch worktree agents — this
  avoids contention by construction rather than resolving it at edit time.

## Commands

- `swarmsync {status|holds|free|events|doctor|init-hooks}` — the operator/agent CLI (above)
- `swarmsync-serve --root <repo> --db <path> --port <n>` — start the blackboard server
- `swarmsync-hook {precheck|postupdate|release|session-start}` — reads a Claude Code hook
  event on stdin; normally invoked *by* hooks (via `scripts/swarmsync-hook-guard`), rarely by hand
- `python <swarm-sync>/demo/run_demo.py` — the reference 5-scenario demo

## Reference

Architecture and internals: `<swarm-sync>/DESIGN.md` and `<swarm-sync>/ARCHITECTURE.md`. The
lease/collision model, the classifier, the integrator, and the crash-recovery reaper are all
documented there; the server also serves interactive Swagger docs at `GET <SWARMSYNC_URL>/docs`.
