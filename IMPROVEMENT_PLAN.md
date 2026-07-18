# Improvement Plan — phased work packages for expert subagents

Companion to [`AUDIT_R6.md`](AUDIT_R6.md), which holds the full findings (C = correctness,
S = security, A = architecture, U = usability). This document is the execution plan: six phases,
each a set of work packages (WPs) sized for one expert subagent, with explicit file footprints so
disjoint WPs can run in parallel — under swarm-sync itself, if you like the symmetry.

## Ground rules (binding on every WP)

1. **The campaign rule:** *a fix without a test that fails when you delete the fix is not a fix.*
   Every WP's acceptance criteria include its mutation test. Revert the fix locally, run the suite,
   watch it fail, restore.
2. **Gates before merge:** `ruff` clean · `mypy` clean · full suite green 3× (no flakes) ·
   `demo/run_demo.py` 5/5 · coverage does not drop.
3. **~50% of unverified findings die on contact** (R4's measured rate). Each WP begins by
   *reproducing* its finding — a failing test or observed behavior — before changing code. If it
   can't be reproduced, report that instead of "fixing" it.
4. **Docs move with code.** Any WP that changes behavior updates README/ARCHITECTURE/DESIGN in the
   same change, and prunes stale history-prose in the docstrings it touches (the U1-U15/S1-S5 unit
   narration is drifting; trim what you touch, don't archaeology-tour it).
5. **Don't touch the parked symbol-mode branches** (`graph.py` symbol arm, `broker.py` symbol arm,
   the `SymbolModeError` guard) except where a WP names them. They are fenced design memory.
6. One WP = one PR-shaped change. Small, reviewable, individually revertable.

## Phase map

| Phase | Theme | Why this order |
|---|---|---|
| 1 | Stop the bleeding — P0/P1 correctness, all small | Trunk-destroying and lock-defeating bugs first; everything else builds on a trustworthy core |
| 2 | Hook-path integrity | The mode real users (Claude Code sessions) run in has the weakest guarantees; fix before promoting it |
| 3 | Bound every resource | Persistent DB + unbounded growth = every long-lived deployment degrades; also unblocks Phase 5's observability claims |
| 4 | Architecture consolidation | Refactors that make the codebase safe to extend; deliberately *after* correctness so refactors land on tested ground |
| 5 | Operability & adoption | Status/doctor/CLI, docs, packaging — the difference between a demo and a tool |
| 6 | Strategic functionality | Rebase-and-resubmit, the contract question, symbol-mode Stage 1 — big rocks, informed by Phase 5's observability |

Dependencies run forward only: a Phase N WP never needs a Phase N+1 WP. Within a phase, WPs are
parallel unless a dependency is named. **Bold file lists** are the write-footprint (tests implied).

---

## Phase 1 — Stop the bleeding

*Everything here is S-effort, high blast-radius, independently landable. Run all six in parallel.*

### WP1.1 — Reconciliation must not re-orphan (C1, the P0)
**Files:** `coordinator/integrator.py`
- Add `integrate_orphaned` to the terminal-event set in `reconcile_orphaned_integrations`; include
  it in the scan filter; pop the open-start key when it appears (payload is `{**started_payload,…}`
  so keys align).
- Key start→verdict matching on the start event's `seq` (carry it in the verdict/orphan payloads)
  instead of `(repo, branch, into)`, so a reused branch name cannot close an unrelated older orphan.
**Acceptance:** test: orphan an integrate (start with no verdict), run reconciliation twice with
landed merges between runs — trunk must not move on the second run. Mutation: remove
`integrate_orphaned` from the terminal set → test fails. Re-verify the existing `kill -9`
end-to-end test still passes.

### WP1.2 — Reaper: off the loop, immortal, clean shutdown (C4)
**Files:** `coordinator/reaper.py`, `server/app.py`, `blackboard/schema.sql`
- Wrap each pass in try/except-log-continue (log at WARNING with the exception; never die).
- Run `reap_once`/`decay_once` via `asyncio.to_thread` (dedicated connection stays on that thread).
- In lifespan shutdown, catch `Exception` (not just `CancelledError`) around `await task` so
  `conn.close()` always runs.
- Add index `(status, ttl_expires_at)` on `leases` (additive `CREATE INDEX IF NOT EXISTS` — no
  migration needed, but see WP3.4 for the version table).
**Acceptance:** test: reaper pass raises `sqlite3.OperationalError` once → next pass still runs and
reaps. Test: shutdown after a poisoned reaper still closes connections. Mutation: remove the
try/except → test fails.

### WP1.3 — TTL validation + clock assertion (C9, C13)
**Files:** `blackboard/models.py`, `server/leases.py`, `hooks/adapter.py`, `server/serve.py`
- `LeaseRequest.ttl` / `HeartbeatBody.ttl`: `gt=0`, ceiling (e.g. `le=86400`), floor documented as
  ≥ 2× busy_timeout (warn below it).
- `_hook_lease_ttl`: clamp to the same bounds; a nonsensical env value logs to stderr and uses the
  default rather than silently disabling protection.
- Startup assertion (serve path): SQLite `julianday`-epoch vs Python `time.time()` agree within a
  tolerance; refuse to start (with a clear message) if not.
**Acceptance:** test: `POST /lease` with `ttl=0` → 422, never `granted=True`. Test:
`SWARMSYNC_LEASE_TTL=0` in the hook env → default TTL used, note on stderr. Mutation: drop the
`gt=0` → test fails.

### WP1.4 — `/parcel/update` requires lease ownership (C5)
**Files:** `server/app.py` (or the SQL helper it delegates to), `blackboard/models.py` if needed
- Single-statement predicate: UPDATE succeeds only if an active, unexpired write/exclusive lease on
  `parcel_id` is owned by `body.agent_id` (same scoping shape as `heartbeat`). On failure return
  the endpoint's soft-fail idiom with a reason naming the actual holder.
- Decide and document the hook `postupdate` interaction: the adapter must tolerate an ownership
  refusal after its lease expired (log, don't crash the hook — fail-open discipline).
**Acceptance:** test: agent B updates a parcel A holds → refused, A's `content_hash` intact; test:
holder updates → succeeds. Mutation: drop the ownership predicate → test fails.

### WP1.5 — Same-agent lease idempotency in the hook (C8)
**Files:** `hooks/adapter.py`, optionally `server/leases.py`
- After a lost acquire in `cmd_precheck`: if `_find_holder(...) == agent_id`, treat as ALLOW and
  keepalive instead of denying.
- Preferred deeper fix (small, do it if clean): make `acquire` idempotent per `(parcel, agent,
  mode)` — a repeat request from the current holder returns `granted=True` refreshed, not a
  conflict.
**Acceptance:** test: two concurrent prechecks, same agent, same file → both allow, one lease row.
Mutation: remove the self-holder check → test fails (deny naming the agent itself).

### WP1.6 — Test-only hardening: the two undefended guards (A8, S3 / M-2)
**Files:** `tests/` only
- `BaseException` rollback: stub the gate to raise `KeyboardInterrupt` mid-integrate; assert trunk
  reset to `pre_merge_sha` and the exception re-raised.
- M-2: managed-root sibling-prefix test (`/managed` vs `/managed-evil`) asserting 403 — the test
  that finally kills the `+ os.sep` mutation survivor.
- Bonus (same shape): pin the three uncovered *reachable* `graph.py` branches — relative-import
  resolution, class signatures, vararg/kw-only rendering (A8).
**Acceptance:** mutations that previously survived (delete `+ os.sep`; delete the `BaseException`
handler) now fail the suite.

### WP1.7 — Remove the internal audit harness from the repo (U7)
**Files:** delete `scripts/audit-r4-workflow.js`
One-commit hygiene fix; no test. (Check `git log` for other internal-only stragglers while there.)

---

## Phase 2 — Hook-path integrity

*The Claude Code path is the product's real deployment mode and its weakest. WP2.1 and WP2.2 are
the load-bearing pair. All four parallel except where noted.*

### WP2.1 — Fix agent-identity collapse (C2)
**Files:** `hooks/adapter.py`
- **First**: capture a real Claude Code hook payload matrix (main session Edit, subagent Edit,
  SubagentStop) and record it as test fixtures — the finding's severity hinges on whether a
  per-subagent `agent_id` is present in current Claude Code. Reproduce before fixing (ground
  rule 3).
- If subagents share `session_id` with no `agent_id`: derive identity from the best per-subagent
  discriminator available in the payload (e.g. transcript path); if none exists, the precheck for
  a file already leased under the *same shared identity by another live tool-call* cannot be
  distinguished — in that case log loudly and document the limitation honestly instead of
  pretending (update ARCHITECTURE's hook-path consequences list).
- `cmd_release` on SubagentStop must release only leases the stopping identity holds.
**Acceptance:** fixture-driven tests for each payload shape; mutation: restore the bare
`session_id` fallback → test fails.

### WP2.2 — Parcel ids keyed to the managed root, not cwd (C12)
**Files:** `hooks/adapter.py`
- Resolve the repo root by walking up from `cwd` to the git toplevel (or query the server's
  managed root once and cache in the marker file); compute `_relpath` against that.
- Refuse (fail-open + loud stderr) when the resolved root disagrees with the server's managed root
  rather than minting ghost parcels.
**Acceptance:** test: payload with `cwd=repo/subdir` produces the same parcel id as one with
`cwd=repo`; test: two different-cwd agents on one file → second denied. Mutation: revert to raw
`cwd` → tests fail.

### WP2.3 — Timeout inversion + coordinated-mode fail-closed (C10)
**Files:** `hooks/adapter.py`, `blackboard/db.py` (constant only, if extracted)
- Raise hook client timeout above the server's `busy_timeout` (8–10s).
- Two-tier policy: server unreachable *and* no evidence of active coordination → fail open
  (unchanged); marker file present *and* recent successful contact recorded (stamp last-contact in
  the marker) → fail **closed** with a deny message telling the agent to retry, because silence
  during active coordination means contention, not absence.
**Acceptance:** test: timeout with fresh last-contact stamp → deny; timeout with no marker →
allow. Mutation: remove the two-tier branch → test fails.

### WP2.4 — Deny messages that inform (U3, part of A6)
**Files:** `server/leases.py`, `blackboard/models.py`, `hooks/adapter.py`
- `LeaseResult` gains `holder` and `ttl_expires_at` on the deny path (server already knows both;
  kills the adapter's second round-trip).
- Hook deny reason becomes: file, holder, TTL-remaining, renewal caveat ("renews while the holder
  is active"), and one actionable pointer (`GET $SWARMSYNC_URL/leases`, or `swarmsync holds` once
  WP5.1 lands).
- Drop "retry shortly" — it is wrong (hook leases renew on every precheck).
**Acceptance:** golden-string test on the deny reason; response-model test for the new fields.
Depends on: nothing (coordinate the `LeaseResult` change with WP4.5's response-model pass — land
this first, it's a superset seed).

---

## Phase 3 — Bound every resource

### WP3.1 — Events: clamp, retain, compact (S2, C3-adjacent)
**Files:** `server/app.py`, `server/events.py`, `coordinator/reaper.py`
- Clamp `GET /events?limit=` (e.g. `le=1000` in the query validator).
- Retention: periodic compaction (piggyback the reaper cadence) deleting heartbeat/keepalive-class
  events older than a window and any event older than a configurable horizon, **except** events the
  recovery path needs (`integrate_started` without a terminal — see WP3.2 — survive
  unconditionally). Emit a `events_compacted` marker with the pruned range.
**Acceptance:** test: limit clamp; test: compaction preserves open `integrate_started` rows and
lease-audit tail; mutation: drop the preserve-open-integrations predicate → recovery test from
WP3.2 fails.

### WP3.2 — Reconciliation from a projection, not a full-log replay (C3)
**Files:** `coordinator/integrator.py`, `blackboard/schema.sql`
- Replace the `limit=1_000_000` oldest-first scan with an `open_integrations` projection table:
  INSERT on `integrate_started`, DELETE on terminal (including `integrate_orphaned` — WP1.1), in
  the same transaction as the event emit (transaction helper exists: `db.transaction`).
- Startup reconciliation reads the projection (O(open), not O(history)) off the event loop.
**Acceptance:** test: reconciliation correctness with an events table larger than the old window
(synthesize via direct inserts, don't generate 1M real events); the WP1.1 double-restart test
re-run against the projection. Depends on: WP1.1.

### WP3.3 — Quota the auto-create paths (S1, S5)
**Files:** `server/leases.py`, `server/app.py`
- Per-agent cap on active leases and on `ensure_parcel`-minted parcels (config knob, generous
  default); shape/length validation on parcel ids accepted via `ensure_parcel`.
- Add a request body-size middleware cap (S5) — one middleware, config knob, generous default.
**Acceptance:** test: agent at cap → lease denied with a reason naming the cap; test: oversized
body → 413. Mutations on both predicates.

### WP3.4 — Schema version + honest recovery contract (C7)
**Files:** `blackboard/db.py`, `blackboard/schema.sql`, `schema.sql` docs, `DESIGN.md`, `ARCHITECTURE.md`
- `schema_version` table; `init_db` stamps it; on open, a mismatched version refuses with a clear
  message (delete-and-reindex is the documented remedy at this maturity — a real migration
  framework is explicitly out of scope).
- **Demote the replay claim** (option the audit recommends): events = append-only audit log;
  SQLite tables = source of truth; the one recovery-critical read path is the WP3.2 projection.
  Rewrite `schema.sql:3-4` and the DESIGN/ARCHITECTURE passages that repeat it. Add the missing
  emits that are cheap and useful as *audit* records (`reindexed` on `POST /index`) — but as
  audit, not as truth.
**Acceptance:** test: opening a DB with a wrong version refuses cleanly. Grep-level doc check: no
remaining "replayable from events" claim.

### WP3.5 — Retire ghost parcels on re-index; park rejected branches (C14, C6-interim)
**Files:** `classifier/store.py`, `coordinator/integrator.py`, `agent/runner.py`, `worktree/git_ops.py`
- Post-land re-index retires parcels/contracts whose path/symbol vanished: close their
  leases/pheromone rows first (FK), then delete, emit `parcel_retired` per parcel.
- `_cleanup_worktree` renames kept rejection branches to `rejected/<task>-<attempt>-<ts>`;
  `_prune_stale_worktree` never deletes `rejected/*`. (Full resubmit is WP6.1; this just stops the
  data loss.)
- Adapter postupdate on a deleted file: post a tombstone update (or explicit `parcel_retired`
  request) instead of returning early with stale hash advertised.
**Acceptance:** test: rename a file, integrate, old parcel gone from `/parcels`, old contract 404s;
test: rerun after rejection → `rejected/*` branch still exists, commits reachable. Mutations on
both.

### WP3.6 — Broker/runner failure containment (C11) + DB lifecycle (U8)
**Files:** `agent/runner.py`, `coordinator/broker.py`, `server/serve.py`
- `run_agent`: `except Exception` path releases all held leases and returns an
  `AgentResult(status="error", …)`; the finally keeps its current duties.
- Broker: per-task exception → error result recorded, run continues; optional single retry for
  `GitOpsError` (transient git lock contention is real under worktree concurrency).
- `swarmsync-serve --fresh` (backup-and-recreate the DB); default DB path keyed to the managed
  root (e.g. under `$XDG_RUNTIME_DIR/swarmsync/<root-hash>.db`) instead of cwd-relative, killing
  the stale-cross-repo-DB hazard.
**Acceptance:** test: mutator raises mid-run → leases released, other tasks complete; test:
`--fresh` produces empty schema, old file preserved as backup. Mutations on the release path.

---

## Phase 4 — Architecture consolidation

*Refactors land after the correctness base is green. WP4.1 → WP4.2 have a soft order (config move
touches launcher files WP4.1 relocates docstrings in). Everything else parallel.*

### WP4.1 — Move `events.py` + `leases.py` into `blackboard/` (A2)
**Files:** `swarmsync/blackboard/{events,leases}.py` (new homes), `server/{events,leases}.py`
(shims), all importers
- Mechanical move; re-export shims in `server/` for one release with a deprecation comment.
- Result invariant to assert in a test (import-linter style or a simple AST walk in a test):
  `blackboard` imports nothing from `server`/`coordinator`/`agent`; `coordinator` no longer
  imports from `server` except `app.py`'s legitimate front-door usage.
**Acceptance:** the import-direction test; full suite green (100% coverage on both files is the
safety net).

### WP4.2 — One config module, one launcher (A4, U9-partial)
**Files:** new `swarmsync/config.py`, `server/app.py`, `server/serve.py`, `coordinator/integrator.py`,
`agent/client.py`, `hooks/adapter.py`, `pyproject.toml`, `README.md`
- `config.py`: typed accessors (`db_path()`, `token()`, `gate_timeout()`, `lease_ttl()`, `url()`,
  `roots()`, `active()`); all seven env vars read in one file; `SWARMSYNC_DB` accepted with
  `SWARM_SYNC_DB` as deprecated alias (stderr warning).
- Kill the launcher split: `swarm-sync` console script becomes an alias of `swarmsync-serve`'s
  main (same port, same DB default, same banner). `serve.py` stops mutating `os.environ`.
- README gains the single env-var table (all 7 knobs, one line each — `SWARMSYNC_LEASE_TTL` gets
  documented for the first time).
**Acceptance:** test: both console scripts resolve to the same behavior/defaults; test: deprecated
var warns and works; grep: no `os.environ` reads outside `config.py` (as a test). Mutation: drop
the alias fallback → test fails.

### WP4.3 — Split the integrator (A3)
**Files:** `coordinator/integrator.py`, new `coordinator/gate.py`
- Extract `run_impact_tests` + `_kill_process_group`/stream-drain machinery into `gate.py`;
  extract the contract-diff block into a named helper. `integrate()` remains the orchestration
  narrative; target ≤ 450 LOC for `integrator.py`, no behavior change.
- Prune the build-history prose in what you touch (ground rule 4).
**Acceptance:** pure refactor — suite green, coverage not down, `radon cc` for `integrate` ≤ C,
no new module-level state. The WP1.6 `BaseException` test must still pass (it pins the safety
path across the seam).

### WP4.4 — Parcel-id helpers + duplication sweep (A5)
**Files:** new `blackboard/parcel_id.py` (or functions in `models.py`), `classifier/indexer.py`,
`classifier/graph.py`, `coordinator/broker.py`, `hooks/adapter.py`, `server/… leases`, `agent/runner.py`,
`agent/client.py`
- `MODULE_SYMBOL` defined once; `module_id(path)`, `split(parcel_id)` used at all six-plus sites.
- Delete `_find_holder`-vs-`_find_lease` duplication; replace `result["_status_code"]` smuggling
  with a small typed result.
**Acceptance:** grep-as-test: exactly one definition of `<module>` literal; suite green.

### WP4.5 — API shape: typed responses, one convention (A6)
**Files:** `server/app.py`, `blackboard/models.py`, `hooks/adapter.py`
- `response_model=list[Lease]` / `ParcelWithLeases` on `GET /leases` / `GET /parcels` (models
  exist; adapter switches from duck-typed dicts to the declared shape).
- Unify unknown-entity handling on the soft-fail `{"ok": false, "reason": …}` idiom (matches the
  fail-open philosophy); `/parcel/update`'s 404 becomes soft-fail. Document the convention in
  DESIGN's endpoint table.
**Acceptance:** OpenAPI schema snapshot test (the wire contract is now asserted, not implied);
adapter tests against the typed shapes. Depends on: WP2.4 (LeaseResult fields).

### WP4.6 — Small consistency debts (A7, A8-logging, C15, C16, C17, A1-decision)
**Files:** `coordinator/broker.py`, `agent/runner.py`, `server/events.py`, `coordinator/integrator.py`,
`blackboard/models.py`
- Broker: per-thread `db.connect()` in `_run_task_with_retries`; retire the shared-connection
  discipline and its scar-tissue comments.
- `logger.debug` in the heartbeat thread and worktree-cleanup silent excepts.
- `decay_pheromone` → single-statement SQL UPDATE (closes the C15 race).
- Event `ts` captured per-emit, not per-`integrate()`-call (C16).
- Runner "read the world" fetches the newest events (`since = last_seq - N`), not `since=0` (C17).
- **A1 decision — wire `needs_rebase`** (recommended over delete: the mechanism is tested and the
  wiring is one field): add `expected_read_deps: dict[str, str] | None` to `IntegrateBody`;
  `run_agent` passes its `contract_snapshot`. If Phase 6 instead decides to trim contracts
  entirely, this WP flips to the delete option — check the Phase 6 decision state before starting.
**Acceptance:** each item mutation-tested per ground rule 1; the A1 wiring gets an end-to-end
test: change a read-dep between plan and submit → `needs_rebase` over real HTTP.

---

## Phase 5 — Operability & adoption

### WP5.1 — `swarmsync` operator CLI (U2, U5)
**Files:** new `swarmsync/cli.py`, `pyproject.toml` (console script), `server/app.py` (`/health`)
- `/health` endpoint: `{root, db_path, active_leases, last_event_seq, version}`.
- `swarmsync status` — server up? managed root? active leases with holder + human TTL-remaining;
  last N events summarized.
- `swarmsync holds` / `swarmsync free [paths…]` — the work-discovery surface agents can Bash-call
  (U5); wire its name into the deny message (WP2.4's pointer).
- `swarmsync events --follow` — tail via `?since=seq` polling.
- One httpx + formatting module; no new server state beyond `/health`.
**Acceptance:** CLI integration tests against a live test server (the `test_serve.py` harness
pattern); `/health` in the OpenAPI snapshot.

### WP5.2 — `swarmsync doctor` + `init-hooks` (U4, U9)
**Files:** `swarmsync/cli.py`, `hooks/adapter.py` (marker/last-contact from WP2.3)
- `doctor`: server reachable at `SWARMSYNC_URL` · root matches cwd's git toplevel · marker file
  present/fresh · hooks wired in settings.json · DB writable · version match. Each check prints
  pass/fail + the remedy (the MultiRootError message is the house style to copy).
- `init-hooks`: writes the settings.json hook block (idempotent, `--dry-run`), drops the marker.
**Acceptance:** doctor detects each seeded failure in tests (server down, wrong root, missing
hooks, stale DB); init-hooks round-trips on a fixture settings.json. Depends on: WP2.3, WP5.1.

### WP5.3 — Docs & packaging honesty pass (U1, U6, U9, A4-docs)
**Files:** `README.md`, `docs/` or `.claude/skills/swarmsync/`, `requirements.txt` (delete),
`demo/run_demo.py` or README block, `pyproject.toml`
- Quickstart: `python3.11 -m venv` + fail-fast version preflight as the first fenced command (U1 —
  the 8-minute silent hang is the single worst first-run outcome).
- Ship the swarmsync skill in-repo, paths parameterized; link from the Claude Code section (U6).
- Sync README sample output with actual demo output; delete `requirements.txt`; mention `/docs`
  (Swagger); wrap broker-client connect errors with the "is the blackboard running?" one-liner.
- Publish `0.x` to PyPI (or at minimum claim the name) (U9).
**Acceptance:** a fresh-clone quickstart walkthrough on a `python3`=3.10 machine fails fast with
the version message (test the preflight in CI by faking the version); README/demo output diff
clean.

---

## Phase 6 — Strategic functionality

*Do these with Phase 5's observability in place — the contract question (AUDIT_R6 §6) should be
answered by watching real sessions, not by argument. Sequenced, not parallel: each changes the
ground the next stands on.*

### WP6.1 — Rebase-and-resubmit (C6, DESIGN §5.5's unkept promise)
**Files:** `coordinator/broker.py`, `agent/runner.py`, `worktree/git_ops.py`, `coordinator/integrator.py`
- On `needs_rebase` / `merge_rejected(merge_conflict)`: rebase the parked branch (WP3.5) onto
  current trunk in a scratch worktree; clean rebase → resubmit (bounded attempts, e.g. 2);
  conflicted rebase → surface to the task's owner as a structured failure, never silent.
- Broker treats resubmit-exhausted as a first-class terminal result distinct from "done".
**Acceptance:** end-to-end: two agents, conflicting-adjacent edits, loser rebases and lands; test
for bounded-attempts exhaustion. Effort L — one subagent, but budget accordingly.

### WP6.2 — Answer the contract question (AUDIT_R6 §6, ROUND5 P1-7/8/9)
**Deliverable:** a decision memo (in-repo), then the implementing WPs it spawns.
- Inputs: WP4.6's now-live `needs_rebase` behavior in real sessions; `swarmsync status` data on
  how often contracts would have fired; the SYMBOL_MODE_DESIGN staged plan; ROUND5's warning that
  ROUND4 §1 option (a) rests on a wrong model of git 3-way merge (re-derive it — the merge unit is
  one unchanged line, not three lines of context).
- Outcome A (contracts are load-bearing): schedule exclusive-mode Stage 1 (the CAS already parses
  modes; make `exclusive` conflict with reads), then span containment, per SYMBOL_MODE_DESIGN.
- Outcome B (decoration): delete the contract-freeze machinery, `exclusive` mode, and the frozen
  auto-upgrade path in broker/runner; retarget the docs. Smaller, honest system.
- Either outcome also resolves ROUND5's leaf-symlink policy question (S4) in writing.

### WP6.3 — Multi-language classifier (ARCHITECTURE "good places", only after WP6.2)
Tree-sitter backend behind the existing `indexer.py` interface, gated to Outcome A-or-B's actual
parcel granularity. Out of scope until the contract question is settled — indexing more languages
into an abstraction that might be deleted is waste.

---

## Suggested execution rhythm

Per phase: launch the phase's parallel WPs as independent subagents with disjoint write-footprints
(the **Files** lines above are the lease map); one integrator-of-record reviews each WP against its
acceptance criteria *including running the mutation*; phase gate = ground-rule 2 plus a
whole-phase demo run. After Phases 1–3, cut a tag — that's the "trustworthy core" milestone. After
Phase 5, the project stops being a demo. Phase 6 is where it becomes interesting again.
