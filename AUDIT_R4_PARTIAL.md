# Round 4 — PARTIAL audit (raw findings, NOT synthesized)

⚠️ **This round did not complete.** It hit an API session limit at 34/67 agents.
Lost: the **mutation** dimension, the **docs** dimension, ~30 adversarial verifiers,
the synthesis report, and the completeness critic. The findings below are the raw
output of the dimensions that *did* finish (unexamined, regressions, concurrency,
security, robustness, architecture). Counters at abort: 16 serious survived
verification, 3 refuted, 9 P2 — but many findings never reached a verifier at all,
so **treat everything here as UNVERIFIED unless noted below**.

## Already actioned (verified by me, fixed in 8f1a449)

- **P0 `_ensure_parcel` wrote `kind='file'`** → `ParcelKind` rejects it →
  `broker.load_scheduling_graph` validates every row → one hook lease on one non-.py
  file bricked ALL broker dispatch. Reproduced, fixed to `kind='module'`, mutation-tested.
- **P1 gate timeout did not bound the gate** — `proc.communicate()` after killpg had no
  timeout; an escaped `setsid` grandchild holds the pipe. Reproduced (2s gate took >25s),
  bounded the drain, mutation-tested.
- **P1 heartbeat's TTL predicate used a stale Python clock** — revives a lapsed lease if
  the UPDATE serializes late. Reproduced deterministically, moved to SQLite's write-time
  clock (`_NOW_SQL`), mutation-tested.

## Still open — NOT yet verified by me. Round 5 must re-verify before acting.

The three above prove this batch has real substance, but the rest below did not all get
adversarial verification. Highest-signal candidates, roughly by claimed severity:

- **P0 `BlackboardClient` uses httpx's default 5s timeout** — every `/integrate` whose gate
  exceeds 5s raises ReadTimeout in the agent *while the server lands the merge anyway*.
  If true this is severe and trivially reachable (the gate's own default ceiling is 600s).
- **P0 crash mid-integrate leaves an un-gated (red) merge permanently on trunk** — no
  startup reconciliation, no event, no rollback. (R3 rated a version of this P2.)
- **P0 `index_repo`'s 5000-file / 30s cap turns EVERY merge into a bogus rejection** on any
  repo large enough to need multi-agent coordination.
- **P1 `POST /parcel/update` has no lease-ownership check** — any agent can overwrite any
  parcel's content_hash, poisoning the §5.5 read-dep re-check in both directions.
- **P1 multi-root is unsound** — parcel ids are root-relative with no repo qualifier, so two
  managed roots overwrite each other's rows and conflate leases across repos.
- **P1 duplicate parcel ids silently collapse** (`@property`/`@x.setter`, any redefined
  symbol) — a whole code region vanishes from the parcel map.
- **P1 `POST /intent` 500s and leaves a half-written blackboard** when a target parcel isn't
  indexed (FK on pheromone aborts mid-route, no transaction, no event).
- **P1 hook lease keys on the unresolved leaf name** — an in-repo symlink alias yields two
  write leases on the SAME file in the shared hook working tree.
- **P1 R3's rollback re-index missed the merge-CONFLICT path** — a rejected conflict leaves
  the agent's self-reported content_hash in the blackboard for code that exists nowhere in git.
  (i.e. my P1-5 fix is incomplete.)
- **P1 the hook's `_keepalive` discards heartbeat's `False`** — an agent whose lease was
  lawfully taken by another is still ALLOWed to edit the shared working tree.
- **P1 reaper: one transient exception kills TTL reclaim + decay permanently**; it also does
  blocking SQLite on the asyncio loop thread (stalls the whole ASGI server ~5s) and breaks
  app shutdown.
- **P1 symbol-span leases are unenforced** — nothing confines an edit to its leased byte
  range, so span disjointness is mutator etiquette, not an invariant.
- **P1 ROUND4's option (a) rests on a wrong model of git** — the 3-way merge unit is "one
  unchanged line", not "3 lines of context". This directly corrects ROUND4.md §1; re-do
  that reasoning before implementing the gap rule.
- Plus P1s restating known-open items (contracts inert in file mode; no rebase implementation).

## P2s raised (unverified)

`_ensure_parcel` writes a caller-controlled, unvalidated, unbounded parcel id — including on
a DENIED acquire · and mints rows under `.git/`, `node_modules/`, `.venv/`, bypassing the
classifier's exclusion policy · the gate inherits `SWARMSYNC_TOKEN` into branch-authored test
code · `_reject_and_reset`'s re-index drops `integrate()`'s `threshold` · P1-6 preserves the
rejected branch but the retry's first step deletes it · no schema versioning · `events` grows
unbounded · `_find_lease`/`_find_holder` ignore lease `mode` · `release()` is the only lease
predicate with no ttl clause.

---

# Raw findings from completed dimensions


## [P1] Duplicate parcel ids silently collapse: @property/@x.setter (and any redefined symbol) loses a whole code region from the parcel map — real code edits become invisible to the blackboard
**swarmsync/classifier/indexer.py:190**

CLAIM: parse_file mints a parcel id purely from the symbol name (f"{rel}::{node.name}.{member.name}" for methods, f"{rel}::{node.name}" for top-level defs/classes). Python legally allows two defs with the same name in one scope — the ubiquitous @property/@value.setter pair, @typing.overload stubs, or a plain redefinition. Each emits a Parcel with the SAME id but a different byte span. store.upsert_parcels feeds them all to one executemany against ON CONFLICT(id) DO UPDATE, so the last one silently wins: N distinct code regions become 1 row. Worse, BOTH spans were appended to method_covered, so _leftover_bytes excludes both from the class-glue hash too. The overwritten region is therefore covered by NO parcel's content_hash at all. This breaks the classifier's stated invariant (indexer docstring: parcels partition the file; schema.sql: content_hash = 'sha256 of current source slice'), and at symbol granularity that region is leasable by nobody. R3 learned comment-only edits don't move content_hash; this is a *semantic code* edit that doesn't move it.

SCENARIO: m.py contains `class Thing:` with `@property def value` (bytes 17-70) and `@value.setter def value` (bytes 76-133). run_index writes ONE parcels row id='m.py::Thing.value' with byte_start=76 (the setter). An agent then edits the getter body `return self._v` -> `return self._v * 1000000` and re-indexes: zero content_hash rows change anywhere in the DB. The integrator's re-index-on-land and /parcel/update staleness tracking both see an unchanged repo for a change that altered behavior.

EVIDENCE: parse_file output: `m.py::Thing.value method 17 70 273d4489` / `m.py::Thing.value method 76 133 25c04482` / `ids: [...] unique: 3 of 4`. After run_index the DB holds only `[('m.py::Thing.value','method',76,133), ('m.py::Thing','class',None,None), ('m.py::<module>','module',None,None)]` — the getter row is gone. Mutation proof: after run_index, edit ONLY the getter body and re-run run_index -> `changed parcels: []` / `=> getter edit invisible to the blackboard: True`.


## [P1] POST /intent 500s and leaves a half-written blackboard when a target parcel isn't indexed — the FK on pheromone aborts mid-route with no transaction and no event emitted
**swarmsync/server/app.py:340**

CLAIM: post_intent issues its INSERT into `intents`, then a drop_pheromone per target parcel, then events.emit — each as a separate autocommit statement (db.connect uses isolation_level=None; the route never opens db.transaction). schema.sql declares `pheromone.parcel_id TEXT NOT NULL REFERENCES parcels(id)` and db._configure sets `PRAGMA foreign_keys = ON`, so an unknown parcel id raises sqlite3.IntegrityError from inside the loop. The already-committed `intents` row survives, any earlier targets' pheromone rows survive, and the `planned` event is never emitted — but schema.sql's own header calls `events` 'the source of truth for recovery: parcels/leases/pheromone are projections replayable from it'. Replay of this log reconstructs no intent, yet the intents/pheromone tables hold one. The caller gets an opaque 500 rather than 400/404. Note the asymmetry R3 introduced: R3's P0-2 fix taught /lease to cope with unindexed files via ensure_parcel, but /intent — which agents call FIRST, per DESIGN §4.3 step 2 — has no such path, so declaring intent on a brand-new file (the exact case P0-2 exists for) is a 500.

SCENARIO: Agent declares intent on a file the classifier hasn't indexed yet (new file, or a path skipped by indexer._is_skipped): POST /intent {agent_id:'a1', task:'t', target_parcels:['ghost.py::foo']} -> HTTP 500. DB afterwards: intents=[('a1','t','["ghost.py::foo"]')], events=[], pheromone=[]. With targets ['real.py::foo','ghost.py::bar'] the first pheromone row also commits, so the partial write is observable.

EVIDENCE: Ran against a fresh create_app + TestClient(raise_server_exceptions=False):
`intent status: 500 Internal Server Error`
`intents rows: [('a1', 't', '["ghost.py::foo"]')]`
`events: []`
`pheromone: []`
Source: app.py post_intent (no `with _db.transaction(conn)`), schema.sql:46 `parcel_id TEXT NOT NULL REFERENCES parcels(id)`, db.py:54 `conn.execute("PRAGMA foreign_keys = ON")`.


## [P1] Multi-root is unsound: parcel ids are root-relative with no repo qualifier, so two managed roots overwrite each other's parcel rows and conflate leases across repos
**swarmsync/classifier/indexer.py:271**

CLAIM: index_repo records each parcel's path/id relative to `root` ('POSIX-style, stable across machines'), and parcels.id is the sole PRIMARY KEY. Nothing in the id, the schema, or the DB file encodes WHICH root a parcel came from. But R3's P1-10 fix promoted multi-root to a first-class documented feature: server/serve.py exposes `--root` with action='append', and app._managed_roots splits SWARMSYNC_ROOTS on os.pathsep into a list. Two managed roots that share any relative path (api.py, utils.py, __init__.py, tests/conftest.py — near-certain across two repos) collide into one row. Indexing root B silently overwrites root A's path/content_hash/byte spans, and a lease on A's file blocks B's file. The hook path is equally exposed: adapter._relpath resolves relative to each session's own CLAUDE_PROJECT_DIR, so two Claude sessions in two repos against one blackboard mint identical parcel ids. Mutual exclusion here is over-broad (false denials = liveness loss), but the parcels-row overwrite is straight corruption: root A's row now reports root B's content_hash and byte offsets, which is what the integrator's re-index and /parcel/update staleness checks read as truth.

SCENARIO: SWARMSYNC_ROOTS=/repoA:/repoB (or `swarmsync-serve --root /repoA --root /repoB`), both containing api.py. agentA leases /repoA/api.py::handler -> granted. Someone indexes /repoB -> repoA's parcels row for api.py::handler is rewritten with repoB's content_hash. agentB then tries to lease /repoB/api.py::handler -> DENIED with "conflicting active lease on 'api.py::handler'", even though the two agents are editing two different files in two different repos. Conversely agentA's lease now 'protects' a row describing repoB's bytes.

EVIDENCE: One blackboard, TestClient, two roots:
`POST /index {root: .../repoA}` -> {'parcels': 2}
`POST /lease agentA api.py::handler` -> {'granted': True, 'lease_id': 1}
`POST /index {root: .../repoB}` -> {'parcels': 2}
`POST /lease agentB api.py::handler` -> {'granted': False, 'reason': "conflicting active lease on 'api.py::handler'"}
Final DB: `[('api.py::handler', 'api.py', 'df58161a...')]` — a single row, holding repoB's hash, with `path='api.py'` giving no way to tell the repos apart.
Source: serve.py:26-33 `--root ... action='append'`; app.py:175-181 `_managed_roots` splits on os.pathsep; indexer.py:271 `rel = rel_path_obj.as_posix()`; schema.sql:10 `id TEXT PRIMARY KEY`.


## [P1] Hook lease keys on the unresolved leaf name: an in-repo symlink alias yields two write leases on the SAME file, in the shared hook working tree
**swarmsync/hooks/adapter.py:198**

CLAIM: `_relpath` deliberately resolves only the PARENT directory and keeps the leaf name (`resolved = abs_p.parent.resolve() / abs_p.name`), so two different paths that are the same on-disk file (a symlink and its target inside the repo) map to two DIFFERENT parcel ids. `_parcel_id` then leases those two ids independently, and `leases.acquire`'s CAS is scoped to `l.parcel_id = :parcel_id` — so both agents are GRANTED a write lease. This is the same 'validate/canonicalize the root, then trust the leaf' pattern as the known `_validate_managed_path` escape, recurring on the enforcement surface rather than the allow-list surface, and with a worse consequence: the hook surface has no worktree isolation (all hook subagents share ONE working tree), so the lease is the only thing preventing a lost update. `classifier/indexer.py:258` (`root_path.rglob('*.py')`) follows the leaf symlink too and dutifully indexes BOTH `alias.py::<module>` and `real.py::<module>` as separate parcels, so the two components agree with each other and are both wrong — which is why the adapter docstring's justification ("map the same way or the parcel-id lookup misses") reads as correct. `ensure_parcel=True` (the R3 P0-2 fix) makes this worse, not better: an alias to a non-indexed file now reliably auto-creates the phantom second parcel and grants on it instead of failing.

SCENARIO: Repo contains `alias.py -> real.py` (an ordinary in-repo symlink; equally reachable with any symlinked source/config file, and on a case-insensitive FS with `API.py` vs `api.py`). Agent A's Edit of `real.py` acquires a write lease on `real.py::<module>` → ALLOW. Agent B's Edit of `alias.py` acquires a write lease on `alias.py::<module>` → ALLOW. Both edits are written to the same inode in the same shared working tree; whichever writes second silently clobbers the other. The product's core guarantee ("one agent per file") is not merely degraded — the deny that should have fired is replaced by a grant, and B is affirmatively told the file is free.

EVIDENCE: Executed against a TestClient-backed blackboard + the real `adapter.main` (repo: `real.py` + `alias.py -> real.py`):
  indexed parcels: ['alias.py::<module>', 'alias.py::a', 'real.py::<module>', 'real.py::a']
  A edits real.py  -> ('', '')            # ALLOW
  B edits alias.py -> ('', '')            # ALLOW  <-- should be DENY
  B edits real.py  -> deny "swarm-sync: real.py is leased by agentA"   # control: the deny path works
  leases: [('agentA', 'real.py::<module>'), ('agentB', 'alias.py::<module>')]
Two active write leases, one file.


## [P1] POST /parcel/update has no lease-ownership check — any agent can overwrite any parcel's content_hash, poisoning the §5.5 read-dep re-check in both directions
**swarmsync/server/app.py:406**

CLAIM: `post_parcel_update` UPDATEs `parcels.content_hash`/`state_summary` scoped only by `WHERE id = :parcel_id`. `body.agent_id` is accepted as an identity claim and used solely to stamp the pheromone/event — it is never checked against an active lease. This breaks the ownership scoping every other mutating lease route enforces (`leases.heartbeat` and `leases.release` are both scoped `AND agent_id = :agent_id AND status='active'` precisely so a foreign caller is a no-op). The written column is not cosmetic: `integrator._check_read_deps` (integrator.py:141-152) compares other agents' plan-time snapshots against exactly `parcels.content_hash` to decide `needs_rebase`. Round 3's P1-5 fix (`_reject_and_reset` re-indexes so the blackboard rolls back with git) exists *because* a wrong `content_hash` here corrupts integration decisions — yet any agent can write that same corruption on demand, with one unauthenticated-by-default POST. No legitimate caller needs this: `agent/runner.py` and `hooks/adapter.py:cmd_postupdate` both post only for a parcel they already hold, so an ownership predicate is non-breaking.

SCENARIO: Agent B (semi-trusted, the stated model) POSTs /parcel/update for `api.py::<module>` — a parcel B holds no lease on. Two consequences, both cross-agent: (1) DENIAL — B writes a garbage hash; agent A, who snapshotted `expected_read_deps` at plan time and did nothing wrong, is bounced with `needs_rebase` and its work never lands; (2) SILENT CLEARING, the worse one — B (or a buggy/retrying agent) writes back the hash matching A's now-stale snapshot, so `_check_read_deps` reports no staleness and A's branch is merged against blackboard state that never landed. The §5.5 optimistic re-check silently passes on a dependency that really did shift. B also overwrites `state_summary`, the note other agents read to decide what a parcel now does.

EVIDENCE: Executed (SWARMSYNC_TOKEN unset = default; with it set, any agent holding the token is equally unrestricted):
  # 'owner' holds the write lease on f.py::<module>
  POST /parcel/update {agent_id:'attacker', parcel_id:'f.py::<module>', content_hash:'deadbeef', state_summary:'pwned'}
  -> 200 {'ok': True, 'parcel_id': 'f.py::<module>', 'event_seq': 9}
  SELECT: {'id': 'f.py::<module>', 'content_hash': 'deadbeef', 'state_summary': 'pwned'}
Contrast leases.py:196-209 (heartbeat) and 220-234 (release), both scoped by agent_id.


## [P2] _ensure_parcel writes a caller-controlled, unvalidated, unbounded parcel id — including on a DENIED acquire
**swarmsync/server/leases.py:58**

CLAIM: R3's P0-2 fix made `POST /lease` a parcels-table WRITE path with a caller-supplied primary key and zero validation: no length bound, no charset check, no repo-relative check, and no rate/row cap. It also runs BEFORE the CAS and outside it, so a losing acquire still leaves the row behind permanently (`store.run_index` explicitly never prunes stale parcel rows — store.py docstring, 'No stale-row pruning'). Judged against the real trust model this is not an escape hatch: the row is inert data (nothing resolves `parcels.path` back to the filesystem — the integrator selects parcels by `p.path in changed_set` from a fresh `index_repo`, so a `../../../etc/passwd` row is never opened), and it cannot shadow a real parcel (`INSERT OR IGNORE`, and `run_index`'s ON CONFLICT DO UPDATE rewrites every column). What remains is real but bounded: unbounded growth of a persistent table at a stable path from a route that is unauthenticated by default, plus rows carrying NUL bytes / 100KB ids that `GET /parcels` then serves to every reader.

SCENARIO: Any localhost process (default: no token) loops `POST /lease {parcel_id: <random 100KB string>, ensure_parcel: true}`. Each call permanently adds a parcels row — even when the acquire is denied. Nothing reaps them; `blackboard.db` grows without bound and `GET /parcels` (unauthenticated, returns the whole table with no limit) degrades for every agent. A NUL-containing id is accepted and stored.

EVIDENCE: Executed, all returned 200 granted and left a row:
  parcel_id '../../../etc/passwd::<module>' -> row (id='../../../etc/passwd::<module>', path='../../../etc/passwd', kind='file')
  parcel_id 'A'*100000+'::x'                -> row stored
  parcel_id 'a\x00b::x'                     -> row (path='a\x00b')
  parcel_id ''                              -> row (id='', path='')
Code: `_ensure_parcel` is called at leases.py:105-106, unconditionally, before the CAS INSERT at 123.


## [P2] The pytest gate inherits SWARMSYNC_TOKEN into branch-authored test code
**swarmsync/coordinator/integrator.py:279**

CLAIM: `env = {**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}` hands the full server environment — including SWARMSYNC_TOKEN, the documented trust boundary for who may reach the blackboard — to the merged branch's `conftest.py`/`test_*.py`, which the gate executes by design. Verdict against the real trust model: this is mostly NOT an escalation, and I want to be explicit rather than inflate it. Submitting a branch requires POST /integrate, which already requires the token, so the submitter already has it; and with the token unset (the default) the README correctly states the whole surface is open anyway. It is a genuine boundary crossing only in the narrow case where branch AUTHOR and branch SUBMITTER differ (e.g. a hook subagent that never held the token authored the commits; an operator submits). The reason to fix it is that it is free: nothing in the gate needs the token, so `env.pop('SWARMSYNC_TOKEN', None)` costs one line and removes the token from the one place the design intentionally runs untrusted code. R3 already flagged this in passing (AUDIT_R3.md:316); I am confirming it rather than re-discovering it.

SCENARIO: A branch whose commits were authored by a party without the token is submitted to /integrate by a party who has it. The branch's `conftest.py` reads `os.environ['SWARMSYNC_TOKEN']` at collection time and thereafter holds full mutating access to the blackboard (steal/forge leases, merge arbitrary branches) beyond the lifetime of the gate run.

EVIDENCE: integrator.py:279 `env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}` passed to `subprocess.Popen(..., env=env)` at 286-294. No key is removed. README:195-203 states the gate runs branch code by design AND that SWARMSYNC_TOKEN is the access gate — the two statements are in tension only for the author≠submitter case.


## [P0] P0-2's fix writes kind='file', which ParcelKind rejects: one hook-leased non-.py file permanently bricks the broker
**swarmsync/server/leases.py:81**

CLAIM: Round 3's P0-2 fix (`_ensure_parcel`) inserts parcel rows with `kind='file'`, but `blackboard/models.py` declares `ParcelKind = Literal["function", "method", "class", "module"]` (schema.sql line 13 comment agrees: `function|method|class|module`). Every consumer that goes through `Parcel.model_validate` -- `broker.load_scheduling_graph` (SELECT * FROM parcels, i.e. ALL rows) and `broker._load_parcel` -- raises pydantic ValidationError. Because `classifier/store.py` documents 'No stale-row pruning' and the blackboard is a persistent SQLite file at a stable path, the poisoned row is never removed. `.py` files self-heal (a later run_index upserts kind='module' over the same id); non-.py files NEVER do, because index_repo only walks *.py.

SCENARIO: Operator runs a hook-coordinated Claude session on a repo (the documented normal use of the hook adapter). A subagent edits any non-Python file -- config.yaml, package.json, Dockerfile, a .ts file. cmd_precheck calls client.lease(..., ensure_parcel=True), which writes parcels row id='config.yaml::<module>', kind='file'. That row is now permanent. Later, anything that drives the broker against the same blackboard.db (the demo, coordinator.broker.run, resolve_task) calls load_scheduling_graph -> SELECT * FROM parcels -> Parcel.model_validate(dict(row)) -> ValidationError. The broker is dead for that blackboard and cannot recover without hand-editing SQL. Note the irony: this is the exact path P0-2 added to stop the hook failing open on unindexed files.

EVIDENCE: Verified end-to-end against the real code (scratch DB + sample_repo, repo untouched, `git status --porcelain` clean):

    indexed ok
    hook lease granted: True
    row written: {'id': 'config.yaml::<module>', 'kind': 'file'}
    BROKER BLEW UP: ValidationError
    1 validation error for Parcel
    kind
      Input should be 'function', 'method', 'class' or 'module' [type=literal_error, input_value='file', input_type=str]

Source: leases.py:81 `VALUES (:id, :path, 'file', :symbol, :now)` vs models.py:23 `ParcelKind = Literal["function", "method", "class", "module"]`. `grep -rn "'file'" --include=*.py swarmsync/` returns only leases.py:74 (docstring) and leases.py:81 -- nothing ever adds 'file' to the Literal. store.py docstring: "No stale-row pruning. If a file/symbol disappears from the repo between two run_index calls, its old parcels/contracts rows are left in place."


## [P0] index_repo's 5000-file / 30s cap turns EVERY merge into a bogus rejection on any repo large enough to need multi-agent coordination
**swarmsync/coordinator/integrator.py:528**

CLAIM: `integrate()` calls `run_index(conn, repo)` inside the post-merge try block. `run_index` -> `index_repo(root)` with DEFAULT_MAX_INDEX_FILES=5000 / DEFAULT_MAX_INDEX_SECONDS=30.0, and neither `integrate()` nor `run_index()` exposes a way to raise those caps (only `index_repo`'s keyword-only params, which nothing threads through; `threshold` is the only knob `integrate` forwards). On a repo over the cap, IndexLimitError (a RuntimeError) is caught by the blanket `except Exception` at line 569 and converted into `_reject_and_reset('integration_error', ...)`. The merge was clean and the tests were green, but it is rejected. `_reject_and_reset` then calls `run_index` AGAIN on the rollback path, which raises the same error and is reported as `rollback_error`. Result: on any repo >5000 .py files (or one where indexing exceeds 30s), integration is 100% dead, permanently, misreported as an internal error rather than a capacity limit. This is the answer to 'is there a realistic multi-agent scenario this architecture cannot handle': the machinery earns its keep precisely at the scale where it stops functioning at all. Note the cliff is NOT the O(repo)-per-merge re-index cost, which I measured as survivable (~4s at 2000 files/42000 parcels) -- it is the hard cap sitting just past it.

SCENARIO: A team points swarm-sync at a real production Python repo with 5001+ .py files (well within normal -- Django, Airflow, any monolith). Agent-1 takes a lease, edits one function, commits, POSTs /integrate. git merges cleanly; the impact-selected pytest gate passes. Then run_index raises IndexLimitError at file 5001. integrate() rolls trunk back and returns merge_rejected/reason='post-merge integration failed: IndexLimitError'. Every subsequent merge by every agent does the same. No work can ever land. The operator sees an opaque 'integration_error', not 'your repo is too big'; there is no env var or parameter to raise the cap.

EVIDENCE: Verified end-to-end with a real 5001-file git repo + real integrator call in /tmp (repo under test untouched):

    >>> integrate status : merge_rejected
    >>> reason           : post-merge integration failed: IndexLimitError('index walk of /tmp/tmptp_aj_5k/big exceeded max_files=5000') (WARNING: trunk rollback also failed: blackboard re-index after rollback failed: IndexLimitError('index walk of /tmp/tmptp_aj_5k/big exceeded max_files=5000'))
    >>> trunk moved?     : False

The merge itself was clean and tests/test_ok.py passed; only the re-index failed. Scaling measurement of the per-merge classifier cost inside the global integrate_lock (integrator does run_index AND _reverse_dep_files, i.e. index_repo x2 + build_graph x2 per merge):

      100 files /   2100 parcels: ONE run_index=0.08s -> per-merge ~0.16s
      500 files /  10500 parcels: ONE run_index=0.44s -> per-merge ~0.84s
     2000 files /  42000 parcels: ONE run_index=2.26s -> per-merge ~3.94s
    index_repo caps: max_files=5000, max_seconds=30.0

Source: indexer.py:58-59 DEFAULT_MAX_INDEX_FILES=5000 / DEFAULT_MAX_INDEX_SECONDS=30.0; store.py:182 `parcels = index_repo(root)` (no cap passthrough); integrator.py:528 `index_result = run_index(conn, repo, **index_kwargs)` where index_kwargs only ever carries `threshold`; integrator.py:569 `except Exception` -> line 575 `_reject_and_reset("integration_error", ...)`; integrator.py:430 `run_index(conn, repo)` on the rollback path raising again.


## [P1] ROUND4's option (a) rests on a factually wrong model of git: the 3-way merge unit is 'one unchanged line', not '3 lines of context'
**ROUND4.md:70**

CLAIM: ROUND4 proposes requiring a >=3-line gap between co-scheduled spans because 'git merges line hunks with 3 lines of context'. That is a property of diff display / `patch` application, not of a 3-way merge with a common ancestor. Empirically git's merge conflicts only when the two branches' CHANGED REGIONS overlap or are immediately adjacent (zero unchanged lines between them); one unchanged line between them merges cleanly. Since two disjoint Python AST spans are always separated by at least the `def` header line, span disjointness ALREADY implies the real safety condition in the common case. Adopting (a) as written would pay a real scheduling cost to fix a problem that largely does not exist, on the strength of a wrong premise -- and, worse, would create false confidence that symbol mode is now 'true of git's merge unit' while the actual hole (no span enforcement, see the separate finding) remains wide open.

SCENARIO: Round 4 implements the >=3-line gap rule, DESIGN/README are updated to claim symbol mode is now sound against git's merge unit, and the audit closes P1-7. The claim is unearned: the rule neither addresses the real failure mode (an agent editing outside its leased span) nor corresponds to how git actually merges. Meanwhile 0.7% of otherwise-safe pairs are needlessly serialized, and the modal real-world gap (exactly 3 lines, 312 of 3956 pairs -- PEP8's two blank lines between top-level defs) sits precisely on the threshold, so small formatting changes flip pairs in and out of schedulability for no safety reason.

EVIDENCE: Real git repos built in /tmp, one per gap width, each with two branches editing one region apiece, then merged:

    same-line       -> CONFLICT
    adjacent-lines  -> CONFLICT    (0 unchanged lines between changed regions)
    1-line-apart    -> CLEAN
    2-lines-apart   -> CLEAN

    gap=0 lines between spans -> git merge: CLEAN
    gap=1 lines between spans -> git merge: CLEAN
    gap=2 lines between spans -> git merge: CLEAN
    gap=3 lines between spans -> git merge: CLEAN
    gap=4 lines between spans -> git merge: CLEAN
    grow-f1 + insert-above-f2 -> CLEAN   (span growth does not break it either)

Cost measurement of rule (a) against this repo via the real classifier (index_repo + span pairs):

    === /home/keith/projects/swarm-sync
    files with >=1 concrete symbol: 45, span-disjoint pairs: 3956
      pairs with >=3-line gap (would survive rule (a)): 3930 (99.3%)
      pairs REJECTED by rule (a) (gap<3): 26 (0.7%)
      gap-line histogram: {1: 1, 2: 25, 3: 312, 4: 2, 5: 3, 6: 96, 7: 22, 8: 14}
    === sample_repo
      span-disjoint pairs: 40; rejected by rule (a): 0 (0.0%)

Minimum observed gap is 1 line (never 0), i.e. span disjointness already satisfies the real >=1-unchanged-line condition.


## [P1] Symbol-span leases are unenforced: nothing confines an edit to its leased byte range, so span disjointness is mutator etiquette, not an invariant
**swarmsync/agent/runner.py:271**

CLAIM: This is the real defect under P1-7, and it is not the one ROUND4 names. `run_agent` acquires a lease on a parcel id carrying (byte_start, byte_end), then calls `mutator(worktree, **mutator_kwargs)` with no byte-range check, and then `git_ops.commit_all(worktree, ...)` which is `git add -A` over the WHOLE tree. There is no code anywhere that verifies the resulting diff falls inside the leased span -- not in runner.py, not in git_ops.py, not in integrator.py (whose changed_files/impact selection is per-FILE). The span-disjointness the scheduler computes is therefore guaranteed only by `mutators.py`'s own convention, which its docstring states explicitly as a promise rather than an enforced property: 'They deliberately do NOT touch anything outside the named symbol's own span, so two mutators targeting two different symbols in the same file produce non-overlapping textual hunks by construction.' The same docstring then says 'A real Claude Agent SDK worker replaces these by producing a diff' -- and a diff is span-unconstrained. Compounding this, the only real-agent surface (the hook adapter) hardcodes file granularity (`_parcel_id` -> `<file>::<module>`), so symbol mode has no real-agent implementation at all and is exercised solely by mutators that are disjoint by construction. Answer to ROUND4's central question: the parcel abstraction is decoration at BOTH granularities -- in file mode because the contract machinery never fires, and in symbol mode because the safety property is delivered by the scripted mutators, not by the lease.

SCENARIO: The prototype is pointed at real agents (the stated intent -- mutators.py: 'A real Claude Agent SDK worker replaces these by producing a diff'), running mode='symbol' for the parallelism it advertises. Agent-A holds calc.py::add, Agent-B holds calc.py::div; co_schedulable returns True (spans disjoint) so they run concurrently in separate worktrees. Agent-A, asked to fix `add`, also adds an import at the top of calc.py and tweaks a shared module-level constant -- both outside its span, both inside calc.py::<module> which it does not hold. Agent-B does the same. commit_all commits everything. At merge, either git conflicts (reported as 'touch-set misprediction', blaming the scheduler for a lease that was never enforced) or -- worse -- both edits merge cleanly and silently clobber each other's semantics with trunk green, because the pytest gate only sees the union. No lease was violated by any check in the system, because no check exists.

EVIDENCE: runner.py:270-272 -- `worktree = git_ops.add_worktree(repo, agent_id, base_commit)` / `mutator(worktree, **mutator_kwargs)` / `commit_all(worktree, f"{agent_id}: {task}")`: no byte-range validation between the lease and the commit. git_ops.py:234 `commit_all(worktree, message, allow_empty=False)  # git add -A && git commit`. mutators.py module docstring (lines 19-22): 'They deliberately do NOT touch anything outside the named symbol's own span, so two mutators targeting two different symbols in the same file (money-shot #1) produce non-overlapping textual hunks by construction' -- 'by construction' is the tell; it is a property of these five functions, not of the lease. mutators.py:12-13: 'A real Claude Agent SDK worker replaces these by producing a diff.' adapter.py:217 `return f"{relpath}::{MODULE_SYMBOL}"` with the docstring 'this hook always leases the one synthetic per-file interstitial parcel id' -- the real-agent surface never uses symbol mode. Confirmed by running the real broker against sample_repo: `resolve_task symbol-mode-> ['calc.py::add']` (a span-bearing parcel is leased) while nothing downstream ever reads byte_start/byte_end for enforcement -- grep shows byte_start/byte_end consumed only by indexer (production), graph.co_schedulable (scheduling), and *_state_summary (display).


## [P1] In file mode (the default, and what the hook hardcodes) the frozen-contract subsystem is unreachable: the exclusive upgrade and co_schedulable's frozen clause can never fire
**swarmsync/coordinator/broker.py:308**

CLAIM: Reported per the brief's allowance because I have the concrete design resolution and the measured mechanism, not as a rediscovery of P1-8. `extract_contracts` keys Contract.symbol on the SYMBOL parcel id (graph.py:52-54: 'Frozen contracts use the parcel id (e.g. "mod_a.py::helper") as Contract.symbol'), so frozen_ids = {'calc.py::add', ...}. `resolve_task(mode='file')` collapses every hint to `<file>::<module>` (broker.py:180-181). The intersection is therefore empty by construction, which makes TWO mechanisms dead code in the default mode: (1) broker.py:307-311 `lease_modes = {pid: "exclusive" for pid in target_parcels if pid in frozen_ids}` always yields {} -> run_agent never receives an exclusive override -> DESIGN 5.3's 'to change a frozen contract you must take an exclusive lease' is never enforced; (2) graph.py:362-366 `co_schedulable`'s frozen clause tests `a.id in frozen_ids` against module ids -> always False -> the frozen-contract scheduling guard never fires. Separately, even when (1) DOES fire (symbol mode), 'exclusive' is indistinguishable from 'write' in `acquire`: the conflict predicate is `(l.mode IN ('write','exclusive') OR :mode IN ('write','exclusive'))`, which treats the two identically, so the 'upgrade' grants nothing extra. Resolution: map frozen_ids up to their owning `<file>::<module>` parcel when mode='file' (a frozen symbol's file-parcel inherits the freeze), AND either give 'exclusive' a distinct predicate in acquire or delete the mode outright -- a three-value enum where two values are behaviourally identical is a documentation lie.

SCENARIO: Operator runs the default configuration (mode='file', contract_aware=True) or the hook adapter (file granularity hardcoded). Agent-1's task targets calc.py, whose `add` is a frozen contract with 4 dependents. The broker resolves ['calc.py::<module>'], finds it is not in frozen_ids, and grants an ordinary 'write' lease. Agent-1 changes add's signature. co_schedulable's frozen clause never fires, so a dependent task on another file that calls add is scheduled into the SAME wave. Both land. DESIGN 5.3 and README's frozen-contract guarantee describe a mechanism that provably cannot execute in the shipped default. The system's headline money-shot #3 works only in symbol mode -- the mode with no real-agent surface and no span enforcement.

EVIDENCE: Verified against the real broker + sample_repo (scratch DB, repo untouched):

    frozen_ids (contract symbols): ['calc.py::add', 'calc.py::div', 'calc.py::mul', 'calc.py::sub', 'formats.py::money', 'formats.py::percent']
    resolve_task file-mode  -> ['calc.py::<module>']
    resolve_task symbol-mode-> ['calc.py::add']
    file-mode ids that are frozen (drives the exclusive upgrade): []

Source: broker.py:180-181 `if mode == "file": candidate = module_id`; broker.py:307-311 the exclusive-upgrade dict comprehension; graph.py:362-366 the frozen clause keyed on `a.id`/`b.id`; graph.py:331 `Contract(symbol=p.id, ...)` where p is a function/class parcel; leases.py:135 `AND (l.mode IN ('write', 'exclusive') OR :mode IN ('write', 'exclusive'))` -- the sole use of 'exclusive' in the conflict predicate, identical in effect to 'write'.


## [P1] DESIGN 5.5 promises rebase-and-resubmit; there is no implementation, so every conflict and every stale read-dep is terminal for that agent
**swarmsync/coordinator/integrator.py:12**

CLAIM: `grep -rn rebase --include=*.py` over swarmsync/ finds only the `needs_rebase` status literal, the EventType literal, and docstrings -- no rebase implementation, and no caller that reacts to needs_rebase by rebasing. The consequences are structural, not cosmetic. (i) `integrate` returns needs_rebase/merge_rejected; `run_agent` records it on AgentResult and returns 'done' (runner.py:302 `landed = integrate_result.get("status") == "merged"`, then status='done' regardless). (ii) `broker._run_task_with_retries` only retries on `result.status != "lease_denied"` -- a needs_rebase or merge_rejected result is NOT retried, it is returned as the task's final answer. So the branch is preserved (correctly, per P1-6's fix) but nothing ever picks it up: the work is orphaned on a branch no code path will ever rebase or resubmit. The optimistic re-check (step 1) is thereby a pure liveness cost with no recovery path -- it converts 'merge and find out' into 'give up', which is strictly worse than not having it, and it is why P1-7's 'conflict is fatal' framing is accurate. This is the honest half of the fix ROUND4's option (b) names.

SCENARIO: Two agents in one wave; agent-A lands first and shifts a parcel's content_hash. Agent-B submits with expected_read_deps snapshotted at plan time. _check_read_deps finds the hash moved, integrate returns needs_rebase without attempting the merge (integrator.py:376-382). run_agent returns status='done' with integrate_result.status='needs_rebase'. broker._run_task_with_retries sees status != 'lease_denied' and returns immediately -- no retry, no rebase. _cleanup_worktree runs with delete_branch=False, so branch 'B-attempt-1' survives pointing at real committed work that will never land and that nothing reports as outstanding. broker.run reports the task as a completed AgentResult. The caller has no signal that the work was silently dropped.

EVIDENCE: `grep -rn rebase --include=*.py swarmsync/` yields only status/EventType literals (models.py:37 `"needs_rebase",  # U10: optimistic re-check (DESIGN 5.5) found a stale read-dep`) and prose. integrator.py:9-18 docstring promises the DESIGN 5.5 path and states 'bounce back to the agent' -- nothing catches the bounce. runner.py:296-318: integrate_result is stored, `landed` set, and AgentResult returned with status='done' unconditionally on this path. broker.py:354 `if result.status != "lease_denied": return result` -- the only retry trigger is lease_denied. runner.py:136-142 (_cleanup_worktree docstring) confirms the branch is deliberately kept 'to preserve the rebase-and-resubmit path DESIGN 5.5 promises' -- preserving a path that does not exist.


## [P0] Crash mid-integrate leaves an un-gated (red) merge permanently on trunk; no startup reconciliation, no event, no rollback
**swarmsync/coordinator/integrator.py:569**

CLAIM: The post-merge atomic block's guard is `except Exception`, which covers only in-process, non-BaseException failures. The merge commit is placed on trunk BEFORE the gate runs, and the rollback that undoes it is purely an in-process compensating action. Any death of the integrating process between `merge_branch` (line 450) and the guard -- SIGKILL, OOM killer, container eviction, host reboot, `kill -9` of uvicorn -- leaves the merge commit on `into` with the gate never having passed, no `merged`/`merge_rejected`/`reindexed` event, and the blackboard still at pre-merge state. There is NO startup reconciliation anywhere: `create_app` (app.py:227) only calls `db.init_db`, which is a pure `CREATE TABLE IF NOT EXISTS` bootstrap. A restart resumes onto a poisoned trunk and never notices. `except Exception` additionally misses BaseException (KeyboardInterrupt/SystemExit), so any direct-call path (broker, demo, CLI, tests -- main-thread callers of `integrate`) reaches the identical end state from a plain Ctrl-C during the gate. This directly breaks DESIGN 5.4's stated core guarantee, quoted in this module's own docstring: 'trunk is never poisoned by a partial edit'. Secondary: R3's P1-4 `start_new_session=True` (line 293) detaches the gate's pytest from the parent's death, so the orphaned pytest keeps running after the integrator is killed (observed PID still alive post-SIGKILL).

SCENARIO: Agent branch `agentD` changes d.py and adds a failing test. `integrate()` merges it onto trunk, then starts the 30s gate. At t=4s the process is SIGKILLed (OOM/reboot/operator). Result observed: trunk moved 00578924 -> ff492db ('merge agentD into integration'), trunk's d.py is now the un-gated `return 666`, trunk's suite is RED, zero events were emitted, the orphan gate pytest (pid 104591) was still running, and re-opening the DB shows nothing reconciles.

EVIDENCE: Induced in an isolated mktemp workspace. SIGKILL run output:
  --- after SIGKILL of the integrator mid-gate ---
  trunk pre : 00578924
  trunk post: ff492db9
  ff492db merge agentD into integration
  orphaned gate pytest still running: 104591 .../python -m pytest -q -p no:cacheprovider --import-mode=importlib tests/test_d.py
  events for this merge: NONE
  trunk d.py: def k():;     return 666
BaseException variant (KeyboardInterrupt raised inside the gate, in-process):
  integrate() escaped with: KeyboardInterrupt -- NOT rolled back by `except Exception`
  trunk pre : f2968838 | trunk post: 7cbf99aa | merge LANDED and left on trunk: True
  un-gated red test now on trunk: True
  events emitted for this merge: NONE -- no audit record at all
  trunk suite now: 1 failed, 1 passed in 0.01s
Code: line 450 `ok, conflicts = git_ops.merge_branch(...)` lands the commit; line 569 `except Exception as exc:  # noqa: BLE001` is the only guard; app.py:227 `conn = db.init_db(db_path)` is the entire startup path.


## [P0] BlackboardClient over a real base URL uses httpx's default 5s timeout -- every integrate whose gate exceeds 5s raises ReadTimeout in the agent while the server lands the merge anyway
**swarmsync/agent/client.py:44**

CLAIM: `BlackboardClient.__init__` builds `httpx.Client(base_url=http)` with no `timeout=`, so it inherits httpx's default `Timeout(5.0)` (verified: httpx 0.28.1). This is the documented 'real deployment talking to uvicorn over the network' form (client.py:10-12) and exactly what demo/run_demo.py:679 uses. It is in direct contradiction with the integrator's own `DEFAULT_GATE_TIMEOUT_SECONDS = 600.0` (integrator.py:101): the system explicitly budgets up to 600s for a gate that the client abandons at 5s. The whole test suite is blind to this because every test drives a `TestClient` (the non-str branch, line 47), which has no such timeout. Consequence chain in `run_agent`: `client.integrate` (runner.py:296) raises ReadTimeout -> `landed` stays False -> the exception propagates through the outer `finally` (runner.py:319), so `client.release` (line 304) NEVER runs and every lease is held until TTL -> no AgentResult is returned -> `broker.run`'s `future.result()` (broker.py:410) re-raises and takes down the whole wave. Meanwhile the server completes the merge and lands it on trunk. The agent/broker records a failure for a merge that actually succeeded: agent-state vs git divergence, on the normal path, for any repo whose suite takes >5s (i.e. any real repo).

SCENARIO: Real uvicorn server + `BlackboardClient("http://127.0.0.1:PORT")`. Branch `agentX` submitted; its impact-selected gate takes 8s. At t=5.0s the client raises httpx.ReadTimeout. At t~=13s the server has merged and landed the branch on trunk. The agent never released its leases, kept its branch as if rejected, and raised out of run_agent.

EVIDENCE: Induced against a real uvicorn server in a mktemp workspace:
  client timeout config: Timeout(timeout=5.0)
  RAISED after 5.0s: ReadTimeout('timed out')
  trunk log: a9af150 merge agentX into integration | 93cf7e6 init
Code: client.py:44 `self._http: _HttpLike = cast(_HttpLike, httpx.Client(base_url=http))` -- no timeout, vs integrator.py:101 `DEFAULT_GATE_TIMEOUT_SECONDS = 600.0`. By contrast hooks/adapter.py:132 DOES pass `timeout=_DEFAULT_TIMEOUT_SECONDS` explicitly, so the omission here is an oversight, not a policy.


## [P1] R3's rollback re-index fix missed the merge-conflict path: a rejected conflict leaves the agent's self-reported content_hash in the blackboard for code that exists nowhere in git
**swarmsync/coordinator/integrator.py:456**

CLAIM: R3's P1-5 fix put the compensating `run_index(conn, repo)` inside `_reject_and_reset` (line 430). But the textual-conflict rejection at line 456 (`if not ok:`) does NOT go through `_reject_and_reset` -- it emits `merge_rejected` and returns directly, with no re-index. `runner.run_agent` posts `/parcel/update` with its freshly-derived content_hash BEFORE calling `/integrate` (runner.py:291 then 296), so by the time the conflict is detected the blackboard's `parcels.content_hash` already holds the agent's branch hash. On conflict that value is never rolled back, and it describes source that is on no branch git will ever land. This is precisely the harm R3's own comment (lines 417-424) describes -- `_check_read_deps` compares other agents' plan-time snapshots against exactly this column, so an innocent agent that snapshotted the REAL trunk hash is spuriously bounced with needs_rebase, and an agent that snapshots the phantom is cleared to merge against state that never existed. The divergence is permanent: nothing re-indexes that file until some other branch touching it merges successfully. Note the conflict path is the EXPECTED rejection path in normal use (DESIGN calls a conflict the hard signal of touch-set misprediction), so this is the more-travelled sibling of the path R3 fixed.

SCENARIO: agent1 branches from base and edits a.py::f; trunk concurrently gets a conflicting edit to the same lines. agent1 posts /parcel/update (hash 95d2b8b9) then /integrate. The merge conflicts -> merge_rejected, no re-index. The blackboard now serves content_hash 95d2b8b9 for a.py::f while trunk's real hash is ac269d51. A second agent that read trunk honestly and snapshotted ac269d51 is told its read-dependency is stale and bounced.

EVIDENCE: Induced in a mktemp workspace:
  integrate status: merge_rejected | conflicts: ['a.py']
  blackboard content_hash : 95d2b8b9dd5295026d0f61b6466e3c536b6aa2eab2f54a57c6621685d27ed925
  REAL trunk content_hash : ac269d51ea7269e36a5464a9e4b3d2151a087322475858a39818324217fd5bf6
  agent's phantom hash    : 95d2b8b9... (identical to the blackboard's)
  trunk file on disk      : def f():;     return 222
  DIVERGED: True | blackboard serving a hash for code that is NOWHERE in git: True
  innocent agent that snapshotted the REAL trunk hash is told stale -> ['a.py::f']
Code: integrator.py:456-475 returns IntegrateResult(status='merge_rejected', ...) directly, never calling `_reject_and_reset` (line 386) where the `run_index(conn, repo)` rollback lives (line 430).


## [P1] One transient exception permanently and silently kills the reaper + pheromone-decay loop for the server's lifetime
**swarmsync/coordinator/reaper.py:166**

CLAIM: `reaper.run`'s `while True` loop has no try/except around `reap_once`/`decay_once`. Both do real SQLite writes on a long-lived connection, so a transient `sqlite3.OperationalError('database is locked')` (busy_timeout is only 5s, and the integrator's gate can hold the DB busy for minutes) escapes the loop. It is started as a bare `asyncio.create_task` (app.py:239) that nobody awaits until shutdown, so the exception is swallowed into the task object: no log line, no restart, no health signal. The reaper is simply gone for the rest of the process's life. Everything the reaper exists for stops: no `reaped` events (which DESIGN 6 and the module docstring name as the signal the broker observes to reassign a crashed agent's task -- broker/observers therefore never learn an agent died), leases stay `status='active'` forever in the table, `GET /leases`'s view degrades, and pheromone decay stops so every 'planned'/'done' trail stays at full strength forever, poisoning the dedup hints DESIGN 2 relies on. At shutdown, `await task` (app.py:250) then re-raises the OperationalError, which the lifespan catches only as `asyncio.CancelledError` -- so the shutdown path errors too. Mutual exclusion itself survives only by luck of `acquire`'s lazy expiry, which is exactly the 'guarantee that holds only by luck' category.

SCENARIO: The reaper's 2nd pass hits one `database is locked` while the integrator's gate has the DB busy. The task dies. 30 minutes later an agent is SIGKILLed holding a write lease on z.py::f; its lease sits `active` forever, no `reaped` event is ever emitted, and nothing observing the event log ever learns the agent crashed.

EVIDENCE: Induced in a mktemp workspace by making reap_once raise ONE OperationalError on its 2nd call:
  reaper task dead after ONE transient error? True
    died with: OperationalError('database is locked')
  reap passes executed: 2 (loop gone; nothing restarts or logs it)
  crashed agent's expired lease status: active
  events: ['lease_granted']   <- no 'reaped' event ever
Code: reaper.py:166-173 -- `while True:` / `reap_once(conn, now)` / `decay_once(...)` / `await asyncio.sleep(interval)`, no exception handling; app.py:239 `task = asyncio.create_task(reaper_mod.run(...))`; app.py:247-252 catches only CancelledError.


## [P2] No schema versioning: init_db silently no-ops on an existing DB, so a schema change fails later as a per-query 'no such column' instead of at boot
**swarmsync/blackboard/db.py:85**

CLAIM: There is no `PRAGMA user_version`, no migration code, and no schema-shape check anywhere in the codebase (grep for user_version/migrat/VACUUM/wal_checkpoint over swarmsync/ returns nothing). `init_db` executes a pure `CREATE TABLE IF NOT EXISTS` script against a persistent DB at a stable default path (`blackboard.db` in cwd, app.py:203/504 -- and one is already sitting in the repo root). The docstring promises 'Safe to call repeatedly ... a second call on an already-initialized DB is a no-op (no errors, no data loss)', which is true and is exactly the problem: on a version skew the bootstrap reports success and the process boots green on the OLD table shape. The failure then surfaces at an arbitrary later request as a bare OperationalError, not at startup where it is diagnosable. R3 rated this P2 and I agree on severity, but the sharper mechanism is that the green boot actively hides the skew -- a two-line `PRAGMA user_version` check would convert a mystery 500 into a startup error.

SCENARIO: An operator upgrades swarm-sync; the new release adds `parcels.owner_agent`. Their existing blackboard.db is untouched by init_db, the server boots clean, and the first request touching the new column dies with `sqlite3.OperationalError: no such column: owner_agent`.

EVIDENCE: Reproduced against a COPY of the tree in a mktemp dir (real schema.sql never touched; `git status --porcelain` clean afterward):
  existing DB user_version: 0
  init_db on old DB raised? no -- it silently succeeded
  parcels columns after 'upgrade': ['id','path','symbol','kind','territory','blast_radius','contract_hash','content_hash','byte_start','byte_end','state_summary','updated_at']   <- owner_agent absent
  RUNTIME FAILURE on the new column the new code expects: no such column: owner_agent
Grep evidence: `grep -rn "user_version|wal_checkpoint|VACUUM|migrat" --include=*.py --include=*.sql swarmsync/` -> no hits.


## [P2] events table is append-only with no pruning, rotation, or retention -- and is simultaneously claimed as the replayable source of truth
**swarmsync/blackboard/schema.sql:62**

CLAIM: Every heartbeat writes an events row (leases.py:212, one per lease per beat, default interval 5s per runner.py:49). Nothing ever deletes from events, no rotation, no retention policy, no VACUUM, and no WAL checkpointing beyond SQLite's automatic one. I measured the real cost rather than speculating, and the growth is modest -- this is a genuine P2, not the resource-exhaustion P1 the dimension brief anticipated. The sharper point is the contradiction: schema.sql:4 and events.py:14 both assert 'events ... is the source of truth for recovery: parcels/leases/pheromone are projections replayable from it', yet (a) no replay code exists anywhere, and (b) any future pruning would destroy the very property being claimed. The table is also unindexed apart from the `seq` PK, which is fine for `tail()`'s `seq > ? ORDER BY seq LIMIT` but means any future type/agent/ts query is a full scan.

SCENARIO: A long-lived deployment (10 agents x 5 leases, 24h/day) accrues 864k heartbeat rows/day ~= 45 MB/day of pure heartbeat noise that never ages out, indefinitely, on a DB nothing ever compacts.

EVIDENCE: Measured in a mktemp workspace by emitting 50,000 real heartbeat events through events.emit and checkpointing:
  50000 heartbeat events -> db 2.61 MB  (52 bytes/row incl. index)
    4 agents x 3 leases x 8h   -> 69,120 rows ~ 4 MB
    10 agents x 5 leases x 24h -> 864,000 rows ~ 45 MB
    4 agents x 3 leases x 720h -> 6,220,800 rows ~ 325 MB
  indexes on events: []
Grep: no `DELETE FROM events`, `retention`, `VACUUM`, or `wal_checkpoint` anywhere in swarmsync/.


## [P1] R3's gate timeout does not bound the gate: proc.communicate() after killpg has no timeout, so a branch can still wedge the global integrate_lock forever
**swarmsync/coordinator/integrator.py:300**

CLAIM: P1-4's fix is meant to convert "the coordinator is dead until someone restarts it" into an ordinary rejection (its own comment, integrator.py:93-101). It does not. On TimeoutExpired the code calls `_kill_process_group(proc)` and then `stdout, stderr = proc.communicate()` with NO timeout. `communicate()` blocks until EOF on both pipes, not until the direct child dies. SIGKILL to the gate's process *group* does not reach any descendant that put itself in a different session (`setsid` / `subprocess.Popen(..., start_new_session=True)` / a double-forking daemon), and such a descendant still holds the inherited write end of the gate's stdout/stderr pipes. `run_impact_tests` then blocks for the descendant's full lifetime — while `app.post_integrate` (app.py:468) holds the ONE process-wide `integrate_lock` — which is precisely the permanent global wedge P1-4 exists to prevent. `start_new_session=True`, added by the same fix, is what makes the pipes survive: it is the reason the killpg is needed and the reason a re-`setsid`'d grandchild escapes it. Note the escape needs the gate's real fd 1 to reach the grandchild, i.e. pytest's fd-capture must be off — which the merged, agent-authored repo controls (`addopts = -s` in its own pytest.ini/pyproject, a common setting for live logs). The gate runs repo code by design, so this is in scope under the real trust model: it is availability, not privilege.

SCENARIO: Merged branch's repo has `pytest.ini` containing `addopts = -s` (capture off, so the gate's PIPE is pytest's real fd 1) and a test that starts a background service via `subprocess.Popen([...], start_new_session=True)` and then hangs. SWARMSYNC_GATE_TIMEOUT elapses; `_kill_process_group` SIGKILLs pytest's group; the daemonized grandchild is in its own session, survives, and keeps the stdout pipe open. `proc.communicate()` (line 300) blocks for as long as the grandchild lives. `integrate()` never returns, `integrate_lock` is never released, and every other agent's POST /integrate queues forever — with trunk still carrying the un-gated merge commit, since `_reject_and_reset` is never reached.

EVIDENCE: Reproduced against the real code (venv, /tmp copy, no repo mutation). Script built a repo with `pytest.ini` = `[pytest]\naddopts = -s\n` and `tests/test_daemon.py`:
    import subprocess, time
    def test_starts_a_background_service():
        subprocess.Popen(['sleep','120'], start_new_session=True)
        time.sleep(120)
with SWARMSYNC_GATE_TIMEOUT=2, calling `integrator.run_impact_tests(repo, [], test_dir="tests")` on a thread.
Output: `gate timeout was 2s. elapsed: 30.0 ->  *** run_impact_tests STILL BLOCKED after 30s ***`
(Control: the identical test WITHOUT `addopts = -s` returns `(False, 2.01)` — i.e. the timeout works only while pytest's fd-capture happens to hold the pipe for us.)
Offending code, integrator.py:298-300:
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        stdout, stderr = proc.communicate()      # <-- no timeout
Fix: `proc.communicate(timeout=N)` inside its own try, and/or close proc.stdout/proc.stderr before the final read.


## [P2] _reject_and_reset's rollback re-index silently drops integrate()'s `threshold`, so the "restore the blackboard" fix restores it to a state trunk never had
**swarmsync/coordinator/integrator.py:430**

CLAIM: The success path re-indexes with the caller's freeze policy (`index_kwargs = {} if threshold is None else {"threshold": threshold}; run_index(conn, repo, **index_kwargs)`, lines 527-528). P1-5's new rollback re-index does NOT: it calls `run_index(conn, repo)` bare, defaulting to `FREEZE_THRESHOLD=3`. For any deployment using a non-default threshold (`POST /index` exposes `threshold` on IndexBody; `integrate(threshold=...)` is a public param), a rejected merge re-derives contracts under a *different* policy than the one the blackboard was built with. Because `extract_contracts` no longer selects those symbols, `run_index` stamps `Parcel.contract_hash = None` and `upsert_parcels` writes `contract_hash=excluded.contract_hash` (store.py:98-116) — NULLing it — while `upsert_contracts` early-returns on the now-empty contract list (store.py:154-155), leaving the `contracts` row intact. That directly violates store.py's own stated invariant (line 16-17): "so a parcel row's contract_hash and the corresponding contracts.type_hash row always agree." The fix intended to make rollback a faithful restore; on this configuration it actively introduces divergence that neither pre-merge nor post-merge trunk ever had, and it is permanent (no pruning, and only a re-index at the right threshold repairs it). Blast radius is contained because no production code path reads `parcels.contract_hash` (broker.py:209-211 derives `frozen_ids` from the `contracts` table), which is why this is P2 rather than P1.

SCENARIO: Operator indexes with `run_index(conn, repo, threshold=1)` (or `POST /index {"threshold": 1}`), freezing `mod_a.py::helper` (blast_radius 1). An agent submits a branch that edits mod_a.py and breaks a test. `integrate(..., threshold=1)` merges, the gate goes red, `_reject_and_reset` resets git correctly, then re-indexes at the DEFAULT threshold 3. `parcels.contract_hash` for `mod_a.py::helper` goes from its real hash to NULL and stays there, while `contracts.type_hash` still holds that hash — trunk's bytes are correct, the blackboard's freeze bookkeeping is not.

EVIDENCE: Reproduced against the real code (venv, /tmp repo):
  BEFORE       parcels.contract_hash='dd299ba5c7fe...' contracts={'type_hash': 'dd299ba5c7fe...', 'version': 1}
  integrate status: merge_rejected
  AFTER REJECT parcels.contract_hash=None            contracts={'type_hash': 'dd299ba5c7fe...', 'version': 1}
  git trunk restored: True
  contract_hash SURVIVED rollback identically: False
Code, integrator.py:429-430 (inside `_reject_and_reset`):
    try:
        run_index(conn, repo)                    # <-- no threshold
compared with integrator.py:527-528 on the success path:
    index_kwargs = {} if threshold is None else {"threshold": threshold}
    index_result = run_index(conn, repo, **index_kwargs)


## [P2] P0-2's _ensure_parcel bypasses the classifier's exclusion policy: the hook mints permanent parcel rows under .git/, node_modules/, .venv/
**swarmsync/server/leases.py:58**

CLAIM: `_ensure_parcel` INSERTs a parcel row for whatever id it is handed, with no filter. The hook reaches it for any path `_relpath` accepts (adapter.py:176-202), which only rejects paths resolving *outside* repo_root. The classifier, the other writer of this table, deliberately excludes `_SKIP_DIRS = {'__pycache__', '.git', '.venv', 'venv', '.pytest_cache', 'node_modules'}` plus every dot-prefixed path component (indexer.py:52, 237). The two parcel-creation paths now disagree about what a parcel *is*. Consequences: (a) rows the classifier will never emit, and which store.py has "no stale-row pruning" for (its own docstring, line 38-45), accumulate without bound — `GET /parcels` is read by every agent at protocol step 1 (runner.py:210) and by the broker; (b) the size/count ceilings the S3 hardening put on indexing (`DEFAULT_MAX_INDEX_FILES=5000`, `DEFAULT_MAX_INDEX_SECONDS=30`, `IndexLimitError`) do not exist on this path at all; (c) leases.parcel_id FK'd rows for `.venv/**` can never be cleaned up while a lease or pheromone references them. Under the real trust model (localhost, semi-trusted agents) this is not a security hole — it is table pollution and a policy split introduced by the P0-2 fix, hence P2.

SCENARIO: A hook subagent runs `Edit` on `node_modules/left-pad/index.js` (e.g. patching a dependency) or `.git/config`. precheck maps it to `node_modules/left-pad/index.js::<module>`, `_ensure_parcel` INSERTs a permanent `kind='file'` row. Over a long session across a JS repo, every touched node_modules file leaves a parcel row that no `run_index` will ever revisit, refresh, or retire, and that every agent downloads on every `GET /parcels`.

EVIDENCE: Reproduced end-to-end through `adapter.main(["precheck"], ...)` against a real `create_app` TestClient:
  parcels the hook created:
      {'id': '.git/config::<module>', 'kind': 'file', 'path': '.git/config'}
      {'id': 'node_modules/left-pad/index.js::<module>', 'kind': 'file', 'path': 'node_modules/left-pad/index.js'}
  classifier _SKIP_DIRS (never indexed by run_index): ['.git', '.pytest_cache', '.venv', '__pycache__', 'node_modules', 'venv']
Code, leases.py:77-84 — no path predicate of any kind:
    path, _, symbol = parcel_id.partition("::")
    conn.execute("INSERT OR IGNORE INTO parcels (id, path, kind, symbol, updated_at) VALUES (...)", ...)
(Verified separately that the coarse row does NOT shadow a later classifier parcel: store.py's `ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, ...` overwrites it, and _ensure_parcel's INSERT OR IGNORE no-ops against an existing real row. That half of the fix is sound. Also verified the two-statement acquire does not break CAS atomicity: db.connect uses isolation_level=None, so the INSERT OR IGNORE commits as its own transaction and the CAS INSERT...WHERE NOT EXISTS remains a single atomic statement.)


## [P2] P1-6 preserves the rejected agent's branch, but the first step of the retry it exists to enable deletes it
**swarmsync/agent/runner.py:324**

CLAIM: P1-6 changed `_cleanup_worktree(repo, agent_id, delete_branch=landed)` so a merge_rejected/needs_rebase run keeps `<agent_id>`, on the stated grounds that "this branch is the ONLY reference to the agent's commits. Deleting it there makes the work unreachable and destroys the rebase-and-resubmit path DESIGN §5.5 promises" (runner.py:136-142). But `run_agent` unconditionally begins with `git_ops.add_worktree(repo, agent_id, base_commit)` (runner.py:270), and `add_worktree` calls `_prune_stale_worktree`, whose last step is `git branch -D --end-of-options <name>` (git_ops.py:169). So the moment the same agent_id is re-dispatched — which IS the rebase-and-resubmit path — the preserved commits are force-deleted and become unreachable. The ref survives only until the agent retries, so the guarantee the fix claims is not delivered; it is delivered only to a human who inspects the repo between the rejection and the retry. Not a regression relative to R2 (which deleted it immediately), so P2 — but the fix's stated purpose is unmet, and reporting it as closed is inaccurate.

SCENARIO: agent-1's merge is rejected by the gate; R3's fix leaves branch `agent-1` at the work commit. The broker re-dispatches the same task to the same agent_id (`_run_task_once` retries per broker.py:353). `run_agent` -> `add_worktree(repo, 'agent-1', base)` -> `_prune_stale_worktree` -> `git branch -D agent-1`. The preserved commit is now referenced by nothing and is garbage-collectable; nothing rebased it, and the agent redoes the work from base.

EVIDENCE: Reproduced with the real git_ops (venv, /tmp repo):
  after rejected run, branch agent-1 -> fca98925ec8bed6438517513b0a0bd7881a96104 == work commit: True
  after rerun add_worktree, branch agent-1 -> 9a1ca34ab276bff99ba096e648ad486dfac9eeb6 (base = 9a1ca34ab276bff99ba096e648ad486dfac9eeb6)
  preserved work commit still reachable from any ref: NOTHING
Code, git_ops.py:169 inside `_prune_stale_worktree` (called by add_worktree at line 199):
    _run(["git", "branch", "-D", "--end-of-options", name], cwd=repo, check=False)


## [P1] heartbeat's TTL predicate is bound to a stale Python-side clock, so R3's P1-1 fix still permits two simultaneous live write leases on one parcel
**swarmsync/server/leases.py:201**

CLAIM: R3 closed P1-1 by adding `AND ttl_expires_at > :now` to heartbeat's UPDATE, and ROUND4 asks whether acquire/heartbeat/reap_once/_find_lease now agree. They agree *textually* but not *semantically*: `:now` is `time.time()` read in Python at leases.py:186, before `conn.execute` at :196. SQLite evaluates the WHERE clause when the statement is serialized, which can be arbitrarily later (GIL preemption, busy_timeout wait of up to 5s, threadpool queueing, the event-loop stall proved in my reaper finding). So the predicate answers 'was this lease alive when I read the clock?', not 'is it alive now?'. If the lease expires inside that gap while another agent lawfully acquires the parcel via acquire()'s lazy expiry, the late UPDATE still matches, returns True, and pushes the dead lease's ttl_expires_at back into the future -- reviving it into a second live write lease. This is exactly the double-lease P1-1 was supposed to eliminate. The asymmetry matters and is specific to heartbeat: a stale (earlier) `:now` in acquire's `l.ttl_expires_at > :now` makes a conflict MORE likely to be seen (fails safe, denies), and a stale `now` in reap_once's `ttl_expires_at <= ?` reaps FEWER rows (fails safe). Only heartbeat's comparison points the unsafe way. The module docstring's promise -- 'a race to double-lease one parcel simply loses on this CAS and serializes instead of corrupting anything' -- is therefore false; it holds only because the default TTL (30s server / 300s hook) happens to dwarf typical scheduler jitter. `ttl` is a caller-supplied, unvalidated knob (POST /lease {"ttl":...}, run_agent(lease_ttl=), SWARMSYNC_LEASE_TTL), so any deployment that shortens it toward request latency reopens P1-1 in full. Fix is the one ROUND4 §1 already proposed: derive all four predicates from one SQL expression evaluated by SQLite at write time (e.g. unixepoch('subsec')) rather than from a per-caller Python timestamp.

SCENARIO: agent-A holds write lease id=1 on p.py::<module> with ttl=0.30s. A's heartbeat calls time.time() (lease still valid, predicate will pass) and is then delayed 0.40s before SQLite serializes the UPDATE. During the delay A's TTL lapses and agent-B calls acquire() on the same parcel: lazy expiry sees id=1 expired, so B is GRANTED id=2 (ttl 5.0s). A's delayed UPDATE then executes, its stale `:now` still satisfies `ttl_expires_at > :now`, and it returns True -- setting id=1's ttl 1.0s into the future. Both id=1 (agent-A) and id=2 (agent-B) are now status='active' with ttl_expires_at in the future on the same parcel: two agents each believe they hold the exclusive write lease.

EVIDENCE: Deterministic repro (real leases.py, real WAL DB; the only injection was a sleep placed between Python's `now = time.time()` and SQLite serializing the UPDATE -- it schedules an interleaving, it changes no predicate or value):
  agent-A acquires write lease id=1, ttl=0.30s
  [A] heartbeat UPDATE built with its Python-side `now`; parking 0.4s before SQLite serializes it...
  agent-B acquires the SAME parcel while A is parked -> granted=True, id=2
  agent-A's delayed heartbeat UPDATE finally runs -> returned True
  provably-live write leases on p.py::<module> at t=1784225600.3022:
    id=1 agent=agent-A mode=write status=active ttl_expires_at=now+0.597s
    id=2 agent=agent-B mode=write status=active ttl_expires_at=now+4.947s
  -> 2 simultaneous live write leases.

Independently confirmed under real load with a checker that has NO clock-race of its own (it reads `now_after` AFTER the query returns and only reports rows with ttl_expires_at > now_after, so both rows were provably live at one real instant). 12 threads, 2 parcels, TTL 0.05s, 15s:
  instants with >1 provably-live WRITE lease on one parcel: 150355
  example, at t=1784225584.7742 on f0.py::<module>:
    lease id=1 agent=agent-0 mode=write ttl_expires_at= now + 0.0154s
    lease id=4 agent=agent-4 mode=write ttl_expires_at= now + 0.0371s
A 16-thread/4-parcel run also showed 0 sqlite errors, 0 FK orphans, and exactly 4 parcel rows despite 16 threads racing `_ensure_parcel` on the same new ids -- so the two-statement acquire introduced by the P0-2 fix is NOT itself a defect (refuted).


## [P1] The hook's _keepalive discards heartbeat's False, so an agent whose lease was lawfully taken by another agent is still ALLOWed to edit the shared working tree
**swarmsync/hooks/adapter.py:267**

CLAIM: `_keepalive` calls `client.heartbeat(...)` and throws the result away; `cmd_precheck` (adapter.py:305) then unconditionally `return None` == ALLOW. R3's P1-1 fix makes that heartbeat correctly return {"ok": false} once the lease has expired -- but the one consumer that has no worktree isolation never looks. cmd_precheck's own docstring says 'already leased by THIS agent_id -> refresh the TTL and ALLOW', treating the GET /leases read as authority; between that read and the heartbeat there are two HTTP round trips, and if the TTL lapses in that window another agent's acquire lawfully wins (lazy expiry) while this agent is still waved through. The blackboard's lease invariant is not violated -- exactly ONE lease row exists -- which is what makes this worse than the P1-1 row-level bug: the collision is invisible in the blackboard, so no reaper, no event, and no /leases query can detect it. This is the same surface as R3's P0-2 ('hook subagents share ONE working tree, so the lease was the only protection'), reopened by a different mechanism, and the fix is one line: honor the return value -- on False, fall through to the acquire path (which already handles both grant and deny) instead of allowing. Note the ~ms window at the default 300s hook TTL is widened arbitrarily by any server stall between the two calls -- see the reaper event-loop finding, which I measured at 4.98s. cmd_postupdate (adapter.py:389-391) has the same discard.

SCENARIO: agent-A holds the hook write lease on shared.py::<module>. A's next Edit fires precheck: GET /leases returns A's own still-valid lease, so cmd_precheck takes the `owner == agent_id` branch. A's TTL lapses before its POST /heartbeat is serialized. agent-B prechecks the same file, sees no live lease, and is GRANTED the write lease. A's heartbeat then returns {"ok": false} -- A demonstrably holds nothing -- but _keepalive discards it and cmd_precheck returns ALLOW. agent-A and agent-B both edit shared.py in the SAME working tree (hook subagents have no worktree isolation), while /leases shows a single, perfectly consistent holder.

EVIDENCE: End-to-end with the real adapter.main(), real server/app.py, real leases.py, reaper OFF (pure lazy expiry). The only injection was adapter.main's own documented test-only `http_factory` seam, used to park agent-A between its GET /leases and its POST /heartbeat:
  1. agent-A's first precheck on shared.py -> ALLOW
     active leases: [(1, 'agent-A', 'write')]
  2. [A] GET /leases -> [(1, 'agent-A')]
     [A] ... pausing 1.6s (its lease TTL lapses here) ...
  3. agent-B prechecks the SAME file -> ALLOW
     [A] POST /heartbeat -> {'ok': False}   <-- return value is DISCARDED by _keepalive()
  4. agent-A's precheck resumes -> ALLOW
  --- result ---
  active leases on shared.py::<module>: [(2, 'agent-B', 'write')]
  agent-A decision: ALLOW | agent-B decision: ALLOW
  heartbeat responses A discarded: [{'ok': False}]
  *** BOTH AGENTS ALLOWED to Edit shared.py in the SAME working tree, while the
  *** blackboard shows exactly ONE lease holder.


## [P1] The reaper's blocking SQLite on the asyncio loop thread stalls the ENTIRE ASGI server for 5s and then kills the reaper permanently, silently, and breaks app shutdown
**swarmsync/coordinator/reaper.py:166**

CLAIM: R3 rated 'one exception kills the reaper forever' a P2; ROUND4 §2 asks whether it is the expected outcome under load. It is, and the consequences I measured are new and specific. `run()`'s loop body has no try/except and calls blocking sqlite3 directly on the event-loop thread (app.py:239 creates it with asyncio.create_task, not a threadpool). Three distinct consequences, all measured on a real uvicorn server: (1) A single contended write blocks the loop for the full busy_timeout -- I measured one GET /leases at 4.98s while all other requests in the same window returned in 2ms, i.e. the whole server froze, nothing to do with the read itself. That 4.98s exceeds adapter._DEFAULT_TIMEOUT_SECONDS = 2.0, and the adapter's FAIL-OPEN umbrella turns a timeout into a silent ALLOW -- so a stalled reaper makes every concurrent hook precheck fail open on the one surface with no worktree isolation. (2) The OperationalError then propagates out of run(), the task dies, and NOTHING logs it or notices; after the contention cleared and 4 further seconds of the 1s reaper cadence, the expired lease was still status='active' with zero 'reaped' events -- permanently, for the life of the process. (3) The lifespan's `await task` (app.py:250) re-raises the dead task's exception, so shutdown fails ('Application shutdown failed. Exiting.') and neither reaper_conn.close() nor conn.close() below it ever runs. What degrades once it is dead: acquire()'s lazy expiry keeps mutual exclusion correct (so this is not P0), but DESIGN §6's documented crash-recovery signal -- 'the coordinator/broker observes the reaped event and reassigns the task' -- never fires again, and pheromone decay stops forever, freezing every strength at its drop value and turning DESIGN §2's decaying trail into a permanent 'everyone is working everywhere' smear. I refuted two candidate triggers rather than assume them: run_index holds BEGIN IMMEDIATE for only 17ms on an 800-file/4000-parcel repo, and one reaper pass over 200,000 pheromone rows costs 1.22s -- so the realistic trigger is an external/long writer on the persistent DB, not the server's own paths. The fix is three-fold and cheap: run the pass via run_in_threadpool, wrap the loop body in try/except with a log, and add a done-callback so a dead reaper is visible.

SCENARIO: Any writer holds SQLite's write lock on the blackboard longer than busy_timeout=5000ms (an external process, a backup, a `sqlite3` shell, a slow/contended volume). The reaper's next UPDATE blocks the event loop for 5s -- during which uvicorn serves no requests at all, so every hook precheck exceeds its 2s httpx timeout, hits main()'s fail-open umbrella, and ALLOWs an unleased edit in the shared working tree. Then the UPDATE raises `database is locked`, run()'s unguarded loop propagates it, the task dies with no log, and for the rest of the process's life no lease is ever marked reaped, no `reaped` event is ever emitted, and no pheromone ever decays. At shutdown, `await task` re-raises and the lifespan dies before closing its connections.

EVIDENCE: Real app under real uvicorn (reaper_interval=1.0), with one competing connection holding BEGIN IMMEDIATE for 12s:
  seed lease: {'granted': True, 'lease_id': 1, 'reason': None}
  --- write lock held for 12.0s ---
  GET /leases latencies during lock hold:
     [0.002, 0.002, 4.979, 0.002, 0.002, 0.002, ...]
    worst read latency while a writer held the lock: 4.98s
  --- lock released ---
  lease 1 status after lock released + 4s of reaper cadence: 'active'
  'reaped' events in log: 0
and from the server's own stderr:
  ERROR:  File ".../swarmsync/server/app.py", line 250, in lifespan
            await task
          File ".../swarmsync/coordinator/reaper.py", line 168, in run
            reap_once(conn, now)
          File ".../swarmsync/coordinator/reaper.py", line 96, in reap_once
            rows = conn.execute(
          sqlite3.OperationalError: database is locked
  ERROR:    Application shutdown failed. Exiting.
Refuted sub-theories (measured, not assumed):
  run_index: 800 .py files -> 4000 parcels; BEGIN IMMEDIATE hold windows: [0.017] -> longest exclusive write-lock hold 0.02s.
  decay_once: 5000 agents / 200000 pheromone rows -> one reaper pass blocks the event loop for 1.22s (linear; ~350k rows to cross the hook's 2s timeout).


## [P2] _find_lease / _find_holder ignore the lease `mode`, so the hook lets a read-lease holder write and lets a read lease block another agent's edit
**swarmsync/hooks/adapter.py:254**

CLAIM: Both helpers match on parcel_id alone and never look at `lease['mode']`, so cmd_precheck's entire allow/deny decision is mode-blind in both directions. (a) An agent holding only a READ lease is treated as the rightful owner of the file and is ALLOWed to Edit it -- a read lease authorizes a write, which is precisely what DESIGN §5.2's conflict rule exists to prevent. (b) Conversely, a read lease -- documented as 'mutually shared' -- DENIES every other agent's edit, so read leases are simultaneously too weak and too strong. Related and noted by R3: _find_lease returns the FIRST matching row, and GET /leases is ORDER BY id, so with several shared read leases on one parcel only the lowest-id holder is ALLOWed and everyone else is DENIED. That accident is the only thing preventing (a) from becoming a multi-writer bug, and it is not robust: if the lowest-id read lease lapses between two agents' GET /leases calls, each can see itself as the first live holder and both get ALLOW. Reachability is the honest limit on severity: nothing in the default configuration issues a read lease -- broker.py's docstring is explicit that 'read-dependencies are fetched, not leased' -- so this needs run_agent(lease_mode='read') or a direct POST /lease, which are both supported public surfaces. Hence P2, not P1.

SCENARIO: Agent 'reader' takes a legitimate read lease on shared.py::<module> (POST /lease mode='read', or run_agent(lease_mode='read')). It then Edits shared.py: cmd_precheck's _find_lease matches on parcel_id only, sees owner == 'reader', keepalives, and ALLOWs -- a read lease has authorized a write. Meanwhile agent 'other' tries to edit the same file and is DENIED by a lease whose whole documented point is that it is shared and non-exclusive.

EVIDENCE: Real adapter + real app:
  === A. _find_lease / _find_holder ignore lease `mode` ===
    agent 'reader' takes a READ lease on shared.py::<module>: {'granted': True, 'lease_id': 1, 'reason': None}
    'reader' now Edits the file via the hook  -> ALLOW   <-- a read lease authorizes a WRITE
    agent 'other' (no lease) Edits the file   -> DENY    <-- blocked by a *read* lease, which is supposed to be shared


## [P2] release() is the only lease predicate without a ttl clause, so an agent can 'release' a parcel another agent already lawfully holds, corrupting the event log
**swarmsync/server/leases.py:220**

CLAIM: Completing ROUND4 §1's predicate diff -- acquire (status='active' AND ttl_expires_at > :now), heartbeat (…AND ttl_expires_at > :now, added by R3's P1-1), reap_once (status='active' AND ttl_expires_at <= ?), and GET /leases -> _find_lease (status='active' AND ttl_expires_at > ?) all carry a ttl predicate. release() alone does not: both its SELECT (:220) and its UPDATE (:230) scope only to (id, agent_id, status='active'). Under lazy expiry an expired row keeps status='active' until the reaper touches it -- and per this finding-set's reaper bug, the reaper may be dead and never touch it -- so a former holder's release() succeeds long after the parcel was lawfully re-granted, and emits a `released` event naming that parcel. events is documented (schema.sql:3, DESIGN §4.1) as append-only and 'the source of truth for recovery: parcels/leases/pheromone are projections replayable from it', so the log's last word on the parcel is that it is free while another agent is actively holding it. No shipped consumer replays the log today, which caps this at P2 -- but it falsifies the stated recovery model, and it is reachable in ordinary operation: runner.py:304 releases every lease unconditionally after /integrate, whose pytest gate has a 600s default timeout against a 30s default TTL.

SCENARIO: Agent A holds write lease id=1 on q.py::<module>. A stalls past its TTL (e.g. runner.py's /integrate gate outruns it, or its heartbeats fail). Agent B lawfully acquires id=2 via lazy expiry. A then reaches runner.py:304 and calls release(id=1); with no ttl predicate the row is still status='active', so release returns True and emits `released {parcel_id: q.py::<module>}` at seq=3 -- after B's `lease_granted` at seq=2. Any replay of the append-only log concludes q.py::<module> is free while B is holding and editing it.

EVIDENCE: Real leases.py against a real WAL DB:
  agent A acquires lease id=1 (ttl 0.2s)
  A's lease lapses; agent B lawfully acquires id=2
  A now calls release(id=1) on the lease it no longer holds -> True
  resulting event log (the documented source of truth for replay):
    seq=1 lease_granted  agent=A {"parcel_id": "q.py::<module>", "lease_id": 1, "mode": "write"}
    seq=2 lease_granted  agent=B {"parcel_id": "q.py::<module>", "lease_id": 2, "mode": "write"}
    seq=3 released       agent=A {"lease_id": 1, "parcel_id": "q.py::<module>"}



(total findings from completed dimensions: 28)
