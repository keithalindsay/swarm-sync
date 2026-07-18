# Audit Round 6 — full-spectrum evaluation

Four independent audit passes (correctness/concurrency, security, code quality/architecture,
usability/DX) run against commit `8beeb49`, every finding verified against source before being
recorded. Companion document: [`IMPROVEMENT_PLAN.md`](IMPROVEMENT_PLAN.md) turns this into phased,
subagent-executable work packages.

Severity scale: **P0** trunk-destroying / data loss · **P1** serious, reachable in normal use ·
**P2** moderate / needs unusual-but-real conditions · **P3** minor. Usability findings use
blocker/major/minor for *adoption* impact.

---

## 1. Scorecard

| Metric | Value | Notes |
|---|---|---|
| Tests | **312 passed**, 0 failed (47.6s) | 3 deprecation warnings (anyio/py3.11) |
| Coverage | **94%** (1,704 stmts, 97 missed) | The missing 6% characterized in §5.1 |
| Ruff | clean | |
| Mypy | clean (25 files) | untyped-body note in `server/app.py:361` |
| Avg cyclomatic complexity | **A (3.39)** across 163 blocks | 8 blocks ≥ C, 1 at D (§5.2) |
| Maintainability index | all modules rank A | lowest raw: `integrator.py` 43.4 |
| Source / test LOC | 5,460 / 7,472 (ratio 1.37) | statements are only 31% of source lines — doc-heavy by design |
| Import cycles | none | but one layering inversion (§5.3) |
| SQL injection / shell injection / auth bypass | **none found** | §4 |
| Demo | 5/5 PASS in 8.6s from a fresh clone | quickstart caveat in §6.1 |

**Overall:** the codebase is unusually careful and unusually honest — every dangerous edge carries a
written justification, the fail-open policy is deliberate and documented, and five audit rounds have
burned off nearly all the easy defects. What's left clusters into four shapes: **(a)** one genuine
P0 in crash-recovery replay, **(b)** the hook path (the mode where the lease is the *only*
protection) has integrity holes the broker path doesn't, **(c)** nothing is bounded — events,
parcels, leases, scans all grow forever against a persistent DB with no schema versioning, and
**(d)** there is no operational surface — a silently-uncoordinated session is indistinguishable
from a working one.

---

## 2. Correctness & concurrency findings (C-series)

### C1 · P0 — Startup reconciliation re-orphans forever; every restart after one orphan resets trunk to a stale sha
`coordinator/integrator.py:709, 736-779`

`reconcile_orphaned_integrations` treats only `{"merged", "merge_rejected", "needs_rebase"}` as
terminal. The event it emits itself, `integrate_orphaned`, is **not** in that set and is skipped by
the scan filter. Scenario: an integrate is SIGKILLed mid-gate (`integrate_started` recorded
`trunk_sha_before=S`); restart #1 correctly resets trunk to `S` and emits `integrate_orphaned`;
agents then land ten legitimate merges (trunk = `S+10`); restart #2 replays the log, finds the same
start still "open," sees `current != S`, and runs `git reset --hard S` — **destroying all ten gated
merges**. Repeats on every restart thereafter.

Fix (S): add `integrate_orphaned` to the terminal set and pop the open-start key on it; match
starts to verdicts by the start's `seq` rather than `(repo, branch, into)` so a reused branch name
can't close an unrelated older orphan.

### C2 · P1 — Hook-path agent identity collapse
`hooks/adapter.py:463-468, 435-441`

`_agent_id` falls back `agent_id → session_id → "main"`. Claude Code hook payloads carry
`session_id`, which parallel subagents of one session **share**. Unless the deployed Claude Code
supplies a distinct per-subagent `agent_id` (tests only exercise synthetic payloads that set it),
every subagent maps to one identity. On the path where the lease is the only protection:
`cmd_precheck` allows when `owner == agent_id`, so two subagents editing the same file are both
allowed — the lock is vacuously permissive between exactly the agents it exists to separate. And
`cmd_release` on any single SubagentStop releases **every** lease under the shared identity,
dropping protection for subagents still mid-edit. Verify against the real payload shape first.

### C3 · P1 — Reconciliation is silently blind past 1M events
`coordinator/integrator.py:736`

`events_mod.tail(conn, since_seq=0, limit=1_000_000)`, oldest-first, on the event loop at startup.
Heartbeats emit events, so 1M rows is weeks of real use. Past it: recent orphans (at the tail) are
invisible — a poisoned trunk is never rolled back; and a start whose verdict landed past the window
looks orphaned — trunk reset to an ancient sha (same blast radius as C1).

### C4 · P1 — Reaper blocks the loop, dies permanently, poisons shutdown
`coordinator/reaper.py:145-173`, `server/app.py:303-319`, `blackboard/db.py:64`

Three verified parts: (1) `run()` is `async` but calls `reap_once`/`decay_once` synchronously on
the loop thread with `busy_timeout=5000` — one contended write freezes the whole ASGI server up to
5s per statement; the reap UPDATE is also a full-table scan (only index is `(parcel_id, status)`)
over a never-pruned `leases` table. (2) No try/except: one transient
`sqlite3.OperationalError: database is locked` kills the task permanently and nothing notices —
no reaping, no pheromone decay for the rest of the process. (3) On shutdown, `await task` re-raises
the stored exception; only `CancelledError` is caught, so lifespan teardown aborts before
`conn.close()`.

### C5 · P1 — `/parcel/update` has no lease-ownership check
`server/app.py:470-508` (ROUND5 known-open, verified still present, impact sharpened)

The UPDATE is keyed on `parcel_id` only; `body.agent_id` is decorative. Any client can overwrite
`content_hash`/`state_summary` for a parcel another agent holds a write lease on. Concrete damage:
`_check_read_deps` compares plan-time snapshots against exactly this column, so a rogue or stale
`parcel_update` (e.g. the hook's `postupdate` posted after its lease expired and another agent took
over — the adapter never verifies ownership before posting) spuriously bounces innocent agents or
validates against state that never landed. Fix (S): a single SQL predicate requiring an active
write/exclusive lease owned by `body.agent_id`, same shape as `heartbeat`'s scoping.

### C6 · P1 — No rebase-and-resubmit, and the "preserved" rejected branch is destroyed on rerun
`coordinator/broker.py:370-383`, `worktree/git_ops.py:149-169`, `agent/runner.py:126-143`

Known-open (DESIGN §5.5 promises it; `needs_rebase`/`merge_rejected` are terminal; broker retries
only `lease_denied`) — plus a **new corollary**: `_cleanup_worktree` deliberately keeps the branch
on rejection ("the ONLY reference to the agent's commits"), but `_prune_stale_worktree`, called at
the top of every `add_worktree`, runs `git branch -D <name>` unconditionally. Broker attempt ids
are deterministic (`{task_id}-attempt-{n}`), so re-running the same task list after a rejection
**deletes the preserved commits before doing anything else**. The preservation rationale is dead in
practice. Interim fix (S): park rejected branches under `rejected/<id>-<ts>` out of pruning's
reach. Real fix (L): the §5.5 rebase-and-resubmit loop with bounded attempts.

### C7 · P1 — The "events are the replay source of truth" claim is false three ways
`blackboard/schema.sql:3-4`, throughout

(1) **Not atomic**: every state change + its event are separate autocommit statements
(`isolation_level=None`) — lease INSERT then emit; parcel UPDATE then emit; reap UPDATE then
per-row emits. A crash between them diverges state from log. (2) **Not complete**: `run_index`
mutates `parcels`+`contracts` and emits nothing; `_ensure_parcel` mints rows silently; pheromone
decay emits nothing. (3) **Not versioned**: `init_db` is `CREATE TABLE IF NOT EXISTS` only; no
schema-version row, no migrations — any schema change strands existing DBs silently. Cheapest
honest fix (S): demote the claim — events = audit log, SQLite tables = truth — and add a
`schema_version` table. Real event-sourcing is L and probably not worth it.

### C8 · P2 — Hook precheck can deny an agent because of its own lease
`hooks/adapter.py:327-337`, `server/leases.py:159-165`

Two concurrent tool calls from one agent (Claude Code batches parallel Edits routinely): both
prechecks see no lease, both POST `/lease`; the CAS has no same-agent exemption, so the second is
denied — and the deny path names the winner without checking whether the winner **is this agent**.
The agent is blocked from a file it just locked, with a message naming itself as the blocker. Fix
(S): after a lost acquire, if `_find_holder(...) == agent_id`, allow and keepalive.

### C9 · P2 — TTL is unvalidated everywhere; a lease can be granted and dead simultaneously
`server/leases.py:116-137`, `blackboard/models.py:138-168`, `hooks/adapter.py:110-119`

`POST /lease` accepts any float. With `ttl <= 0` the row is born expired: the caller gets
`granted=True` while the CAS treats the lease as non-blocking — **two writers, both told they hold
the lock**. Reachable by one typo: `SWARMSYNC_LEASE_TTL=0` parses fine and silently disables all
hook-path protection. Huge TTLs are the symmetric hazard (effectively permanent lease). Fix (S):
`gt=0` + sane ceiling on `LeaseRequest`/`HeartbeatBody`, clamp in `_hook_lease_ttl`.

### C10 · P2 — Hook 2s timeout < server 5s busy_timeout: load silently un-gates the shared tree
`hooks/adapter.py:85, 563-565`, `blackboard/db.py:64`

Precisely when the system is busiest (integrate re-index holding a write txn, WAL checkpoint on a
grown events table, reaper contending), server writes can take up to 5s; the hook gives up at 2s
and the umbrella `except → exit 0` turns that into ALLOW. Fail-open was designed for *broken*
setups; here it engages for *healthy-but-loaded* ones, invisibly, in the one mode where the lease
is the only protection. Fix (S): raise hook timeout above busy_timeout (8-10s is acceptable
PreToolUse latency), and/or fail closed with a retry message when the marker file confirms active
coordination and recent contact.

### C11 · P2 — One transient git error aborts the whole broker run and leaks leases
`agent/runner.py:219-324`, `coordinator/broker.py:425-440`

`run_agent` has try/finally but no except: an exception from
`add_worktree`/mutator/`commit_all`/HTTP leaves all acquired leases active until TTL. The broker
re-raises from `future.result()`, aborting `run()` and discarding all other tasks' results.
Concurrent `git worktree add`/`checkout` in one repo can transiently fail on ref/index lock
contention, making this reachable under exactly the concurrency the broker creates. Fix (S-M):
release held leases on the exception path; catch per-task exceptions into an error `AgentResult`.

### C12 · P2 — Hook parcel ids are cwd-relative; the server's are root-relative
`hooks/adapter.py:471-473, 176-214`, `server/app.py:179-185`

If a session runs in `repo/subdir` (or two agents run with different cwds), the same file yields
two parcel ids (`a.py::<module>` vs `subdir/a.py::<module>`); `ensure_parcel=True` mints the ghost
row and two write leases coexist on one physical file — the exact collision the lease exists to
prevent. Also split-brains hook-path leases against broker/index-path leases. Fix (S-M): resolve
the repo root by walking to the git root, or query the server's managed root at session start.

### C13 · P2 — Heartbeat clock knife-edge (known; currently latent, sharpened)
`server/leases.py:239-251`

The hardened form is correct at default TTLs (liveness predicate evaluated on SQLite's clock,
atomic with the SET). What keeps it a knife-edge: TTL is caller-supplied and unvalidated (C9), and
correctness rests on `julianday('now')`-derived epoch agreeing with Python `time.time()` (acquire
and reap still use Python-side timestamps) — an implicit cross-clock invariant with no test or
assertion. Fix (S): TTL floor (≥ 2× busy_timeout) + a startup assertion comparing the two clocks.

### C14 · P2 — Renames/deletes leave ghost parcels and contracts served as truth
`classifier/store.py:38-45`, `hooks/adapter.py:395-396`, `coordinator/integrator.py:626-632`
(ROUND5 known-open, verified still present)

No stale-row pruning: deleted/renamed files' `parcels`/`contracts` rows persist forever;
`GET /contract/{symbol}` serves dead signatures; a renamed symbol never triggers
`contract_change` (the old row simply never changes) so dependents are never told. The adapter's
postupdate returns early when the file no longer exists, leaving the last-good `content_hash`
advertised. Fix (M): the integrator's post-land re-index retires vanished parcels/contracts
(closing leases/pheromone first for the FK), emitting `parcel_retired`.

### C15-C17 · P3 — Minor races and staleness
- **C15** `server/events.py:158-177` — `decay_pheromone` is read-modify-write in Python; a
  `drop_pheromone` landing between SELECT and `executemany` gets overwritten with a stale decayed
  value. Fix: single-statement SQL UPDATE with `pow()`.
- **C16** `coordinator/integrator.py:411, 666-691` — every event in one `integrate()` carries the
  call-entry `ts`; a `merged` after a 600s gate is stamped 10 minutes old.
- **C17** `agent/runner.py:211` — "read the world" fetches `events(since=0)`: the *oldest* 1000
  events; past 1000 rows the awareness signal is pure noise.

### Examined and found sound
The acquire CAS across connections; `RETURNING`-based id capture (lastrowid race closed);
`reap_once`'s single-statement UPDATE (reap-vs-heartbeat TOCTOU closed); Python-side `:now` in
acquire/reap points the safe way; `.worktrees` excluded from index walk and pytest collection;
`post_integrate`'s lock + threadpool pattern (single-process assumption documented).

---

## 3. Security findings (S-series)

Graded against the *documented* trust model (localhost, cooperative agents, integrator runs
agent-authored tests by design). **No SQL injection, no shell/argument injection, no auth bypass
found.** Timing-safe token comparison (`hmac.compare_digest`); mutating-route guard completeness is
itself asserted by a test; `git_ops.py` argv-only with `-`-prefix rejection, name allow-listing,
and `--end-of-options` fences.

### S1 · P2 — Unbounded parcels/leases via `/lease` with `ensure_parcel=True`
`server/leases.py:102-113` via `server/app.py:427`

Any client can post unlimited distinct parcel ids; each mints a permanent `parcels` row + an
acquirable lease + a `lease_granted` event. No cap, no cleanup, no per-agent quota. Fix (M):
per-agent quota on auto-created parcels/active leases; shape/length check on `ensure_parcel` ids.

### S2 · P2 — Unbounded `events` table; uncapped `GET /events?limit=`; 1M-row boot scan
`server/events.py:54`, `server/app.py:513`, `coordinator/integrator.py:736`

`?limit=999999999` materializes the whole table into memory + JSON. Compounds C3/C7. Fix (S/M):
clamp limit (e.g. 1000); retention/compaction job (heartbeat events especially); bounded or
projection-based reconciliation scan.

### S3 · P3 — The `root + os.sep` boundary check is undefended by tests (M-2, confirmed)
`server/app.py:229`, `tests/test_security.py`

The check is *correct*; the problem is that deleting `+ os.sep` (reducing to
`real.startswith(root)`) leaves the whole suite green — the documented mutation survivor. One
careless edit from a sibling-prefix escape (`/managed` vs `/managed-evil`) with nothing to catch
it. Fix (S): one test with a sibling dir whose name extends the root, asserting 403.

### S4 · P3 — Out-of-repo leaf symlinks leak external file structure on unauthenticated reads
`classifier/indexer.py:274-280`, `hooks/adapter.py:190-214`

By design (S5: keep it leasable), `link.py -> /home/other/private/thing.py` is parsed and its
symbol names/signatures/spans land in `parcels`/`contracts`, served on unauthenticated
`GET /parcels`. README warns reads leak "your code's structure" but not *out-of-repo* structure.
This is also ROUND5's open "leaf-symlink escape" decision. Decide: doc-note the behavior, or skip
out-of-repo targets in the indexer (S either way) — and write the decision down.

### S5 · P3 — No HTTP request-body size limit
No middleware cap anywhere; a giant POST body is read fully into memory. Trivial to add (S).

### Documented-accepted (consistent between trust model and code — no action)
Hook fail-open policy · Bash-write bypass · arbitrary code via the pytest gate · open read routes ·
TOCTOU on managed-path validation · `/integrate` merging any existing commit-ish.

---

## 4. Architecture & code-quality findings (A-series)

### A1 — `needs_rebase` / `expected_read_deps` is unreachable in production
`coordinator/integrator.py:142-160, 413-436`, `blackboard/models.py:178-184`, `agent/runner.py:245-247`

`IntegrateBody` has no `expected_read_deps` field, so `POST /integrate` — the only path the runner,
broker, and hook use — can never trigger the optimistic re-check. The runner collects
`contract_snapshot` and never sends it. ~50 LOC + an event type + 4 tests exercise a capability no
deployment can reach, and unlike the parked symbol-mode branches it is **not** behind a guard that
says so. Pick one: **wire it** (add the field, pass the snapshot — S, turns dead code into the
advertised drift detection) or **delete step 1** and note it in SYMBOL_MODE_DESIGN.md. Carrying it
as-is is the worst option.

### A2 — Layering inversion: pure-SQL `events.py`/`leases.py` homed in `server/`
`coordinator/integrator.py:88`, `coordinator/reaper.py:55` import `server.events`; `server/app.py:90-91`
imports `coordinator` — bidirectional package dependency, one import away from a cycle. Both
modules are pure SQLite domain operations (no FastAPI); ARCHITECTURE.md's own diagram places them
*inside the blackboard*. Fix (M, low risk): move both to `blackboard/`, keep re-export shims one
release; the layering becomes strictly `blackboard ← {classifier, server, coordinator, agent}`.

### A3 — `integrator.py` is the god module
780 LOC, MI 43.4 (repo lowest), 3 of the 8 flagged functions (`integrate` C19,
`reconcile_orphaned_integrations` C16, `run_impact_tests` C14). It does five jobs: merge,
gate/subprocess-kill machinery, re-index, contract diff, reconciliation. The highest-stakes file
(trunk safety) is the hardest to modify safely. Fix (M, medium risk — strong tests mitigate):
extract `run_impact_tests` + process-group-kill/stream machinery into `coordinator/gate.py`;
extract the contract-diff helper.

### A4 — Config sprawl: 7 point-of-use env knobs, two launchers, one misnamed var
- `SWARM_SYNC_DB` breaks the `SWARMSYNC_*` convention and is honored only by the `swarm-sync`
  launcher (`server/app.py:568`).
- Two launchers with different defaults for everything: `swarm-sync` (port 8000, `blackboard.db`,
  no `--root`) vs `swarmsync-serve` (port 8787, `swarmsync.db`, `--root`, banner). The hook's
  default URL matches only the second — the wrong launcher yields a hook that silently fails open
  against the wrong port.
- `SWARMSYNC_GATE_TIMEOUT` parsed twice with subtly different fallbacks (`integrator.py:112-120`,
  `client.py:47-62`) plus a third parse-float-env copy in `adapter.py:110-119`.
- `serve.py:37` mutates `os.environ` to pass config to `app.py`.
- `SWARMSYNC_LEASE_TTL` appears in no doc.

Fix (S-M): one `swarmsync/config.py` with typed accessors; accept `SWARMSYNC_DB` with the old name
as deprecated alias; retire one launcher (make the other an alias); one env-var table in README.

### A5 — Duplication worth factoring
- `MODULE_SYMBOL = "<module>"` defined 3× (`indexer.py:49` canonical, `graph.py:80`,
  `broker.py:140`); parcel-id construction/splitting scattered across 6+ sites (`broker._module_id`,
  `adapter._parcel_id`, `partition("::")` in `leases.py:102`, `runner.py:280`). Any id-scheme change
  (which the multi-root fix and symbol mode both contemplate) touches all of them. Fix (S): central
  parcel-id helpers in `blackboard/`.
- `_find_holder` vs `_find_lease` (`adapter.py:259-271`) — delete one.
- `client.integrate` smuggles transport into domain data (`result["_status_code"]`).

### A6 — API-shape inconsistencies
`GET /parcels` and `GET /leases` (`app.py:352-382`) return raw `dict(row)` with no response model —
the wire shape is whatever `schema.sql` says today; the hook duck-types against it. Unknown-entity
signaling is split: `/parcel/update` → 404 but `/heartbeat`/`/release` → 200 `{"ok": false}`.
Fix (S): `response_model` from the existing pydantic models; pick one convention (the soft-fail
fits the philosophy better).

### A7 — Broker's shared-connection model contradicts the server's
`coordinator/broker.py:425-440` shares one SQLite connection across worker threads (the `RETURNING`
comments are scar tissue from real races this invites), while the server uses per-request
connections — two opposing connection disciplines in one codebase. Fix (S-M): per-thread
`db.connect()` in `_run_task_with_retries`.

### A8 — Test-coverage gaps that matter (the 6%)
- **The `BaseException` trunk-rollback handler** (`integrator.py:654-663`) — the only guard between
  an operator's Ctrl-C and a permanently un-gated merge on trunk — has **zero test coverage**. One
  test: stub the gate to raise `KeyboardInterrupt`, assert rollback + re-raise. (S, no risk.)
- **`graph.py` is the lowest-covered module (85%) containing the highest-complexity function**
  (`build_graph`, D-26). Uncovered *reachable* logic: relative-import resolution
  (`163-169`), class signatures (`140-146`), vararg/kw-only rendering (`125-133`) — these feed
  blast radius → contracts → impact-test selection, and would fail silently. (S: targeted unit
  tests; M: split `build_graph`'s import pass from its signature pass.)
- Silent `except: pass` in `runner.py:112-118` (heartbeat thread) and `144-147` (worktree
  cleanup) — a dying blackboard mid-run leaves zero trace. Add `logger.debug`. (S)

### Explicitly not issues
The guarded symbol-mode branches (<60 lines, fenced by a guard that raises with a full explanation
— cheapest form of design memory; keep). `app.py`'s size (it's docs, not logic — 172 statements).
The hook's broad excepts (fail-open is the documented policy). The three state-summary builders
(intentional advisory/authoritative split).

---

## 5. Usability & DX findings (U-series)

Measured on this machine: venv + `pip install -e ".[dev]"` 11.6s; demo 8.6s, 5/5 PASS; suite 48.7s,
312 green. Time-to-first-success ≈ 1 minute — *if* your `python3` is 3.11+.

### U1 · Major — Quickstart's literal commands hang on stock Ubuntu
README says "check your version first" then shows `python3 -m venv .venv`. On Ubuntu 22.04
(`python3` = 3.10, `python3.11` alongside), pasting literally builds a 3.10 venv and
`pip install -e ".[dev]"` **hangs in resolver backtracking 8+ minutes with zero output** (verified
live). Fix (S): quickstart leads with `python3.11 -m venv` + a fail-fast preflight one-liner.

### U2 · Blocker (for operating) — No status/observability surface at all
No `/health`, no `/` (both 404, verified), no CLI for "what's leased right now, by whom, expiring
when." `GET /leases` returns raw epoch floats. The event log is the system's soul and the only tail
is hand-curl with `?since=`. Combined with fail-open quietness (U4), "it's silently not
coordinating" is indistinguishable from "it's working." Fix (M): `swarmsync status` / `leases` /
`events --follow` + a `/health` endpoint returning `{root, db_path, active_leases, last_event_seq}`.

### U3 · Major — The deny message under-informs, and "retry shortly" is misleading
Live capture: `swarm-sync: calc.py is leased by agent-a; pick different work or retry shortly.`
No TTL remaining, no pointer to what *is* free — and hook leases renew on every precheck/postupdate,
so the lease lives until the holder *stops*; "retry shortly" tells a waiting agent to burn turns
polling a lock that won't lapse. The TTL is already in the lease dict the adapter just fetched.
Related: the server-side `LeaseResult.reason` omits the holder entirely (the adapter does a second
round-trip to recover it) — include `holder`/`ttl_expires_at` in the deny response. Fix (S).

### U4 · Major — Silent-failure modes are documented but not detectable
Server down / wrong root / wrong port → hooks fail open with a note on hook stderr, which Claude
Code doesn't surface (adapter.py:54 says so itself). Fix (M): `swarmsync doctor` — server reachable
at `SWARMSYNC_URL`, root matches cwd, marker file present, hooks wired in settings.json, DB
writable. ~150 lines; kills both README Troubleshooting bullets.

### U5 · Major — No work-discovery affordance on the hook path
The only coordination signal an agent ever receives is being denied *after* deciding to edit.
Pheromones/intents/`/parcels` exist but nothing surfaces them. Fix (M): `swarmsync free [path...]`
/ `swarmsync holds` that agents can Bash-call, named in the deny message.

### U6 · Major — The swarmsync skill is machine-local and unshipped
A good SKILL.md (deny-message etiquette, setup) exists at `~/.claude/skills/swarmsync/` but the
repo contains zero references to it and doesn't ship it; it also hardcodes `~/projects/swarm-sync`
paths. Every adopter rebuilds that knowledge. Fix (S): ship it in-repo, parameterize, link from
README.

### U7 · Major — First-impression/trust: internal audit harness shipped in `scripts/`
`scripts/audit-r4-workflow.js` is a personal audit workflow containing private context (a
near-miss anecdote, absolute home paths). Commit d6ef512 removed internal docs; this survived,
sitting next to `swarmsync-hook-guard` — the one script users are told to wire into their editor.
Fix (S): remove it.

### U8 · Major — Stale-DB hazard has no remedy command
`blackboard.db` sits in the repo root right now (residue of running the `swarm-sync` launcher from
cwd). DB paths are cwd-relative; leases survive restart; re-index never deletes stale parcels.
Reusing a DB against a different repo silently mixes parcel maps (compounds C14). Fix (S-M):
`swarmsync reset` / `--fresh`; default the DB under `$XDG_RUNTIME_DIR` keyed by root hash.

### U9 · Minor — Papercuts
Not on PyPI, name unclaimed (squatting risk) · README sample output says `PASS: case #1`, demo
prints `PASS: money-shot #1` · `requirements.txt` duplicates pyproject (drift trap) ·
`/docs` (Swagger) works and is mentioned nowhere · broker `BlackboardClient` against a down server
surfaces a raw `httpx.ConnectError` traceback · `swarmsync init-hooks` would replace a 20-line
hand-edited settings.json paste.

### What's already strong (keep it that way)
Honest docs that document their own footguns; the doc-triad router (README → ARCHITECTURE →
DESIGN); exemplary fail-open engineering in the adapter (deny only on positive confirmation;
zero-cost when inactive); self-cleaning demo; config-layer error messages that name their remedies
(`MultiRootError` and the managed-path 403 are model error messages).

---

## 6. The one strategic question (deferred since R3, still open)

ROUND5 posed it and this audit re-confirms it: **is the parcel/contract abstraction load-bearing or
decoration?** Today the default mode (file granularity) is the one where frozen contracts do
nothing; the mode where they'd work (symbol) is the parked-unsafe one; `needs_rebase` — the one
contract-consuming mechanism — is unreachable over the wire (A1); and renames silently ghost the
contract rows (C14). Everything in the C/S/A/U lists can be fixed without answering this, but
Phase 5+ of the improvement plan forces the choice: wire the contract machinery to do real work
(A1-wire, exclusive-mode Stage 1, span containment) or trim the system honestly down to
file-lease + gate, which is the part that demonstrably works. The improvement plan sequences
everything else first so the answer can be made with the operational surface (U2) in place to
observe real behavior.
