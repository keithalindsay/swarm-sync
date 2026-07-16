# BUILD_PLAN — swarm-sync

Ordered, independently buildable + testable units for an autonomous build loop. Each unit is
~1 agent-session, ends with a concrete **done-when** test, and builds only on units before it.
The loop should implement units in order, run the done-when test, and only advance on green.
Update `MEMORY.md` after each unit.

Conventions: run `pytest tests/<file>` for a unit's test; `git` and Python 3.11 available;
DESIGN.md section references point to the authoritative spec.

---

### U1 — Blackboard DB + schema
- **Goal:** SQLite (WAL) connection helper + idempotent schema init. (DESIGN §4.1)
- **Files:** `swarmsync/blackboard/db.py`, `swarmsync/blackboard/models.py`, `swarmsync/blackboard/schema.sql` (exists), `tests/test_blackboard.py`
- **Done when:** `init_db(tmp)` creates all 6 tables, `PRAGMA journal_mode` returns `wal`, and a second `init_db` on the same file is a no-op. `pytest tests/test_blackboard.py` green.

### U2 — Classifier: parcel extraction
- **Goal:** `parse_file` / `index_repo` turn Python source into parcels (symbol + spans + content_hash). (DESIGN §3 steps 1-2)
- **Files:** `swarmsync/classifier/indexer.py`, `tests/test_indexer.py`
- **Done when:** parsing a fixture with 2 functions + 1 class-with-method yields exactly those parcels plus one `<module>` interstitial, each with correct kind, non-overlapping byte spans, and a stable sha256 content_hash. Green.

### U3 — Dependency graph, blast radius, frozen contracts
- **Goal:** build import/call graph, compute reverse-dep blast radius, extract frozen contracts, `co_schedulable`. (DESIGN §3 steps 3-6)
- **Files:** `swarmsync/classifier/graph.py`, `tests/test_graph.py`
- **Done when:** on a 3-module fixture, a symbol imported by 3+ others has blast_radius ≥3 and is returned as a frozen contract with a signature+type_hash; `co_schedulable` is False for two symbols in one file (file mode) and True for symbols in different files. Green.

### U4 — Index API population
- **Goal:** write classifier output into the blackboard (`parcels`, `contracts`). (DESIGN §3 output)
- **Files:** `swarmsync/classifier/store.py` (new) or extend `graph.py`; `tests/test_index_api.py`
- **Done when:** running the index over `sample_repo/` (or a fixture) inserts one row per parcel and one row per frozen contract; re-running updates in place (no duplicates). Green.

### U5 — Lease manager (atomic CAS)
- **Goal:** `acquire`/`heartbeat`/`release` with single-transaction compare-and-swap. (DESIGN §5.2)
- **Files:** `swarmsync/server/leases.py`, `tests/test_leases.py`
- **Done when:** two `acquire()` calls on the same parcel in write mode yield exactly one `granted` and one `denied`; a read+read pair both grant; after `release`, the parcel is acquirable again. Green.

### U6 — Event log + pheromone
- **Goal:** `emit`/`tail` append-only log; pheromone drop/decay. (DESIGN §4.1, §4.3)
- **Files:** `swarmsync/server/events.py`, `tests/test_events.py`
- **Done when:** `emit` returns monotonically increasing seq; `tail(since=k)` returns only seq>k in order; `decay_pheromone` reduces strength and never below 0. Green.

### U7 — FastAPI server
- **Goal:** wire all endpoints over the blackboard. (DESIGN §4.2)
- **Files:** `swarmsync/server/app.py`, `tests/test_server.py`
- **Done when:** via `TestClient`, POST /index then GET /parcels returns the map; POST /lease returns granted/denied; POST /intent, /heartbeat, /release, /parcel/update, GET /contract/{sym}, GET /events?since= all return expected shapes. Green.

### U8 — git worktree ops
- **Goal:** subprocess wrappers for worktree lifecycle + merge. (DESIGN §5.1, §5.4)
- **Files:** `swarmsync/worktree/git_ops.py`, `tests/test_git_ops.py`
- **Done when:** on a temp repo, `add_worktree` creates an isolated branch dir; edits+`commit_all` in two worktrees on disjoint files both `merge_branch` into `integration` with no conflict; an overlapping edit returns `ok=False` with conflict paths. Green.

### U9 — Agent client + runner + mutators
- **Goal:** thin blackboard client and the full agent lifecycle with scripted edits. (DESIGN §4.3, §2)
- **Files:** `swarmsync/agent/client.py`, `swarmsync/agent/runner.py`, `swarmsync/agent/mutators.py`, `tests/test_agent.py`
- **Done when:** against a running TestClient server, one `run_agent` declares intent, acquires a write-lease, edits a function in its worktree via a mutator, commits, posts parcel_update, and releases — verified by the resulting event sequence and a committed diff. Green.

### U10 — Serial test-gated integrator
- **Goal:** serialized merge + impact pytest gate + re-index + authoritative summary regen. (DESIGN §5.4, §5.5)
- **Files:** `swarmsync/coordinator/integrator.py`, `tests/test_integrator.py`
- **Done when:** two disjoint-file branches integrate serially and both land with green tests + `merged` events; a branch whose edit breaks a sample test is `merge_rejected`, is reset out, and leaves `integration` tests green. Green.

### U11 — Reaper + pheromone decay loop
- **Goal:** reclaim leases whose heartbeats stopped; decay pheromone. (DESIGN §6)
- **Files:** `swarmsync/coordinator/reaper.py`, `tests/test_reaper.py`
- **Done when:** a lease with `ttl_expires_at` in the past is marked `reaped`, emits a `reaped` event, and its parcel becomes acquirable by a new agent; decay runs without error. Green.

### U12 — Broker (task→parcel scheduling)
- **Goal:** resolve tasks to parcels, run only co-schedulable work concurrently, reassign on reap. (DESIGN §5, §6)
- **Files:** `swarmsync/coordinator/broker.py`, `tests/test_broker.py`
- **Done when:** given 3 tasks (2 disjoint, 1 overlapping), the broker dispatches the 2 disjoint concurrently and serializes the overlapping one; a task whose agent is reaped is reassigned and completes. Green.

### U13 — Sample repo + its test suite
- **Goal:** the small target codebase agents edit. (DESIGN §7)
- **Files:** `sample_repo/*.py`, `sample_repo/tests/*.py`, `tests/test_sample_repo.py`
- **Done when:** `sample_repo` has ≥3 modules with a real import/call graph, ≥1 file with two independent functions, ≥1 high-fan-in symbol (frozen-contract candidate), and `pytest sample_repo/tests` is green. Green.

### U14 — End-to-end demo: money shots #1, #2, #4, #5
- **Goal:** wire the demo harness; prove concurrent disjoint lands, serialization, crash recovery, gate rejection. (DESIGN §7)
- **Files:** `demo/run_demo.py`, `tests/test_demo.py`
- **Done when:** `python demo/run_demo.py` runs ≥3 agents on `sample_repo`, prints PASS for money-shots #1/#2/#4/#5, exits 0; zero same-file textual collisions reached `integration`; trunk green throughout. Green.

### U15 — Money shot #3: frozen-contract change + dependent re-plan
- **Goal:** contract-change notify path and dependent re-plan. (DESIGN §5.3, §7)
- **Files:** extend `swarmsync/agent/runner.py`, `swarmsync/coordinator/broker.py`, `demo/run_demo.py`; `tests/test_demo.py`
- **Done when:** the demo shows an agent changing a frozen signature (exclusive lease + `contract_change` event), a dependent agent observing it, re-reading the contract, and landing a call-site fix with tests green; money-shot #3 prints PASS and the full demo exits 0 with all five PASS.

---

**Final acceptance (whole prototype):** `python demo/run_demo.py` exits 0 with all five money
shots PASS; across the run, **zero same-file textual collisions reach the integration branch** and
every landed commit leaves `sample_repo` tests green.
