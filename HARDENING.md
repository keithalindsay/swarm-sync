# Hardening log

Correctness/security fix passes on top of the overnight build. Each entry: what
was wrong, what changed, and how a regression test pins it.

## S1-integrate-serialize (P0)

**Defect.** `POST /integrate` ran unserialized. FastAPI dispatches the sync
handler on Starlette's threadpool, so N concurrent `/integrate` requests raced
on the single shared `integration` checkout -- interleaved `git checkout`/`git
merge --no-ff`/`git reset --hard` plus `.git/index.lock` collisions. Result:
requests bubbled a 500 (`GitOpsError: MERGE_HEAD exists`), clean file-disjoint
branches were spuriously rejected or silently dropped, and trunk could be left
dirty / mid-merge. Separately, `integrate()` was not atomic: any failure AFTER
the merge commit landed (a later `GitOpsError`, a parse/re-index crash) bubbled
out as a 500 leaving a half-integrated, un-reindexed merge sitting on trunk.

**Fix.**
- `swarmsync/server/app.py`: added a process-wide `asyncio.Lock`
  (`app.state.integrate_lock`); `post_integrate` is now `async` and does
  `async with integrate_lock: await run_in_threadpool(integrator.integrate, ...)`.
  At most one merge touches the shared checkout at a time; waiters queue on the
  lock without tying up threadpool workers.
- `swarmsync/coordinator/integrator.py`: made `integrate()` atomic. Once the
  merge commit is on trunk, the pytest gate + re-index + summary regen +
  contract detection run inside one guard; any failure `git reset --hard`s trunk
  back to `pre_merge_sha` (byte-identical) and returns a structured
  `merge_rejected` (reason `integration_error` / `merge_error`) instead of a
  500. Success-path events (`merged`/`contract_change`/`reindexed`) are now
  emitted only after that guarded block succeeds, so a rolled-back merge never
  leaves a dangling `merged` event in the replay log. A non-conflict
  `GitOpsError` from the merge itself is also caught and surfaced structurally.

**Regression test.** `tests/test_integrate_serialize.py` fires N=6 concurrent
`POST /integrate` for N independently committed, file-disjoint branches at a
live `create_app()`/`TestClient` (threads released together via a barrier) and
asserts: every request is a structured 200, all N come back `merged`, trunk is
not dirty / mid-merge, every branch's file is present with its committed
content, exactly N `merged` events exist, and trunk's full suite is green.
Verified to FAIL on the pre-S1 source (raced `GitOpsError` 500) and PASS after.

Full suite: 194 passed. ruff clean.

## S2-atomic-primitives (P0/P1)

Four disjoint-file atomic/injection fixes. Each ships a regression test verified
to FAIL on the pre-S2 source and PASS after.

**(a) `coordinator/reaper.py` -- reap TOCTOU vs a renewing heartbeat.**
`reap_once` did `SELECT` the expired-lease ids, then a per-row `UPDATE ... WHERE
id=? AND status='active'`. That second statement re-checked only `status`, never
the ttl, so a heartbeat that renewed a lease's `ttl_expires_at` in the window
between the SELECT snapshot and the UPDATE would still be reaped -- yanking a
parcel out from under an agent that was demonstrably still alive. *Fix:* one
atomic statement `UPDATE leases SET status='reaped' WHERE status='active' AND
ttl_expires_at<=:now RETURNING id,agent_id,parcel_id` (rows sorted by id for the
documented ordering) -- the timeout predicate is now evaluated at write time, so
a just-renewed lease fails the WHERE and is left active. *Regression*
(`tests/test_reaper.py::test_reap_once_excludes_lease_renewed_by_heartbeat_in_the_race_window`):
a `_RaceConn` proxy fires a real `heartbeat` renewal inside the reaper's critical
window (after the old SELECT / before the new atomic UPDATE); asserts the renewed
lease stays `active` and emits no `reaped` event.

**(b) `server/events.py` -- `emit` returned a raced seq.** `return cur.lastrowid`
reads `sqlite3_last_insert_rowid()`, a per-CONNECTION value fetched after the GIL
is re-acquired post-`step`; under concurrent emits on the one shared connection a
sibling INSERT lands in that window and two distinct events report the same seq,
corrupting the replay log's identity. *Fix:* `INSERT ... RETURNING seq` +
`fetchone()["seq"]` (mirrors `leases.acquire`). *Regression*
(`tests/test_events.py::test_concurrent_emits_return_distinct_monotonic_seqs`): 8
threads x 80 emits behind a barrier; asserts the returned seqs are a clean
bijection with the persisted rows (old form yields >100 duplicate seqs here).

**(c) `classifier/indexer.py` -- one malformed file aborted the whole index.**
`index_repo`'s `parse_file` call was unguarded, so a single `.py` with a syntax
error or invalid encoding propagated `SyntaxError`/`UnicodeDecodeError`/`OSError`
and took the entire parcel map down. *Fix:* wrap the per-file parse in
try/except (OSError, SyntaxError, UnicodeDecodeError) -> skip-and-log (module
`logger.warning`), matching `build_graph`'s existing per-file guard. *Regression*
(`tests/test_indexer.py::test_index_repo_skips_malformed_file_and_indexes_the_rest`):
a repo with a broken-syntax and an invalid-UTF-8 file plus two good files still
indexes both good files, contributes no parcels for the broken ones, and logs a
skip for each.

**(d) `worktree/git_ops.py` -- git argument injection via leading-'-' refs.**
`shell=False` does not stop git from parsing a user-derived argv entry that
begins with '-' as an OPTION: a branch literally named `--upload-pack=<cmd>` (or
`--output=...`) was handed straight to `git merge`/`worktree add`/etc. and
executed as a flag. *Fix:* `_reject_option_like` refuses any user-derived
ref/branch/path starting with '-' before any git process spawns, and all
user-derived positionals are fenced with `--end-of-options` (merge, diff,
branch -D) or `--` (checkout, reset, worktree add/remove) separators. `git
rev-parse` is guarded by rejection only -- it echoes `--end-of-options` onto
stdout, which would corrupt the parsed sha. *Regression*
(`tests/test_git_ops.py::test_merge_branch_rejects_option_like_branch_before_running_git`
+ parametrized siblings): a `--upload-pack=...` branch raises `GitOpsError`
("begins with '-'") with `_run` monkeypatched to prove not one git subprocess ran
(old code reached git: `error: unknown option 'upload-pack=...'`); a normal
branch still merges cleanly.

Full suite: 205 passed. ruff clean (source package; the 7 pre-existing test-lint
findings are unchanged baseline, none introduced by S2).

## S3-security (P0/P1)

Security hardening across `server/app.py`, `classifier/indexer.py`,
`coordinator/integrator.py`, and `hooks/adapter.py`. The blackboard is a service
a stranger runs and that gates real editing sessions, so it must not be an open,
network-reachable, path-unrestricted RCE surface.

**(1) Optional token auth on mutating routes.** When `SWARMSYNC_TOKEN` is set,
every *mutating* (POST) route -- `/index /intent /lease /heartbeat /release
/parcel/update /integrate` -- requires `Authorization: Bearer <token>`, compared
in constant time with `hmac.compare_digest` (`require_token` FastAPI dependency).
When unset, no auth (dev/test/demo and every pre-S3 test keep working headerless).
Read-only GET routes stay open. The token is the trust boundary (WHO may reach the
blackboard); `agent_id` is untouched and remains the in-session coordination
identity per DESIGN §4.3. `hooks/adapter.py`'s `_default_http_factory` now sends
the token as a default bearer header when set, riding along on both the
`BlackboardClient` calls and `cmd_session_start`'s direct `POST /index`.

**(2) Managed-root path allow-list.** `POST /index` (`root`) and `POST /integrate`
(`repo`) `os.path.realpath` the caller-supplied path and require it under a managed
root (`SWARMSYNC_ROOTS`, os.pathsep-separated, default = server launch cwd) --
`_validate_managed_path` returns 403 otherwise. realpath resolves symlinks, so a
link that lives inside a managed root but points outside is caught. `/integrate`
validates before any git process spawns. `classifier/indexer.index_repo` grew a
bounded walk (`max_files`/`max_seconds`, defaults 5000/30s, raising the new
`IndexLimitError`); `POST /index` maps that to 413. A new `tests/conftest.py`
autouse fixture points `SWARMSYNC_ROOTS` at the system temp root for the suite
(the legitimate operator move) so every tmp_path/demo repo the tests build is a
valid root.

**(3) main() binds localhost + argparse.** `server/app.py:main()` now binds
`127.0.0.1` by DEFAULT (was `0.0.0.0`) and takes `--host/--port/--db` mirroring
`serve.py`. `swarmsync-hook` prints usage on `--help`/`-h`.

**(4) Sandboxed integrator pytest gate.** `run_impact_tests` runs the gate against
the just-merged untrusted branch with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` in the
env and `-p no:cacheprovider --import-mode=importlib` on the argv, so third-party
plugin/conftest autoloading can't execute arbitrary code in our environment and
imports don't mutate the parent's `sys.path`/module namespace.

**Regression tests** (`tests/test_security.py`, 17 cases, each fails on pre-S3
source): with `SWARMSYNC_TOKEN` set, `POST /index` without/with-wrong bearer is
401 and only the correct token is 200 (pre-S3 had NO auth -> was 200); GET stays
open; token-unset stays open; `POST /index`/`/integrate` on an outside path or a
symlink escaping `SWARMSYNC_ROOTS` is 403 (pre-S3 walked/merged any host path);
`index_repo` raises `IndexLimitError` past its file/time caps and the endpoint
maps it to 413; `main()` binds `127.0.0.1` by default (pre-S3 `0.0.0.0`) and
`--host` overrides it, `--help` exits 0; the adapter's default factory sends the
bearer header iff the token is set; plus an end-to-end token-gated
index/lease/release round-trip.

Full suite: 222 passed (205 -> +17). ruff clean (source package + the two new
test files; the pre-existing test-lint baseline is unchanged, none introduced by
S3).

## S4-connection-model (P0/P1)

**Defect.** Every FastAPI request handler shared ONE process-wide SQLite
connection (`app.state.conn`, wired via `get_conn`), and so did the async reaper
task. Two failures follow:

- *WAL concurrency thrown away.* WAL's "one writer, many concurrent readers" only
  holds across *separate* connections. Funneling every threadpool request handler
  (plus the reaper) through a single shared handle serialized all DB work on that
  one connection -- readers could not proceed while any other request touched the
  DB. The DESIGN §4 promise ("concurrent readers with one writer") was not being
  delivered.
- *Cross-request transaction swallow (correctness).* SQLite has exactly ONE
  transaction per connection. `classifier/store.py`'s `upsert_parcels` /
  `upsert_contracts` did `conn.execute("BEGIN") ... COMMIT/ROLLBACK` on the shared
  connection. Any *other* handler's (or the reaper's) single-statement write that
  landed on that same connection while a `run_index` batch's `BEGIN` was open was
  folded into that open transaction -- and a subsequent `ROLLBACK` (a bad parcel,
  a re-index crash) silently swallowed the unrelated, already-"committed"-looking
  write, corrupting the leases/events/reaped state it thought it had persisted.

**Fix.**
- `swarmsync/server/app.py`: `get_conn` is now a per-request generator dependency
  that opens its OWN connection (`db.connect(app.state.db_path)`) and closes it in
  `finally`. Concurrent requests therefore run on independent connections -- WAL
  delivers real reader concurrency + a single writer, and one request's
  transaction can never enclose another's writer. FastAPI runs a sync generator
  dependency's setup/teardown around the handler *sequentially*, so the connection
  moves between threadpool workers within one request without ever being touched by
  two threads at once (safe with `check_same_thread=False` + SQLite serialized
  mode). The background reaper gets its own dedicated connection in the lifespan
  (closed on shutdown). `app.state.conn` survives only as an out-of-band inspection
  handle for tests/tools and the broker's single-writer entry point (U12's
  `ThreadPoolExecutor`-over-one-connection model is unchanged and out of scope --
  it deliberately relies on `sqlite3.threadsafety == 3`; the server now shares
  *less*, never more, so it is strictly safer).
- `swarmsync/blackboard/db.py`: new `transaction(conn)` context manager --
  `BEGIN IMMEDIATE` (write lock taken up front, so two writers can't deadlock
  upgrading a read lock under WAL; the loser waits on `busy_timeout`), `COMMIT` on
  success / `ROLLBACK` + re-raise on any exception, and an `in_transaction` guard
  that refuses to nest (the "one transaction per connection" invariant made
  explicit). `classifier/store.py`'s two batch upserts now go through it instead of
  hand-rolled `BEGIN`/`COMMIT`/`ROLLBACK`.

**Regression tests** (`tests/test_connection_model.py`, 5 cases; the first two
verified to FAIL on the pre-S4 shared-connection `get_conn` and PASS after):
- `test_concurrent_requests_use_distinct_connections` -- two requests rendezvous on
  a `threading.Barrier(2)` inside their handlers (proving both are in flight at
  once) and each records `id(conn)`; asserts the two connections differ. Pre-S4
  both got the one `app.state.conn` -> identical id -> fails.
- `test_reader_does_not_see_a_writers_uncommitted_row` -- the requested
  readers-while-writing test: one request holds an OPEN write transaction with an
  uncommitted sentinel parcel while a concurrent `GET /parcels` reads; asserts the
  reader does NOT observe the uncommitted row (WAL snapshot isolation across
  connections), and that after the writer's rollback it exists nowhere. Pre-S4 the
  reader shared the writer's connection, read inside its open transaction, and saw
  the uncommitted sentinel -> fails.
- `test_transaction_rollback_does_not_swallow_a_writer_on_another_connection`,
  `test_transaction_refuses_to_nest`, `test_transaction_commits_on_success` --
  pin `db.transaction`'s cross-connection isolation + no-nesting + commit contract.

Re-ran the S1/S2 concurrency regressions (`test_integrate_serialize`,
`test_reaper`, `test_events`, `test_leases`, `test_broker`) under the new
connection model: all pass unchanged.

Full suite: 227 passed (222 -> +5). ruff clean (source package + the new test
file; pre-existing test-lint baseline unchanged, none introduced by S4).

## S5-product-robustness (P1)

Product-surface robustness across `hooks/adapter.py`, `agent/runner.py`,
`coordinator/integrator.py`, and `worktree/git_ops.py` (plus the small
`heartbeat`-TTL plumbing the keepalive needs). Each fix ships a regression test
verified to FAIL on the pre-S5 source and PASS after.

**(a) `hooks/adapter.py` -- lease keepalive (the shipped "one-agent-per-file"
promise).** The hook acquired a lease on precheck and never renewed it; the
server's default lease TTL is 30s, but normal think time between edits (and even
a single big edit generated between a precheck and its postupdate) routinely
exceeds that, so the reaper silently expired a still-active agent's lease and
another agent could grab the file mid-session. *Fix:* the hook now acquires (and
renews) with a long, configurable TTL (`SWARMSYNC_LEASE_TTL`, default 300s) and
refreshes it via `POST /heartbeat` on EVERY precheck (for a lease it already
holds) AND every postupdate. To make a renewed lease keep the long window rather
than collapse to the 30s server default, `HeartbeatBody`/`POST /heartbeat`/
`BlackboardClient.heartbeat` gained an optional `ttl` (backward compatible:
`None` -> old default). *Regressions* (`tests/test_hook_adapter.py`):
`test_precheck_refreshes_ttl_on_own_held_lease` and
`test_postupdate_refreshes_ttl_on_own_held_lease` seed a short-TTL lease and
assert each hook call bumps `ttl_expires_at` on the SAME lease id (pre-S5: no
heartbeat -> unchanged);
`test_keepalive_prevents_expiry_across_a_ttl_window` seeds a 0.4s lease and drives
6 postupdates over ~0.9s (a window >1 TTL), asserting the lease is still active,
still that agent's, and a different agent is still denied (pre-S5: the lease
expired one TTL in and the other agent could acquire).

**(b) `hooks/adapter.py` -- unparseable edit no longer a silent no-op.**
`cmd_postupdate`'s `parse_file` was unguarded, so an edit that left the file
syntactically invalid raised `SyntaxError` up into `main()`'s fail-open umbrella
-> nothing posted -> the blackboard kept advertising the STALE last-good
content_hash, hiding that the file is now dirty. *Fix:* catch
`(SyntaxError, UnicodeDecodeError, ValueError)` and push a raw whole-file
`sha256` content_hash + a deterministic `DIRTY/UNPARSEABLE (<ExcName>)`
state_summary, so the parcel's hash genuinely changes and the marker flags it as
un-indexable. *Regression*
(`test_postupdate_pushes_dirty_marker_when_edit_is_unparseable`): a broken-syntax
edit changes the parcel's content_hash off the last-good value and stamps the
marker (pre-S5: hash unchanged).

**(c) `hooks/adapter.py` -- symlink policy reconciled with the indexer.**
`_relpath` did `abs_p.resolve()`, following a leaf symlink to its target; but
`classifier.indexer.index_repo` records an indexed symlinked `.py` under its own
on-disk name. So an edit to a symlinked file mapped to a different parcel id than
the indexer registered -- and if the target resolved outside the repo, to `None`,
silently BYPASSING the lease entirely. *Fix:* resolve only the PARENT dir
(canonicalizes `..` / symlinked parent dirs, so a real escape still returns None)
and KEEP the leaf name, matching the indexer's mapping. *Regression*
(`test_indexed_symlinked_py_stays_leasable`): a `.py` symlink pointing OUTSIDE
the repo is indexed under its in-repo name; the hook's precheck now acquires a
lease on that same `linked.py::<module>` parcel (pre-S5: `_relpath` -> None -> no
lease acquired at all).

**(d) `agent/runner.py` + `worktree/git_ops.py` -- worktree/branch cleanup +
idempotent add.** `run_agent` created `.worktrees/<agent_id>` + branch
`<agent_id>` and never removed them, leaking one per run and making a rerun with
the same agent_id collide on `git worktree add -b` ("branch/path already
exists"). *Fix:* `run_agent` now tears the worktree + branch down in a `finally`
after integrate/release on the done path (and on any mid-way failure), and best-
effort on the lease_denied path; a landed merge's commits already live in trunk
history, so deleting the branch ref loses nothing. `git_ops.add_worktree` is now
idempotent -- `_prune_stale_worktree` removes a leftover same-named worktree
(registered, orphaned-dir, or stale-admin-entry) + branch before adding; its
`git worktree prune` only drops MISSING-dir entries, so a concurrently-added
sibling worktree (broker waves) is never affected. *Regression*
(`tests/test_agent.py::test_run_agent_cleans_up_worktree_and_branch_and_rerun_is_idempotent`):
after one run the worktree dir + branch are gone, and a second run under the SAME
agent_id succeeds (pre-S5: worktree/branch leaked -> assert fails; second run
raised `GitOpsError`). Two existing U9 tests that read the (now-removed) worktree/
branch after `run_agent` were updated to assert the durable trunk state + the
`merged` integrate verdict instead.

**(e) `coordinator/integrator.py` -- impact-test selection no longer skips an
affected test.** `run_impact_tests` selected test files by bare-stem SUBSTRING
match, which skips a test that exercises a changed module only INDIRECTLY (it
imports M which imports the changed C, and never names C textually) while still
selecting a directly-naming test -- so no full-suite fallback fired and the
affected test never ran. *Fix:* selection is now the UNION of (i) the
classifier's real dependency-graph TRANSITIVE reverse-deps of the changed
parcels mapped back to their test files (`_reverse_dep_files`: re-index the
merged repo, BFS `reverse_edges` from every changed parcel) and (ii) the old
substring heuristic (kept as a backstop for classifier misses -- dynamic
dispatch / string imports), with the full-suite fallback when nothing matches. A
strict over-approximation of the old behavior: it can only run MORE tests, never
fewer. *Regression*
(`tests/test_integrator.py::test_impact_selection_runs_a_transitively_affected_test`):
`test_mid` transitively exercises the changed `base.core()` via `mid.use()` but
never names `base`; a co-present `test_base` DOES name `base` (suppressing the
full-suite fallback). The change breaks `test_mid` but not `test_base`, so the
old selector merged a broken change; the new selector runs `test_mid`, catches
the break, and returns `merge_rejected`.

Also stabilized a pre-existing flaky S2/S4-era test
(`test_reaper.py::test_reaper_is_wired_into_app_lifespan_and_reaps_expired_leases`,
~1/3 failures in isolation, independent of every file S5 touches): it polled the
intermediate lease `status='reaped'` on a separate inspection connection, which
can be observed before the reaper's subsequently-emitted `reaped` event lands --
now it polls the asserted end-state (the event). Product code untouched.

Full suite: 234 passed (227 -> +7). ruff clean (source package; the pre-existing
test-lint baseline -- E741 `l`, F841 `client` in earlier suites -- is unchanged,
none introduced by S5).

## S6-tooling-tests (P2)

Tooling + the remaining regression coverage. No product-code behavior change --
this pass adds lint/type gates, fixes what they flagged, fills test gaps, and
cuts the demo suite's redundant runtime.

**Tooling.** `pyproject.toml` gained `ruff`+`mypy` in the `dev` extra plus
`[tool.ruff]` (line-length 100) and `[tool.mypy]` (python 3.11,
`files=["swarmsync"]`, `ignore_missing_imports`, `warn_unused_ignores`) sections.

**mypy: 13 errors -> clean** (`.venv/bin/mypy swarmsync/`), all real narrowings,
zero blanket ignores:
- `classifier/indexer.py` (4): `_decorated_start`/`_node_span` typed their node
  param `ast.AST`, which declares no position attrs. Retyped to `ast.stmt` (every
  caller passes FunctionDef/AsyncFunctionDef/ClassDef) and `assert`ed the
  `end_lineno`/`end_col_offset` invariant (typeshed types them Optional; `ast.parse`
  always populates them).
- `classifier/graph.py` (1): a `for node in tree.body` (`stmt`) loop var was reused
  by a later `for node in ast.walk(tree)` (`AST`) -> rename the first to `def_node`.
- `agent/client.py` (1): `httpx.Client` satisfies the duck-typed `_HttpLike`
  protocol at runtime but not nominally (keyword-only signatures) -> `cast`.
- `hooks/adapter.py` (3): coalesce a possibly-`None` lease owner before
  `_deny_response(str)`; `assert` the parse-derived `content_hash` non-None before
  `parcel_update(str)`; annotate `factory: HttpFactory` so the `Any`-returning
  factory type flows to `BlackboardClient`.
- `agent/runner.py` (2): `assert match.content_hash is not None` before
  `parcel_update` + the `dict[str,str]` insert.
- `coordinator/broker.py` (2): `_load_parcel` is `Optional[Parcel]`; added
  `_resolved_parcels` that asserts non-None (resolve_task guarantees it) so
  `co_schedulable(Parcel, Parcel)` typechecks.

**ruff: clean** (`swarmsync/` + `tests/`). Cleared the last pre-existing test-lint
baseline this pass finally gates on: 2x F401 (unused imports, autofixed), 3x E741
(`l` -> `lease`), 2x F841 (`with TestClient(app) as client` -> no bind).

**New regression tests (+13).**
- `tests/test_leases.py` (2): `test_barrier_gated_write_acquires_on_one_parcel_grant_exactly_once`
  (N=24 threads race one parcel behind a `Barrier` -> exactly 1 granted + exactly 1
  active row, and the grant reports THAT row's id) and
  `test_barrier_gated_acquires_on_distinct_parcels_return_distinct_lease_ids`
  (8x12 distinct parcels -> all grant, returned lease_ids are a clean bijection with
  their own inserted rows). Both verified to FAIL on the pre-fix `cur.lastrowid`
  form (distinct grants collide on one id; single-parcel mutual exclusion breaks)
  and PASS on `INSERT ... RETURNING id`.
- `tests/test_integrator.py` (1):
  `test_stale_frozen_contract_type_hash_short_circuits_to_needs_rebase` inserts a
  frozen contract whose symbol is no parcel's id, forcing `_check_read_deps`'
  `contracts.type_hash` fallback branch (the parcels lookup misses): a drifted
  type_hash -> `needs_rebase`/no merge; a MATCHING type_hash lets the merge proceed
  (the matching case is what pins the fallback -- without it a contract-only id
  would read `None` and wrongly rebase a fresh branch).
- `tests/test_agent.py` (2):
  `test_run_agent_releases_already_held_leases_on_a_mid_acquire_denial` (agent
  targets [helper, other]; another agent holds `other`, so the first acquires
  `helper` then is denied on `other` -> must release `helper`; proven by a fresh
  agent immediately re-acquiring `helper`) and
  `test_heartbeater_survives_a_raising_heartbeat_and_keeps_beating` (a client whose
  first `heartbeat` raises -> the daemon thread swallows it and produces >=2 later
  successful beats, thread still alive).
- `tests/test_serve.py` (3, new file): `serve.main()` smoke -- builds a real
  FastAPI app + `uvicorn.run` on `127.0.0.1:8787` by default, host/port overridable,
  `--help` exits 0 (uvicorn stubbed, no socket bound).
- `tests/test_hook_guard.py` (5, new file): drives the real
  `scripts/swarmsync-hook-guard` as a subprocess across the matrix -- dormant (no
  env, no marker) exits 0 without launching the adapter; active-via-env and
  active-via-marker launch it and forward argv; active-but-adapter-unavailable is
  fail-open (exit 0); an active adapter's exit code propagates through the guard's
  `exec`. HOOK_BIN is rewritten to a sentinel-dropping stub so "did it launch?" is
  directly observable with no server up.

**Demo suite refactor.** `tests/test_demo.py`'s five in-process assertions each
used to spin up their own ~9s `run_demo()` (5 redundant full runs). They now share
one session-scoped `demo_run` fixture (`run_demo(keep=True)`, run once). The
fixture also pins `SWARMSYNC_ROOTS` itself, since a session fixture is set up
before conftest's function-scoped managed-root autouse. The literal
`python demo/run_demo.py` subprocess done-when stays separate (it uniquely covers
`main()`'s PASS-printing / real exit code). Full-suite wall time 58s -> 30s.

Full suite: 247 passed (234 -> +13). ruff clean (source + tests). mypy clean
(`.venv/bin/mypy swarmsync/`: no issues in 25 source files).

## S7-docs (P2)

Docs + design reconciliation. No product-code behavior change except
`scripts/swarmsync-hook-guard`'s HOOK_BIN resolution (a real portability fix).

**DESIGN.md drifts fixed:**
- **§5.3 contract freeze rewritten** to describe what the code actually does: detection is
  **POST-merge**, in the integrator (`coordinator/integrator.py`), which snapshots
  `contracts.type_hash` before/after its post-land re-index and diffs it -- there is no
  separate "exclusive contract lease" step (only the ordinary write/exclusive parcel lease
  every edit takes) and no proactive "mark dependents stale" push. Documented the weaker
  guarantee this implies: a dependent only learns of a landed contract change by polling
  `GET /events` or opting into the integrator's `expected_read_deps` re-check at its own
  integrate time -- there's a real window where in-flight work builds against a stale
  signature, with the test gate as the eventual backstop, not this mechanism.
- **Recovery claim corrected** (§4.1 + §6 table): the old text claimed the projection
  tables (parcels/leases/pheromone) uniformly "rebuild by replaying" `events`. Only
  `leases`/`pheromone` are actually event-replayable (pure projections of grant/release/
  reap/heartbeat/decay events); `parcels`/`contracts` hold derived facts about the live
  on-disk source (spans, `content_hash`, `blast_radius`, signatures) the event log never
  carries -- `classifier/store.py`'s own docstring already says as much ("full rebuild
  every call" / "always re-parses every `.py` file"). Recovering them means re-running the
  classifier (`POST /index`), not replaying history. Both §4.1's events-table paragraph and
  §6's "Blackboard SPOF/corruption" + "Coordinator/server crash" rows now say this.
- **Fabricated quotation deleted** from all 3 docstrings that cited it (`server/app.py`
  module docstring + `create_app`'s docstring, `coordinator/reaper.py`'s `run` docstring):
  none quoted "Background (startup): reaper + pheromone decay run as asyncio tasks" from
  DESIGN.md §4.2 -- DESIGN.md never actually says that (§4.2 is the HTTP endpoint table).
  Reworded each to describe the real wiring without the invented citation. Also touched
  the one lingering test comment (`tests/test_reaper.py`) citing the same fabricated
  string, for consistency.
- **Single-host filesystem-path constraint documented**: new §6 table row -- the
  blackboard is one SQLite file at a filesystem path (`SWARM_SYNC_DB`/`--db`) and every
  agent worktree is a plain directory under the repo's `.git`; both assume one shared
  filesystem/host. No network DB, no distributed lock, no cross-host worktree sharing;
  SQLite's WAL locking guarantees don't hold over NFS/network mounts.

**README.md** gained a "Use with Claude Code (hook-enforced coordination)" section: a
copy-pasteable `settings.json` block (pulled from this machine's actual working config)
wiring `PreToolUse`/`PostToolUse`/`SubagentStop`/`SessionStart` to
`scripts/swarmsync-hook-guard`; documents `.swarmsync-active`/`SWARMSYNC_ACTIVE`/
`SWARMSYNC_URL` and starting the server with `swarmsync-serve --port 8787`. Also documents
**both launchers and their ports** (`swarm-sync` -> `:8000` default, `swarmsync-serve` ->
`:8787` default) and the **fail-open port mismatch**: the hook adapter's own
`SWARMSYNC_URL` default is `:8787`, so booting the server with plain `swarm-sync` (its
`:8000` default) without also setting `SWARMSYNC_URL` makes every hook call silently fail
to reach the blackboard and fail-open -- edits proceed with **no leasing at all**, no
error surfaced. And clarifies the granularity split: money-shot #1's same-file concurrent
edits use the **broker**'s opt-in `mode="symbol"` in the demo; the Claude-Code-hook path
(`hooks/adapter.py`) always leases **whole-file**, so two subagents editing different
functions in the same file under hook enforcement will NOT both proceed the way the demo
shows -- the second is denied and must wait/pick different work.

**`~/.claude/skills/swarmsync/SKILL.md`** (outside this repo) had its setup step pointing
at `swarmsync-hook` directly and omitting `SessionStart` from the hook list -- both fixed
to match the real wiring, and the vague "see the repo's README / hook-setup docs" now
names the exact new README section.

**`scripts/swarmsync-hook-guard`**: `HOOK_BIN` was a single hardcoded absolute path
(`/home/keith/projects/swarm-sync/.venv/bin/swarmsync-hook`) -- broken for any checkout
elsewhere, or a non-venv install. Replaced with `resolve_hook_bin()`: try `command -v
swarmsync-hook` (PATH) first, then fall back to `../.venv/bin/swarmsync-hook` relative to
the guard script's own location (`dirname "$0"`) -- covers settings.json wiring this
script by absolute path with a PATH that doesn't include the venv's `bin/`.

**New regression test** (`tests/test_hook_guard.py::
test_active_resolves_hook_bin_via_venv_sibling_fallback_when_not_on_path`): lays out a
repo-shaped tmp tree (`<checkout>/scripts/swarmsync-hook-guard` next to
`<checkout>/.venv/bin/swarmsync-hook`) far from the real absolute path, with nothing named
`swarmsync-hook` on `PATH`, and asserts the guard still launches the stub adapter and
forwards its argv. Verified this (and 3 sibling tests that now stub the adapter via `PATH`
instead of patching a `HOOK_BIN=` line) **fail against the pre-fix guard**: swapped the old
script in via `git show HEAD:scripts/swarmsync-hook-guard`, reran the file -- 4/6 tests
failed (old script ignores `PATH` entirely and only checks its one hardcoded absolute
path), confirming these tests actually exercise the fix rather than the harness. The rest
of `tests/test_hook_guard.py` was adapted to the new resolution mechanism (stub named
`swarmsync-hook` on a controlled `PATH` instead of regex-patching a `HOOK_BIN=` line,
which no longer exists as a static assignment).

Full suite: 248 passed (247 -> +1). ruff clean (source + tests). mypy clean
(`.venv/bin/mypy swarmsync/`: no issues in 25 source files).
