# Architecture — how swarm-sync works

This is the doc to read if you want to **understand or improve** swarm-sync, not just run it. The
[README](README.md) is the usage guide; [`DESIGN.md`](DESIGN.md) is the exhaustive build spec with
every caveat and edge case spelled out. This doc sits between them: the mental model in plain
language, a walkthrough of one edit end to end, a map from each concept to the code that implements
it, and an honest list of where the interesting problems still live.

New to the vocabulary (parcel, lease, blackboard, pheromone)? The README's
[glossary](README.md#the-words-this-readme-uses) defines every term in one line each. This doc
assumes you've skimmed it.

---

## The one idea

Many AI coding agents editing one repo collide: same files, merge conflicts, broken assumptions,
duplicated work. swarm-sync stops that with a rule and a gate:

- **A rule:** before an agent may edit a file, it must hold an exclusive **lease** on that file. One
  writer per file; everyone else waits or works elsewhere.
- **A gate:** every finished change passes through a single **integrator** that merges it, runs the
  tests, and rolls the merge back if they fail — so the main branch is never broken.

Everything else is machinery that makes those two things safe, fast, and crash-proof.

The coordination is **stigmergic** — the ant-colony trick. Ants don't hold meetings; they leave
trails in the environment and read the environment to decide what to do next. swarm-sync's agents
**never message each other**. They read and write one shared memory — the **blackboard** — and that
is the only channel between them. The architecture's name, **Pheromesh**, is just this idea:
*pheromone* (decaying trails in a shared medium) + *mesh* (agents around one live memory).

```
   agent A ─┐
   agent B ─┼──►  BLACKBOARD  ──►  INTEGRATOR  ──►  trunk (always green)
   agent C ─┘   (SQLite: parcels,   (serial merge +
                 leases, events,      pytest gate,
                 pheromone)           rollback on red)
        ▲            │
        └──── read ──┘   agents coordinate ONLY through the blackboard, never directly
```

---

## The core mechanism: the lease (the lock)

This is the load-bearing piece, and it's simpler than the name "atomic CAS lease" suggests.

**A lease is a temporary, exclusive claim on a file.** "Agent A holds the lease on `payments.py`"
means A is the only one allowed to edit that file right now. The claims all live in one table in the
blackboard (SQLite). That's it — the "lock" is a row in a database.

**Why it can't be a naïve check-then-write.** The obvious way to grab a lock is: (1) check if anyone
holds the file, (2) if not, write down that you do. The bug is the gap between 1 and 2 — two agents
can both check at the same instant, both see "free," and both claim it. That race is the whole
problem a lock has to solve.

**How swarm-sync closes the gap — compare-and-swap (CAS).** The check and the write are fused into a
*single* SQL statement: *insert a claim for agent A **only if** no active, unexpired claim on this
file already exists.* SQLite runs a single statement atomically, so when two agents race, the
database serializes them: the first insert succeeds, the second's `only if none exists` is now false
and inserts nothing. One winner, one loser (`rowcount == 1` vs `0`), no gap. The loser is told
"denied — that file's taken" and picks other work or waits. The safety comes from leaning on the
database's own atomicity guarantee instead of hand-rolling a lock. See
[`server/leases.py`](swarmsync/server/leases.py) — the `acquire()` docstring and SQL are the
canonical reference.

**Read vs. write.** Many agents can hold a *read* lease on one file at once (reading doesn't
collide). A *write* lease conflicts with everything: if anyone is reading or writing, a new writer is
denied. One writer, many readers.

**Why leases expire (TTL, heartbeat, reaper).** An AI agent can crash or hang mid-edit. A permanent
lock would leave that file hostage forever. So every lease has a **time-to-live**; the holder must
**heartbeat** ("still alive, still working") every few seconds to keep it fresh. Stop heartbeating
and the claim goes stale — the CAS predicate treats an expired lease as not blocking, so the next
agent can take the file immediately (lazy expiry). A background **reaper**
([`coordinator/reaper.py`](swarmsync/coordinator/reaper.py)) also sweeps expired leases, marks them
`reaped`, and lets the work be reassigned. The dead agent's half-finished edit lived only in its
private worktree and never reached trunk, so nothing is corrupted — it's discarded and redone.

**The deliberate simplification: the lock is a whole file.** Even though the classifier parses the
repo down to individual functions, **every lease is taken on the whole-file parcel**. Two agents
never edit the same file at once; parallelism comes from working on *different* files. Per-function
locking was designed and then **parked on purpose** — it's unsafe with today's lease store, which
compares parcel ids by string equality and has no notion of one parcel (a function) living *inside*
another (its file), so it would hand out both claims and let two agents collide. File-level locking
is safe *by construction*: every id is the same shape, so string equality is exactly what you want.
The full reasoning and a staged revival plan are in
[`SYMBOL_MODE_DESIGN.md`](SYMBOL_MODE_DESIGN.md).

---

## The life of one edit

End to end, what happens when an agent changes some code (the broker-driven path):

1. **Classify.** The classifier parses the repo into parcels, computes each parcel's *blast radius*
   (how much breaks if it changes), and freezes high-fan-in signatures as *contracts*. Output goes
   into the blackboard. → `classifier/`
2. **Read the world.** The agent reads the blackboard: the parcel map, each parcel's `state_summary`
   (what the code does now), and the recent event stream (who's active where). No agent-to-agent
   messages. → `GET /parcels`, `GET /events`
3. **Declare intent.** The agent posts which parcels it means to touch — a `planned` pheromone that
   lets others avoid duplicating the work. → `POST /intent`
4. **Acquire the lease (CAS).** The agent requests a write lease on its target file. Granted → it
   owns the file. Denied → it backs off or picks another task. → `POST /lease`,
   `server/leases.py::acquire`
5. **Edit in isolation.** The agent works in **its own git worktree** — a private checkout — so two
   agents can't even physically touch the same file on disk. It heartbeats every few seconds to keep
   the lease alive. → `worktree/git_ops.py`, `agent/runner.py`
6. **Submit to the gate.** On done, the agent posts its new content hash + summary and submits its
   branch to the **integrator**. → `POST /parcel/update`, `POST /integrate`
7. **Gated merge.** The integrator, running one branch at a time, merges the branch, runs the tests,
   and: green → land it, re-index the touched files, regenerate their `state_summary`, emit `merged`;
   red → reject, roll trunk back, bounce the branch to its agent with the logs. Trunk is never left
   broken. → `coordinator/integrator.py`
8. **Release.** The lease is released; the file is immediately acquirable again.

If the agent dies anywhere in 4–6, the reaper reclaims the lease after the TTL and the task is
reassigned. Because nothing reaches trunk except through step 7, a crash can never poison the main
branch.

---

## Where each piece lives in the code

| Concept | Code | What it does |
|---|---|---|
| **Classifier** | [`classifier/indexer.py`](swarmsync/classifier/indexer.py) | Parses the repo (Python stdlib `ast`) into parcels — one per function/method/class, plus a synthetic whole-file parcel. |
| ↳ dep graph / blast radius / contracts | [`classifier/graph.py`](swarmsync/classifier/graph.py) | Builds the import+call graph, computes reverse-dependency blast radius, extracts frozen contracts, and holds `check_file_granularity` / `co_schedulable`. |
| ↳ write to blackboard | [`classifier/store.py`](swarmsync/classifier/store.py) | `run_index` — populates `parcels` + `contracts` (this is `POST /index`). |
| **Blackboard** (shared memory) | [`blackboard/db.py`](swarmsync/blackboard/db.py), [`schema.sql`](swarmsync/blackboard/schema.sql) | The single SQLite-WAL database and its schema: `parcels`, `leases`, `contracts`, `pheromone`, `intents`, `events`. |
| ↳ typed rows | [`blackboard/models.py`](swarmsync/blackboard/models.py) | Pydantic models every reader validates through (`Parcel`, `LeaseMode`, `LeaseResult`, …). |
| **Lease** (the lock) | [`server/leases.py`](swarmsync/server/leases.py) | `acquire` (atomic CAS), `heartbeat`, `release`, `_ensure_parcel`. The mutual-exclusion primitive. |
| **Pheromone trail / event log** | [`server/events.py`](swarmsync/server/events.py) | `emit` — the single write path into the append-only `events` table, which doubles as the audit log and the recovery source of truth. |
| **HTTP API** | [`server/app.py`](swarmsync/server/app.py) | FastAPI wiring every endpoint; `check_single_root` enforces the one-managed-root rule. |
| ↳ launcher | [`server/serve.py`](swarmsync/server/serve.py) | `swarmsync-serve` — starts the blackboard server. |
| **Broker** (scheduler) | [`coordinator/broker.py`](swarmsync/coordinator/broker.py) | Matches tasks to parcels, spawns agents in file-disjoint waves, reassigns on reap (`resolve_task`, `_run_task_once`). |
| **Integrator** (the gate) | [`coordinator/integrator.py`](swarmsync/coordinator/integrator.py) | Serial, pytest-gated merge with rollback-on-red, post-merge re-index, and orphan recovery (`reconcile_orphaned_integrations`). |
| **Reaper** | [`coordinator/reaper.py`](swarmsync/coordinator/reaper.py) | Expires the leases of crashed agents past TTL and decays pheromone. |
| **Worktree isolation** | [`worktree/git_ops.py`](swarmsync/worktree/git_ops.py) | Per-agent git worktree lifecycle — physical "two writers, one file" impossibility. |
| **Agent** | [`agent/runner.py`](swarmsync/agent/runner.py), [`agent/client.py`](swarmsync/agent/client.py) | The full sync-protocol lifecycle in a worktree; `client.py` is the thin, swappable interface a real Claude Agent SDK worker drops into. |
| ↳ demo stand-in | [`agent/mutators.py`](swarmsync/agent/mutators.py) | Deterministic scripted edits used in place of a live LLM so the demo/tests are reproducible. |
| **Claude Code hooks** | [`hooks/adapter.py`](swarmsync/hooks/adapter.py), [`scripts/swarmsync-hook-guard`](scripts/swarmsync-hook-guard) | The transparent enforcement path: gates every `Edit`/`Write` a Claude subagent makes. The guard is a zero-overhead shim when coordination is off. |
| **Demo** | [`demo/run_demo.py`](demo/run_demo.py) | Boots everything and runs the five "test case" scenarios end to end. |

---

## Two paths, and why the difference matters

swarm-sync coordinates agents two ways, and a contributor must not confuse them:

- **Broker path** (the demo, the test suite, embedding in your own orchestrator): each agent gets its
  own **git worktree** *and* a lease *and* passes through the integrator. Physical isolation makes
  same-file clobbering structurally impossible; the lease and the gate are additional layers.
- **Claude Code hook path** (`hooks/adapter.py`): the subagents all share the **one working tree** the
  user's session is in. There is **no worktree isolation and no integrator** here — the lease is the
  *only* thing preventing a collision. Two consequences, both load-bearing: (1) any file the lease
  layer fails to cover is completely ungated, which is why a hook lease on an unindexed file
  auto-creates a coarse whole-file parcel rather than failing open; (2) the lease is consulted only
  for `Edit`/`Write`-family tools, so a `Bash` write (`sed -i`, `cat >`) bypasses it entirely. The
  hook path is a cooperative protocol among well-behaved agents, not a sandbox.

### Hook coordination identity has a Claude Code version floor

The hook derives each edit's lease identity from the payload (`hooks/adapter.py::_agent_id`): a
subagent's PreToolUse/PostToolUse/SubagentStop payload carries a **unique `agent_id`**, and that is
what lets swarm-sync tell the parallel subagents of one session apart and give each its own leases.
All subagents of a session **share the parent `session_id`** — so `agent_id` is the *only* field that
distinguishes them, which is why it takes precedence over `session_id`. A main-thread payload has no
`agent_id` and correctly leases under its `session_id` (the whole session is one editor there).

This is an **honest version dependency, not a bug**: per-subagent coordination requires a Claude Code
version whose hook payloads include `agent_id`. On an older version whose payloads omit it, every
subagent of a session falls back to the shared `session_id` and the fabric cannot distinguish them —
they collapse to one holder and effectively coordinate at session granularity. A payload lacking
*both* fields (malformed/unrecognized) does **not** collapse to a shared constant: the adapter mints a
per-invocation-unique id and warns on stderr, so two different agents are never *silently* fused into
one lease holder (fail-open in effect, never false-sharing).

---

## Good places to contribute (where the real problems are)

These are documented honestly rather than hidden — each is a genuine limitation with a known shape,
which makes them the best entry points for improving the project.

- **Symbol-granularity leasing** — parked, not merely missing. The payoff and the full staged revival
  plan are in [`SYMBOL_MODE_DESIGN.md`](SYMBOL_MODE_DESIGN.md). The blocker: the lease conflict rule
  in `server/leases.py::acquire` is a string match with no containment awareness. Teaching it that
  `m.py::alpha` lives inside `m.py::<module>` is the crux.
- **`exclusive` buys nothing over `write` today** — the CAS predicate treats the two identically
  (`server/leases.py`). Reviving a true exclusive mode is Stage 1 of the symbol-mode plan and needs
  no symbol mode itself.
- **Contract freeze is detection, not prevention** — the integrator *detects* a landed signature
  change and emits `contract_change` (`coordinator/integrator.py`), but the preventive half (an
  exclusive lock on a frozen symbol before it changes) is inert at file granularity. See DESIGN §5.3.
- **The heartbeat clock knife-edge** — `server/leases.py::heartbeat` is the one predicate where a
  stale clock points the *unsafe* way (it could revive a dead lease). It's currently correct because
  it reads SQLite's own clock, but a deployment that shrinks the TTL toward request latency reopens
  it. Well-commented; worth hardening.
- **Multi-language** — the classifier is Python-`ast` only. The `indexer.py` interface was written so
  a tree-sitter backend can replace it for other languages; that backend is stubbed.
- **Multi-host** — out of scope by construction: one SQLite file on one filesystem, local worktrees.
  A network-attached blackboard / distributed lock is a real project, not a config flag (DESIGN §6).
- **Semantic conflicts** — two edits to *different* files whose *combination* is wrong. Leases and
  hashes catch structure, not behavior; the test gate is the backstop, but a semantic conflict no
  test covers can still reach trunk. This is the honest hard limit of the whole field — swarm-sync
  surfaces these rather than pretending to prevent them.

Before exposing any of this beyond localhost, read the
[security and trust model](README.md#security-and-trust-model-read-before-exposing-this-to-anything)
— the integrator runs agent-authored test code by design, and mutating routes are unauthenticated
unless you set `SWARMSYNC_TOKEN`.

---

## Further reading

- [`DESIGN.md`](DESIGN.md) — the full spec: schema, every endpoint, all five test case demos, the
  complete failure-handling table, and the operational surface (env vars, launchers).
- [`SYMBOL_MODE_DESIGN.md`](SYMBOL_MODE_DESIGN.md) — why per-symbol leasing is parked and how it
  would come back.
