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
