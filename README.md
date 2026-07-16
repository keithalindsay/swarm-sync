# swarm-sync

**Stigmergy for AI coding swarms.** A shared, live memory that lets many AI coding agents edit the
*same* codebase at once without colliding.

> Multiple AI agents on one repo collide — same files, merge conflicts, broken assumptions, duplicated
> work. swarm-sync analyzes and classifies the codebase into safely-parallel **parcels**, hands each
> agent an exclusive **lease** and its own **git worktree**, and keeps everyone in sync through a shared
> **blackboard** — a live SQLite representation of the current state of the code. Coordination is
> *stigmergic*: agents read and write the environment, never each other.

This is the **Pheromesh** architecture. See [`DESIGN.md`](DESIGN.md) for the full spec and
[`BUILD_PLAN.md`](BUILD_PLAN.md) for the ordered build units.

## How it works (one paragraph)

A **classifier** parses the repo (Python via stdlib `ast`) into *parcels* — leasable units at
function/method granularity with file-level fallback — computes each parcel's **blast radius**, and
freezes high-fan-in **interface contracts**. A **blackboard** (SQLite in WAL mode) holds the parcel map,
leases, contracts, decaying **pheromone** trails, and an append-only event log; each parcel carries a
live `state_summary` of what it now does. Agents declare intent, acquire an **atomic CAS write-lease**,
edit inside their **own git worktree**, heartbeat, then submit their branch to a **serial, test-gated
integrator** that merges (conflict-free by construction, since only disjoint work runs concurrently),
runs pytest, and re-indexes. Crashes are reclaimed by a **TTL reaper**; trunk is never poisoned because
merges are gated.

## Collision handling at a glance

| Layer | Mechanism | Guarantees |
|---|---|---|
| Physical | git worktree per agent | same-file clobbering is structurally impossible |
| Logical | atomic SQLite CAS lease per parcel | one writer per parcel; mispredictions serialize |
| Interface | frozen contracts + `contract_change` events | no silent signature breaks under dependents |
| Integration | serial pytest-gated merge | semantic breaks caught before landing; trunk stays green |

## Quickstart (once built)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run the full 5-money-shot demo (boots server, indexes sample_repo, runs 3 agents)
python demo/run_demo.py

# Or run the server standalone
swarm-sync            # uvicorn on :8000
pytest                # test suite
```

## Use with Claude Code (hook-enforced coordination)

The other way to run swarm-sync (besides the scripted demo/broker) is to let Claude Code's own
hooks enforce leasing **transparently**, so you don't wire agents in by hand: every `Edit`/`Write`
a (sub)agent makes is gated by a real-time lease check, and its lease is released automatically
when the agent stops.

### 1. Wire the hooks in `settings.json`

Add this to `~/.claude/settings.json` (global) or `<project>/.claude/settings.json`
(project-scoped), pointing the `command` paths at your actual swarm-sync checkout:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [
          { "type": "command", "command": "/path/to/swarm-sync/scripts/swarmsync-hook-guard precheck", "timeout": 10 }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [
          { "type": "command", "command": "/path/to/swarm-sync/scripts/swarmsync-hook-guard postupdate", "timeout": 10 }
        ]
      }
    ],
    "SubagentStop": [
      {
        "hooks": [
          { "type": "command", "command": "/path/to/swarm-sync/scripts/swarmsync-hook-guard release", "timeout": 10 }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "/path/to/swarm-sync/scripts/swarmsync-hook-guard session-start", "timeout": 15 }
        ]
      }
    ]
  }
}
```

Wire `scripts/swarmsync-hook-guard`, **not** `swarmsync-hook` directly. The guard is the
zero-overhead opt-in shim in front of the real adapter: when swarm-sync isn't activated (see
below) it exits `0` immediately without ever starting Python, so normal, non-coordinated editing
pays no cost. It only `exec`s the real `swarmsync-hook <subcommand>` adapter when a session is
active.

### 2. Start the blackboard server

```bash
swarmsync-serve --db /tmp/swarmsync.db --port 8787
```

Leave it running for the duration of the coordinated session — it's the shared blackboard every
hook call and agent talks to.

### 3. Turn coordination on/off per repo

Enforcement is **opt-in** and **fail-open**, controlled by:

- **`.swarmsync-active`** — a marker file at the repo root. Its mere presence activates the
  hooks for that repo; `touch .swarmsync-active` to turn on, `rm .swarmsync-active` to turn off.
- **`SWARMSYNC_ACTIVE=1`** — an env-var alternative to the marker file (wins outright if set,
  e.g. for CI/demo runs that should always be active regardless of what's on disk).
- **`SWARMSYNC_URL`** — the blackboard base URL the hook adapter talks to (default
  `http://127.0.0.1:8787`, i.e. it assumes you started the server with `swarmsync-serve` per
  step 2 — see the port-mismatch warning below if you didn't).

When neither `.swarmsync-active` nor `SWARMSYNC_ACTIVE` is set, or the blackboard is unreachable,
the guard/adapter is a no-op and every edit is allowed — swarm-sync never blocks normal,
uncoordinated work.

### Two launchers, two different default ports — don't mix them up

swarm-sync ships **two** ways to boot the same FastAPI blackboard, with **different default
ports**:

| Launcher | Entry point | Default port |
|---|---|---|
| `swarm-sync` | `swarmsync.server.app:main` | **8000** |
| `swarmsync-serve` | `swarmsync.server.serve:main` | **8787** |

The Claude Code hook adapter's own default `SWARMSYNC_URL` is `http://127.0.0.1:8787` — it
assumes the server was started with **`swarmsync-serve`**, not plain `swarm-sync`. This is a real
footgun: if you boot the server with `swarm-sync` (port 8000, its own default) for a hook-enforced
session and don't also set `SWARMSYNC_URL`, the adapter will try (and fail) to reach `:8787`, and
because it's **fail-open by construction**, that failure is silent — every edit is simply allowed
through with **no leasing whatsoever**, no error, no denial, nothing in the transcript to flag it.
Always either run `swarmsync-serve --port 8787` for hook-enforced sessions, or explicitly export
`SWARMSYNC_URL` to match whichever launcher/port you actually used.

### Granularity note: money-shot #1 vs. the hook's default

`DESIGN.md` §2's de-risking decision is that the **enforced** lease granularity defaults to
**whole-file**, and that's exactly what `swarmsync/hooks/adapter.py` does for every Claude-Code-hook
session — it always leases the one synthetic per-file parcel, never a symbol inside it, regardless
of how finely the classifier parsed the file. **Money-shot #1** (two agents editing different
functions in the same file, both landing clean) is demonstrated by `demo/run_demo.py` using the
**broker** (`coordinator/broker.py`) in `mode="symbol"` — an opt-in, finer-grained leasing mode the
demo/broker path supports but the hook path does not. Under hook enforcement, two subagents editing
different functions in the *same file* at the same time will **not** both proceed: the second is
denied the whole-file lease and must pick different work or wait, rather than co-editing the file
the way the demo's symbol-mode broker run shows.

## The demo proves

1. Two agents edit **different functions in the same file** concurrently → both land clean.
2. A third agent hitting a leased parcel is **serialized** (denied → waits → lands).
3. A **frozen-contract change** notifies a dependent, which re-plans.
4. An agent **killed mid-edit** is reaped; its task is reassigned; trunk untouched.
5. A test-breaking edit is **rejected** at the gate and never lands.

Success criterion: **zero same-file textual collisions reach the integration branch**, every landed
commit leaves the sample repo's tests green, all five assertions print PASS.

## Status

Prototype / overnight build. Scope intentionally tight: Python target, deterministic scripted agents
(real Claude Agent SDK worker is a drop-in), serial integrator, no TUI. See `DESIGN.md` §8 for what's
deliberately out of scope.

## License

MIT © 2026 Keith Lindsay
