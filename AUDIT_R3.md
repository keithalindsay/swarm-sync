# Round 3 Audit — swarm-sync (Pheromesh)

## 1. Verdict

**Does not meet the "proud to ship" bar.** It fails on the two criteria that matter most for this product.

| Bar criterion | Status |
|---|---|
| ruff + mypy clean | **Pass** (25 source files) |
| Suite green, no flakes | **Pass** (248/248) |
| Suite *meaningful* | **Fail** — Round 2's headline fixes are unguarded; the rollback and 5 of 7 auth dependencies can be deleted with 248/248 still green |
| Concurrency invariants hold under stress | **Fail** — mutual exclusion on a parcel is breakable on the *happy path*, reproduced |
| Demo money shots pass standalone | Pass, but #1 ("zero conflicts") passes by luck of source formatting, not by mechanism |
| Sound error handling | **Fail** — unguarded reaper loop; no timeout on the pytest gate under a global lock |
| No confirmed P0/P1 | **Fail** — 2 P0, 8 P1 |
| DESIGN.md matches code | **Fail** — contract subsystem inert at default granularity; §5.5 "forced rebase" has no implementation |
| README usable by a stranger | **Fail** — the flagship Claude Code path silently degrades to zero enforcement if `SWARMSYNC_ROOTS` is unset, and the var is undocumented |

**What blocks ship, minimally:**

1. **P0 — runner stops heartbeating before `/integrate`.** Any test gate longer than the 30s TTL gets the working agent's lease reaped mid-flight and hands the same parcel, in write mode, to a second agent. This is the ordinary case on a real repo (impact selection defaults to the full suite), not an edge case.
2. **P0 — the hook path fails open on any file the classifier never indexed.** Every non-`.py` file and every new file is completely ungated, and hook-driven subagents share **one working tree**, so there is no worktree isolation to fall back on. Last writer wins, silently, while the skill tells the user leases are enforced.
3. **P1 cluster on the lease fabric itself** — `heartbeat` has no TTL predicate (expired leases resurrect); `add_worktree`'s guard misses path traversal into `shutil.rmtree`; `Bash` edits bypass the gate entirely.
4. **The test suite does not defend Round 2's own work.** Mutation testing shows the atomic-integrate rollback and the auth on `/integrate`, `/release`, `/parcel/update` can each be deleted with the full suite green. The fixes are real; the guards around them are not.

The honest summary: Round 2 fixed the specific bugs Round 1 named, and fixed them correctly. It did not fix the *classes* those bugs belonged to, and in one case (the global `integrate_lock` with no timeout on what it serializes) the fix created a new P1.

---

## 2. Confirmed P0 / P1

### P0-1 — Agent stops heartbeating before `/integrate`; its lease is reaped mid-flight and re-granted to another agent
`swarmsync/agent/runner.py:254`

`run_agent` calls `heartbeater.stop()` in the `finally` of the inner try that wraps only `add_worktree`/`mutator`/`commit_all`. Step 6 — `parse_file`, `POST /parcel/update`, and `POST /integrate` (which runs the full impact-selected pytest gate) — runs with **zero heartbeats**. Leases keep their last `ttl_expires_at` (`DEFAULT_TTL_SECONDS = 30.0`).

**Scenario (reproduced against real `runner.py` + `leases.py` + `reaper.py`):** agent-a acquires a write lease on `mod_a.py::<module>` (ttl 3s), commits in <1s, POSTs `/integrate`; the gate takes 6s. At t=3s the reaper flips agent-a's lease to `reaped` while agent-a is still working. At t=3.1s agent-b calls `acquire('mod_a.py::<module>', mode='write')` and is **granted**.

```
agent-a: integrate START t=0.0s (still holds the parcel, per its protocol)
>>> agent-b acquire(... write) WHILE agent-a integrates: granted=True lease_id=2
agent-a final status: done
  lease: {'id': 1, 'agent_id': 'agent-a', 'status': 'reaped'}
  lease: {'id': 2, 'agent_id': 'agent-b', 'status': 'active'}
```
Note **zero** beats fired at all — the first beat is due at `interval` and `stop()` lands first. The broker compounds it by keying reassignment off the `reaped` event, so it also dispatches a duplicate agent for a task that is about to finish. `run_impact_tests`' documented default when selection matches nothing is the *full suite*, so exceeding a 30s TTL is the normal case on a real repo.

**Fix:** move `heartbeater.stop()` to the **outer** `finally`, so it covers `/parcel/update` and `/integrate`. Additionally, the integrator should not depend on the client's liveness at all: `integrate` should validate that the submitting agent still holds an active lease on the target parcels and should hold/extend that lease for the duration of the gate, rather than trusting an out-of-band heartbeat thread to outlive a subprocess of unbounded runtime.

---

### P0-2 — Hook adapter fails OPEN for any unindexed file (all non-`.py`, all new files), with no worktree isolation behind it
`swarmsync/hooks/adapter.py:309` · root cause `swarmsync/server/leases.py:110`

`cmd_precheck` maps every edit target to `<relpath>::<module>` and calls `client.lease(...)`. `leases.parcel_id` is an FK to `parcels(id)` with `foreign_keys=ON`, so leasing a parcel the classifier never emitted raises `IntegrityError` → 500 → `raise_for_status()` → the module's deliberate fail-open umbrella in `main()` → exit 0, **no deny, no lease**. `indexer.index_repo` only indexes `.py`, and a file created during a session has no parcel until the next `POST /index`.

This is far worse on the hook surface than in the broker: hook-driven Claude subagents share **one** working tree (`_repo_root = payload['cwd']`). DESIGN §5.1's "two agents can never share a working tree" physical isolation does not exist here, and there is no integrator or test gate either. The lease is the *only* collision protection on that surface, and for these files it is absent.

**Scenario (verified end-to-end against real `create_app` + `adapter.main`):**
```
{'root': '/tmp/tmpydsb_aw4', 'parcels': 2, 'contracts': 0}
A pkg.json: (0, '', "precheck: failing open (IntegrityError('FOREIGN KEY constraint failed'))")
B pkg.json: (0, '', "precheck: failing open (IntegrityError('FOREIGN KEY constraint failed'))")
leases: []
A newmod.py / B newmod.py: same — both ALLOW, no lease
A mod.py: (0, '', '')                       <- indexed .py: lease acquired
B mod.py: deny "m.py is leased by A"        <- correctly denied
```
Two subagents told to add a dependency to `package.json`, or both creating `swarmsync/newmod.py`, both write the same file in the same cwd. Last writer wins and silently destroys the other's edit.

**Fix, two parts:**
1. Distinguish "blackboard is down" (transient → correctly fail open) from "this parcel does not exist" (permanent, deterministic property of the file). `leases.acquire` should catch the FK violation and return a structured `unknown_parcel` result, not a 500.
2. Decide the policy for unknown parcels and state it in the docs. For the shared-working-tree hook path the only safe answer is to lease **path-keyed**, not parcel-keyed: auto-create a coarse `<relpath>::<file>` parcel on demand so *any* file — `.ts`, `.yaml`, `Dockerfile`, brand-new `.py` — is coordinated. Fail-open on an unindexed file is not a degraded mode; it is the absence of the product.

---

### P1-1 — `heartbeat()` has no TTL predicate: an expired-but-unreaped lease can be resurrected → two active write leases on one parcel
`swarmsync/server/leases.py:149`

The two predicates disagree:
- `leases.py:97` (`acquire`, lazy expiry — an expired lease does not block): `AND l.ttl_expires_at > :now`
- `leases.py:153` (`heartbeat`, no ttl check at all): `WHERE id = :lease_id AND agent_id = :agent_id AND status = 'active'`

So once lease L has expired but its row is still `status='active'` (the reaper hasn't ticked), agent B can lawfully acquire the parcel **and** agent A's in-flight heartbeat can push L's `ttl_expires_at` back into the future. Both rows are then `active AND ttl_expires_at > now`, `mode='write'`, same parcel.

The module docstring asserts the opposite — "so a stale/foreign/**expired** heartbeat is a silent no-op … rather than reviving a lease." The `expired` clause is simply not implemented. Mutual exclusion holds only if the reaper flipped `status` first, contradicting the same file's note that "correctness here does not depend on the reaper having run first." The window is real: the reaper ticks at `DEFAULT_INTERVAL=1.0s`, `create_app(reaper_interval=None)` disables it entirely, and the reaper task can die permanently (P1-6), making the double lease permanent. `runner.py`'s `_Heartbeater` beats blindly every 5s on `_lease_ids` and swallows every exception — exactly the client that heartbeats with no idea its lease expired.

**Reproduced** (no reaper — a supported config, and equivalent to the gap between ticks):
```
A granted: True lease 1
B granted: True lease 2
A's post-expiry heartbeat returned: True   <-- docstring says this must be False
ACTIVE, UNEXPIRED write leases on ONE parcel: [(1,'agent-A','write'), (2,'agent-B','write')]
AssertionError: DOUBLE LEASE: 2 agents hold the same parcel
```
Downstream, `hooks/adapter.py::_find_lease` returns the FIRST matching row, so a third agent is denied naming an arbitrary one of the two holders while both A and B write the file.

**Fix:** one clause — `AND ttl_expires_at > :now` in `heartbeat`'s WHERE. Add a test that asserts a post-expiry heartbeat returns False *with no reaper running*.

---

### P1-2 — `add_worktree`'s arg-injection guard misses path traversal; `shutil.rmtree` deletes directories outside the repo
`swarmsync/worktree/git_ops.py:158`

`_reject_option_like` (git_ops.py:70) — the module's only documented SECURITY guard for user-derived names — checks only `str(value).startswith("-")`. `add_worktree` then builds `worktree_path = repo / ".worktrees" / name` and passes it to `_prune_stale_worktree`, which calls `shutil.rmtree(worktree_path, ignore_errors=True)` (git_ops.py:135) **before any git process runs**. `..` escapes the repo; an absolute `name` discards the repo prefix entirely via pathlib join semantics.

```
'../../../tmp/evil' -> /repo/.worktrees/../../../tmp/evil
'/etc/passwd'       -> /etc/passwd        # repo prefix discarded
```
Reachable in normal use: the broker derives the worktree name from `task.task_id`, and `agent/runner.py:251` passes it straight to `add_worktree`.

**Live reproduction (mktemp sandbox):**
```
.worktrees now exists: True
victim file BEFORE: True
add_worktree raised: GitOpsError
victim file AFTER : False
victim dir  AFTER : False
```
The `GitOpsError` is raised only *after* the data is gone. Note the first call in a fresh repo silently no-ops (`.worktrees/` doesn't exist yet → `exists()` is False) — the deletion fires from the second agent onward, which is exactly why tests miss it.

**Fix:** validate `name` against `^[A-Za-z0-9._-]+$` (rejecting `..`, `/`, and leading `-` together), and additionally assert `worktree_path.resolve().is_relative_to((repo / ".worktrees").resolve())` before any `rmtree`. Round 2 hardened this function against option injection and stopped there; the sink is recursive deletion, not a bad git ref.

---

### P1-3 — Bash-mediated edits bypass the lease gate entirely
`swarmsync/hooks/adapter.py:101`

`EDIT_TOOLS = frozenset({"Edit","Write","MultiEdit","NotebookEdit"})`; `cmd_precheck` returns an immediate ALLOW for everything else (`adapter.py:288-289`), and README:67 registers the PreToolUse matcher as `Edit|Write|MultiEdit|NotebookEdit`. `Bash` is not gated, so `sed -i`, `cat > file`, `python - <<EOF`, `patch`, `git checkout` write to a leased file with no lease check, no deny, and no `/parcel/update`.

This is not an attack under the stated trust model — it is routine Claude agent behavior, and it is the *predictable* reaction to a deny. Agent B is denied on Edit, reads "pick different work or retry shortly," and falls back to `Bash: sed -i`. The write lands with no lease, the blackboard still shows A as sole owner, `postupdate` never runs so the content_hash stays stale, and both edits collide at merge.

Compounding: `_is_active` (adapter.py:170) keys off a `.swarmsync-active` marker file *inside the repo*, which any agent can delete via Bash — a fail-open kill switch writable by the very agents it constrains.

`grep -rn -i "bash|limitation|not gated|escape hatch" README.md DESIGN.md HARDENING.md` returns only code-fence hits. The gap is documented nowhere, including the swarmsync SKILL.md, while README:53 claims hooks enforce leasing "**transparently** … every `Edit`/`Write` a (sub)agent makes is gated."

**Fix:** add a `Bash` PreToolUse matcher that extracts write targets from the command (redirections, `sed -i`, `tee`, `patch`, `git checkout/apply`) and runs the same lease check; deny on parse ambiguity against a leased path rather than allowing. Move the activation marker outside agent-writable space (or make `SWARMSYNC_ACTIVE` authoritative). At minimum, document the gap prominently — the current docs make a guarantee the code does not make.

---

### P1-4 — The pytest gate has no timeout and runs while holding the global `integrate_lock`: one hanging test wedges ALL integration forever
`swarmsync/coordinator/integrator.py:242`

```python
result = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, env=env)
```
No `timeout=`. `grep -n "subprocess.run|timeout"` over the file returns only line 242. That call executes **untrusted, just-merged, agent-authored** test code. `server/app.py::post_integrate` holds `request.app.state.integrate_lock` across `await run_in_threadpool(integrator.integrate, ...)`, so the hanging pytest child holds the one global integration lock indefinitely.

Every subsequent `/integrate` from every other agent queues behind it forever. The serial test-gated integrator — the component the entire "trunk is never poisoned" guarantee rests on — is permanently dead, with no timeout, no cancellation, and no recovery short of a server restart. Trunk is left with the hanging branch's merge commit still on it (rollback only happens after the gate returns), so a restart resumes on a trunk that never passed the gate. The orphaned pytest child is never killed, and a client disconnect does not stop it.

**Proof** (`run_impact_tests` against a repo whose test hangs, 30s SIGALRM watchdog):
```
--> run_impact_tests STILL running after 30s: subprocess.run() has NO timeout=.
```
Corroborated through real `create_app` + TestClient: agent-hang integrating, healthy agent-ok integrating 2s later — agent-ok's `/integrate` never returned and had to be killed at the harness timeout.

**Round 2 made this strictly worse:** the `asyncio.Lock` fix converted a race into a single global chokepoint with no timeout on the thing it serializes.

**Fix:** `subprocess.run(..., timeout=GATE_TIMEOUT)` with a configurable default; on `TimeoutExpired`, kill the whole process **group** (`start_new_session=True` + `os.killpg`) — `subprocess`'s own timeout only kills the direct child, and pytest spawns. Treat timeout as `merge_rejected` via `_reject_and_reset`. Consider a per-repo lock rather than a process-global one.

---

### P1-5 — `integrate()`'s rollback resets git but never rolls back the blackboard
`swarmsync/coordinator/integrator.py:446`

The S1 atomicity guard (`except Exception: return _reject_and_reset('integration_error', ...)`) covers `git reset --hard pre_merge_sha` only. But inside the same guarded block, `run_index(conn, repo)` (integrator.py:446) has **already committed**: `store.upsert_parcels`/`upsert_contracts` each wrap their batch in `_db.transaction(conn)`, which COMMITs on exit (db.py:130). SQLite has one transaction per connection and no nesting (`db.transaction` explicitly refuses to nest), so nothing in `integrate` can undo them.

Any exception after `run_index` returns — the `regenerate_summary`/`UPDATE parcels` loop (450-456), the contract-diff SELECT (468), or a transient `database is locked` from another writer exceeding `busy_timeout` — takes the `integration_error` path. Trunk is reset byte-identically, but `parcels.content_hash`, `parcels.blast_radius`, `contracts.type_hash` and the **monotonic** `contracts.version` (bumped by `CASE WHEN contracts.type_hash != excluded.type_hash THEN version+1`, store.py:143) all remain derived from the merge that was thrown away. `upsert_contracts` can also fail after `upsert_parcels` committed, splitting the two tables.

Not cosmetic: `integrate` step 1's own `_check_read_deps` (119-129) compares agents' plan-time snapshots against exactly these columns. After one rejected merge, every other agent's read-dep check is evaluated against a phantom hash — some agents get spuriously bounced with `needs_rebase`, and an agent that re-snapshotted after the failure is cleared to merge against state that never landed.

**Reproduced:**
```
integrate status: merge_rejected
trunk rolled back to pre-merge sha: True
trunk calc.py on disk: 'def add(a, b):\n    return a + b\n'

blackboard parcels.content_hash for calc.py::add
  before merge : 8f75a68646c879fd
  after REJECT : 1b4b37d510189d65
  DB rolled back with trunk?  False
```
`_reject_and_reset` (324-364) issues only `git_ops.reset_hard(...)` plus an `events_mod.emit(...)` — no DB compensation of any kind. The module docstring's atomicity contract ("trunk is left byte-identical … so the log stays consistent") is true of git and false of the blackboard, which is the projection everything else reads.

*(Also reported independently as "a rolled-back integrate leaves the blackboard holding the rejected edit's content_hash and state_summary" — same defect, merged here. The `runner.py`-posted `/parcel/update` before `integrate` is a second, earlier source of the same drift.)*

**Fix:** re-run `run_index(conn, repo)` inside `_reject_and_reset` **after** the `reset_hard`, so the blackboard is re-derived from the restored trunk. That also repairs the `runner.py` `/parcel/update` drift for free. The `contracts.version` bump is not undoable by re-indexing (it's monotonic by design); accept the version gap explicitly and document it, or gate the bump on a landed merge.

---

### P1-6 — Round 2's worktree cleanup `git branch -D`s the agent branch on `merge_rejected`/`needs_rebase`, making rejected commits unreachable
`swarmsync/agent/runner.py:298`

The `finally: _cleanup_worktree(repo, agent_id)` added in `34d115d` runs `remove_worktree(..., delete_branch=True)` unconditionally after `client.integrate(...)` returns — **including** on `merge_rejected` (conflict, red gate, integration_error) and `needs_rebase`. Nothing else references those commits: the integrator explicitly `reset --hard`s trunk back to `pre_merge_sha`, so `git branch -D <agent_id>` makes the work unreachable (reflog only).

`_cleanup_worktree`'s justification — "A landed merge's commits already live in trunk's history, so deleting the now-redundant branch ref loses nothing" — is true **only on the landed path**, and the code does not distinguish. This directly defeats DESIGN §5.5's `needs_rebase` bounce-back ("bounce back to the agent" → rebase → resubmit): there is no branch left to rebase. `AgentResult.branch`/`commit_sha` are returned pointing at a deleted ref, with `status` reported as `"done"`.

**Reproduced** (real server + `run_agent`, mutator that breaks a test):
```
status: done | integrate: merge_rejected | result.branch: agent-x
branches after run_agent:
 * integration
does branch agent-x still exist? False
commit still reachable? False
```
The broker only retries on `lease_denied`, so no re-plan recovers it.

**Secondary defect in the same fix:** on the `lease_denied` path, `_cleanup_worktree` cannot do what its comment claims ("prune any worktree/branch this agent_id leaked on a PRIOR run") — `git_ops.remove_worktree` runs `git worktree remove` with `check=True` (git_ops.py:179), so with no worktree dir it raises `GitOpsError` and the `git branch -D` on line 181 is never reached.

**Fix:** pass the integrate status into `_cleanup_worktree` and only `delete_branch=True` on `merged`. Make `remove_worktree` tolerate a missing worktree dir and still attempt the branch delete. Report `status='rejected'`, not `'done'`, when integrate rejected.

---

### P1-7 — Span-disjointness (`co_schedulable` symbol mode) is not git-merge safety
`swarmsync/classifier/graph.py:355`

`co_schedulable(mode='symbol')` returns True iff byte spans don't overlap. But git's merge unit is a **line hunk with context**, not a byte span — two changes on *adjacent lines* in different spans conflict. DESIGN §2 states the opposite as the architecture's core rationale ("we lease at symbol level but merge with ordinary git, so two agents editing different functions in one file just produce non-overlapping hunks"), and money shot #1 asserts "zero conflicts."

When it does conflict, `integrator.integrate` treats it as touch-set misprediction and rejects (374-393) — but there was no misprediction; both agents obeyed their leases perfectly. Nothing recovers: `broker._run_task_with_retries` only retries on `lease_denied`, never on `merge_rejected`, and **no rebase exists anywhere in the codebase** — `grep -rn "rebase" --include=*.py swarmsync/ demo/` returns only `client.py:172/177` (docstrings), `models.py:37` (the status literal), and `integrator.py:13/97/304/315` (emit sites). DESIGN §5.5's "forced rebase before merge" is a label with no implementation. The loser's committed work is silently dropped (and per P1-6, its branch is deleted).

**Verified with real git worktrees + the real classifier:**
```
co_schedulable(symbol): True
merge a: (True, [])
merge b: (False, ['calc.py'])
```
(A edits the last line of `C.add`; B edits the first line of the adjacent `C.sub`.)

The demo passes only because `sample_repo`'s functions happen to be blank-line-separated and the mutators happen not to touch span-boundary lines — the guarantee holds by luck of source formatting.

**Fix, pick one and say so in DESIGN:** (a) require a gap of ≥ git's context size (3 lines) between co-scheduled spans, which makes the disjointness claim actually true of git's merge unit; or (b) implement the rebase-and-resubmit path DESIGN §5.5 already promises, so a conflict is recoverable rather than fatal; or (c) drop the "zero conflicts" claim and document symbol mode as best-effort. (a)+(b) together is the honest combination.

---

### P1-8 — The frozen-contract subsystem is inert in the DEFAULT file granularity
`swarmsync/coordinator/broker.py:307`

`extract_contracts` only emits contracts for parcels of kind function/class, so every id in `frozen_ids` looks like `core.py::helper` — never `core.py::<module>`. In `mode='file'` — DESIGN §2's own de-risking default, the default of `run`/`resolve_task`/`schedulable`/`group_schedulable`, and the granularity the hook adapter **hardcodes** (adapter.py:217) — `resolve_task` collapses every hint to `<file>::<module>`. Therefore:

1. `_run_task_once`'s `{pid: 'exclusive' for pid in target_parcels if pid in frozen_ids}` is **always empty**. The docstring's claim that this makes "to change a frozen contract you must take an exclusive lease" an "enforced runtime invariant rather than a convention" is false in the default config.
2. `co_schedulable`'s frozen clause (`a.id in frozen_ids`) never fires, so a task rewriting a frozen signature and a task editing its direct dependent land in the **same concurrent wave**.

All contract-awareness machinery (`contract_aware=True`, `load_scheduling_graph`, the frozen clause, the exclusive upgrade) is dead code unless the operator opts into the non-default — and separately unsafe (P1-7) — `mode='symbol'`.

**Verified against the real broker with a real indexed repo:**
```
frozen_ids: {'core.py::helper'}
mode=file:   contract task resolves to ['core.py::<module>']
  exclusive upgrade fires? False
  co-scheduled WITH its dependent concurrently? True
mode=symbol: contract task resolves to ['core.py::helper']
  exclusive upgrade fires? True
  co-scheduled WITH its dependent concurrently? False
```
Compounding: `exclusive` and `write` are indistinguishable in `leases.acquire`'s conflict predicate (`l.mode IN ('write','exclusive') OR :mode IN ('write','exclusive')`), so the upgrade is semantically a no-op even when it does fire.

**Fix:** in file mode, map `frozen_ids` up to their owning `<file>::<module>` parcel before both the exclusive-upgrade check and the `co_schedulable` frozen clause — a contract-changing task should be exclusive at whatever granularity is in use. Then either give `exclusive` a distinct meaning in `acquire` (blocks readers too) or delete the mode.

---

### P1-9 — Renaming or deleting a frozen contract emits NO `contract_change` and leaves a ghost contract served as current truth
`swarmsync/classifier/store.py:38`

`store.run_index` has "No stale-row pruning" by design: rows for vanished symbols are never deleted. `integrator.integrate`'s contract-change detection (466-486) skips a symbol when `before is None` (new) and when `before[1] == row['type_hash']` (unchanged). A **rename hits both escape hatches**: the old symbol's row is untouched (type_hash identical → skipped) and the new symbol has no `before` (→ skipped). No `contract_change` fires.

Renaming/deleting a function is the single most common way to break a frozen interface, and DESIGN §5.3 / money shot #3 rest entirely on this event. Only an in-place edit to a same-named symbol is ever detected.

The ghost rows are actively harmful: `GET /contract/core.py::helper` keeps returning 200 with a signature for a function that no longer exists (DESIGN §4 calls this table "the live semantic reality every agent reads before acting"), and `broker.resolve_task` will resolve and lease the ghost parcel — a lease that excludes nobody, since the real code now lives at `core.py::helper_v2` — while `co_schedulable(mode='symbol')` compares against the ghost's frozen, pre-rename byte span.

**Verified with the real `run_index` pipeline:**
```
contracts v1: [{'symbol': 'core.py::helper', 'signature': 'helper(x)', 'version': 1}]
contracts v2: [{'symbol': 'core.py::helper',    'signature': 'helper(x)',    'version': 1},
               {'symbol': 'core.py::helper_v2', 'signature': 'helper_v2(x, y)', 'version': 1}]
parcels: ['core.py::<module>', 'core.py::helper', 'core.py::helper_v2', 'u0.py::<module>', ...]
```
`integrator.py:473`: `if before is None or before[1] == row["type_hash"]: continue`.

**Fix:** prune rows for symbols absent from a re-indexed file (`run_index` knows the full symbol set per file), and emit `contract_change` with `kind='removed'` for any frozen contract that disappears. A `contract_change` for the *new* symbol should be emitted when it shadows a removed frozen one.

---

### P1-10 — `SWARMSYNC_ROOTS` is undocumented, and getting it wrong silently disables ALL leasing on the flagship Claude Code path
`README.md:108`

README §2 tells the user to run `swarmsync-serve --db /tmp/swarmsync.db --port 8787` and never mentions `SWARMSYNC_ROOTS`. But `POST /index` and `POST /integrate` reject any path outside the managed roots, which default to the **server's launch cwd** (`app.py:175-181`, `_managed_roots()` → `[os.getcwd()]`; `app.py:184-199` `_validate_managed_path` → 403). If the server starts anywhere that isn't an ancestor of the target repo — a different terminal, a systemd unit, `~`, or `/tmp` per the README's own `--db` example — indexing 403s, the parcel map stays empty, every lease `acquire` 500s on the parcels FK, and the fail-open hook (P0-2) allows every edit. **Zero leasing, silently.**

**Reproduced on a live server** (server cwd `/tmp/ss-audit-*/serverhome`, repo `/tmp/ss-audit-*/myrepo`, `SWARMSYNC_ROOTS` unset):
```
POST /index {"root":"/tmp/ss-audit-*/myrepo"} -> HTTP=403
  "path '...' resolves outside the managed roots (set SWARMSYNC_ROOTS ...)"
POST /lease {"agent_id":"a1","parcel_id":"a.py::<module>","mode":"write"} -> HTTP=500
```
`grep -rn SWARMSYNC_ROOTS README.md` → no hits. Contrast: the README spends 18 lines (130-147) documenting the analogous `SWARMSYNC_URL`/port fail-open footgun — the authors know this failure class matters.

**Fix:** document `SWARMSYNC_ROOTS` in the quickstart, and make `swarmsync-serve` take an explicit `--root` (defaulting to cwd with a startup log line naming the managed roots). Better: have the SessionStart hook **fail loudly** — a 403 on `/index` should surface as a visible session-start error, not a stderr line the model never sees. A coordination system that silently becomes a no-op is worse than one that refuses to start.

---

## 3. P2 backlog

**Reaper / event-log durability**
- `reaper.run`'s loop body (reaper.py:166-173) has no try/except; one `OperationalError: database is locked` silently kills TTL reclamation and pheromone decay for the process lifetime. Proven reachable through unmodified code (a second connection holding `BEGIN IMMEDIATE` makes `reap_once` raise after busy_timeout). *Only P2 in isolation because lazy expiry keeps work flowing — but it is the sole thing preventing P1-1's double-lease window, so fix it with P1-1.* Secondary: `app.py:250`'s `await task` catches only `CancelledError`, so the dead reaper's stored exception re-raises at shutdown, skipping both `conn.close()` calls.
- `reap_once`'s `UPDATE ... RETURNING` autocommits (`isolation_level=None`) separately from its per-row `reaped` emits (reaper.py:112-122): a crash between them leaves leases permanently reaped with no events. Same shape for `run_index`'s COMMIT vs. the `merged`/`reindexed` emits (integrator.py:498-518). Wrap each in one `db.transaction(conn)`.
- `integrate`'s atomicity is in-process only: SIGKILL/OOM — or a `BaseException` (`except Exception` misses `KeyboardInterrupt`/`SystemExit`, e.g. Ctrl-C or uvicorn shutdown during the gate's subprocess) — strands an untested merge on trunk with no `merged` event and no startup reconciliation. A rollback journal row written before `merge_branch` would make it crash-safe.

**Test suite doesn't defend Round 2's fixes** *(highest-leverage P2 group)*
- The atomic-integrate rollback (integrator.py:487-495) has **zero** coverage — the suite's own report flags 487-493 as MISSED. Mutation: removing the reset → `248 passed`, while trunk permanently keeps an ungated merge and the API still says `merge_rejected`. Reachable via a bad `base_commit` (typo, GC'd commit, rewritten fork point).
- Token auth is guarded on only 2 of 7 mutating routes. Per-route mutation, full suite each time: `/lease` → 1 failed (caught); `/release`, `/integrate`, `/parcel/update` → **248 passed** (not caught). Demonstrated: with the `/integrate` guard dropped and `SWARMSYNC_TOKEN` set, an unauthenticated merge lands on trunk (200, `merged`) — arbitrary-branch merge plus caller-specified-repo pytest execution.
- The pytest gate's sandbox env (`PYTEST_DISABLE_PLUGIN_AUTOLOAD`, `-p no:cacheprovider`, `--import-mode=importlib`) is asserted nowhere.

**Security hardening gaps**
- Managed-roots allow-list is defeated by **leaf symlinks**: `_validate_managed_path` realpaths only the root, then `rglob("*.py")` matches symlinked files and reads through them. The docstring's "catches symlink escapes" is true of the root only. Symlinked *dirs* aren't followed (pathlib `**`), so the escape is leaf-only.
- `require_token` raises `TypeError` → 500 on a non-ASCII bearer token (`hmac.compare_digest` on `str` needs ASCII; Starlette decodes headers as latin-1). Fail-closed, but a non-ASCII `SWARMSYNC_TOKEN` bricks auth in both directions.
- The gate's "sandbox" comment overclaims: none of those flags stop the merged branch's own `conftest.py`/`test_*.py` from executing — the exact attacker-controlled surface named. Also `env = {**os.environ, ...}` hands `SWARMSYNC_TOKEN` — the documented trust boundary — to that code.

**Robustness**
- `run_agent`'s outer `finally` calls only `_cleanup_worktree`; `client.release(...)` is success-path only. Any mid-flight exception leaves leases `active` with the heartbeater already stopped, so `GET /leases` misreports a live holder for up to a full TTL (unbounded if the reaper died).
- `create_app` does `db_path = str(db_path)` with no validation — passing a `sqlite3.Connection` (easy in a codebase where half the API takes `conn` first) silently creates a junk DB plus `-wal`/`-shm` in cwd instead of raising `TypeError`.
- `build_graph`'s Round 2 per-file parse guard covers only `read_text`/`ast.parse`; the signature loop immediately after does an unguarded `graph.signatures[local_symbols[def_node.name]]`, where `local_symbols` comes from the **passed-in** parcels, not the parsed file. `broker.load_scheduling_graph` deliberately passes blackboard parcels ("CURRENT parcel map"), and there is no incremental indexing, so any def added since the last `POST /index` → `KeyError: 'g'`, killing dispatch before any task is scheduled. Reproduced.

**Architecture / performance**
- The integrator's re-index is O(whole repo), not O(changed files) as DESIGN §3 claims — `run_index` re-parses every `.py` on every merge and `integrate` merely *filters* to `changed_set`; `run_impact_tests` → `_reverse_dep_files` then calls `index_repo` + `build_graph` a **second** time for the same merge. Both run inside the global `integrate_lock` alongside pytest. Per-merge cost scaling with repo size rather than change size turns the serial integrator into a throughput cliff exactly at the scale where the machinery is worth it.

**Docs**
- README:33 claims "no silent signature breaks under dependents" — DESIGN.md:244-249 (S7's own rewrite) says the exact opposite in so many words, and the code confirms DESIGN. The two docs contradict each other and the more-read one carries the overclaim.
- README:34 claims "semantic breaks caught before landing" — DESIGN.md:270 calls semantic conflict "the honest hard limit of the whole field" and concedes breaks reach trunk; and the code merges *then* tests *then* rolls back, so "before landing" is wrong on its face.
- README has no security/trust-model section: never says the server executes arbitrary branch code via the gate, that mutating routes are unauthenticated by default, that `SWARMSYNC_TOKEN` exists, or that agents are semi-trusted. HARDENING §S3 has the right reasoning; none of it reached the doc a stranger reads.
- README quickstart's first command fails verbatim on a stock box (`python` doesn't exist; only `python3`), and the undocumented `requires-python = ">=3.11"` floor sends pip into a 15-minute resolver backtrack on 3.10 rather than erroring.
- README's `SWARMSYNC_ACTIVE` semantics ("wins outright if set") match neither the adapter (requires literal `"1"`; `=0` falls through to the marker check rather than deactivating) nor the guard script (any non-empty value). Three-way disagreement.
- DESIGN §3 claims incremental per-changed-file re-indexing; it re-parses the whole repo, contradicted by store.py's own docstring that S7 cites as its source of truth.
- `schema.sql`'s header still carries verbatim the false recovery claim HARDENING §S7 reports as corrected — the fix landed in DESIGN.md only, leaving the stale copy in the file a developer opens first.
- DESIGN §3 step 6 specifies Territories as weakly-connected components; nothing computes them, `Parcel.territory` is always None, and §8's out-of-scope list doesn't mention it.

---

## 4. Considered and dismissed

*(Round 4: do not re-litigate these. Each was investigated, the mechanism was often real, but the impact claim failed verification.)*

- **"`POST /index` is not serialized against `POST /integrate`, re-indexing trunk's tree mid-merge."** Refuted: the persistence claim is false. `run_index` (store.py:172-196) indexes the **whole** repo and `upsert_parcels`/`upsert_contracts` (98-160) rewrite **every** column of every row — any torn snapshot is fully overwritten by the next index. No durable corruption.
- **"The `<module>` parcel's content_hash is blind to changes inside function bodies, making §5.5's read-dep re-check a no-op at the default granularity."** The mechanism is real and reproduced byte-for-byte (indexer.py:227 hashes only `module_glue`; a body edit leaves `01ba4719c80b6fe9` while `mod_a.py::helper` moves). But the "no-op" impact claim did not survive — the re-check is not the only thing standing between a stale read-dep and a bad merge.
- **"`_check_read_deps` resolves frozen-contract read-deps against `parcels.content_hash` → 100% false `needs_rebase`."** Mechanism reproduced (all 6 contracts share an id with a parcels row via graph.py:331, so the parcels-first lookup at integrator.py:120-126 always wins and the `contracts.type_hash` fallback is dead code). The "100% false positive" claim did not hold on verification.
- **"DESIGN §6 promises FIFO exclusive-lease escalation for livelock; no such mechanism exists."** Literally correct that no FIFO code exists (broker.py:346-359 does bounded retries, then returns the last `lease_denied`), but the load-bearing claim — that contenders don't serialize and work doesn't eventually land — is wrong, and the failure scenario doesn't reproduce. A doc-precision nit at most.

---

## 5. Round 1 vs Round 3 delta — did the hardening hold?

**The fixes are real, not cosmetic. Every Round 2 fix I inspected does what it says.** That's worth stating plainly, because the rest of this section is critical. Round 1's P0 and its P1 list are genuinely closed:

| Round 2 fix | Verdict |
|---|---|
| `/integrate` serialized via `asyncio.Lock` | Real. **But it created P1-4:** it converted a race into a global chokepoint with no timeout on the thing it serializes. |
| Atomic reset-hard-on-failure | Real for **git**. Doesn't touch the blackboard (P1-5). **Zero test coverage** — deletable with 248/248 green. |
| Atomic reaper `UPDATE ... RETURNING` | Real. The select-and-flip is genuinely atomic against a concurrent heartbeat. Doesn't help if the reaper task is dead (P2), and doesn't cover the flip-vs-emit gap. |
| Events seq via `RETURNING` | Real. |
| Classifier per-file parse guard | Real, but stops one line short of the signature loop → `KeyError` (P2). |
| Git arg-injection guards | Real for option injection. **Missed traversal into `shutil.rmtree`** (P1-2). |
| Token auth + realpath roots + localhost bind + sandboxed pytest | Real, but: 5 of 7 routes unguarded by tests; roots defeated by leaf symlinks; "sandbox" doesn't sandbox; and **the whole thing is undocumented and default-off** (P1-10, P2 docs), so a stranger never turns it on. |
| Hook lease keepalive | Real. |
| Worktree cleanup | **Regressed.** Deletes the branch on rejection, destroying the work and defeating §5.5's rebase story (P1-6). |
| Concurrency regression tests | Real for what they cover — but mutation testing shows they don't cover the fixes they were written alongside. |

**The pattern.** Round 2 fixed the reported bugs, and stopped at each bug's boundary. It did not fix the class:

- **The lease fabric was hardened at `acquire` and the reaper, but not at `heartbeat`** (P1-1) — the one write path Round 1 didn't happen to name. The predicates now disagree, and mutual exclusion holds only because the reaper usually ticks first, which the file's own design note says it must not depend on.
- **The git guard was hardened against the injection Round 1 found, and not against the sink** (P1-2). The dangerous thing about `add_worktree` was never the git argv; it was `shutil.rmtree`.
- **Auth was added and then not tested** (P2). Four routes' guards are deletable in silence, including the one that runs pytest from a caller-specified path.

**And Round 2 didn't look at the surfaces Round 1 didn't look at.** The two P0s here are both in territory Round 1 never touched: the runner's heartbeat lifetime (P0-1) and the hook adapter's fail-open on unindexed files (P0-2). Both are on the **happy path**, not in an edge case — P0-1 fires on any repo whose test gate takes >30s, which the impact-selection default makes ordinary; P0-2 fires on every `.ts`, `.yaml`, `package.json`, and every newly-created file, on the surface the README leads with and where there is no worktree isolation to fall back on.

**The uncomfortable conclusion.** Three of the system's four load-bearing guarantees currently hold by luck rather than by mechanism:

- *Mutual exclusion on a parcel* — broken on the happy path (P0-1), and separately resurrectable (P1-1). Holds because reaper timing usually cooperates.
- *One agent per file* — enforced only where the agent chooses an Edit-family tool (P1-3), and not at all for unindexed files (P0-2).
- *Zero merge conflicts at symbol granularity* — holds because `sample_repo`'s functions are blank-line-separated (P1-7). Real code isn't.
- *Frozen contracts prevent signature breaks* — the entire subsystem is dead code in the default configuration (P1-8), and blind to renames in any configuration (P1-9).

Round 2 was competent, targeted work on a correctly-diagnosed list. The gap is that Round 1's list was treated as the specification. **For Round 4, the instruction that matters is: don't fix these fifteen findings — fix the classes, and add the mutation test that proves each fix is load-bearing.** A suite that stays green when you delete the fix is not a suite; it's a formality. That is the single highest-leverage change available here, and it would have caught roughly half of this report.
---

## 6. Completeness critic (what this audit itself missed)


The audit was **diff-shaped, not codebase-shaped**. Every confirmed finding lands on a file Round 2 touched (`adapter.py`, `integrator.py`, `reaper.py`, `git_ops.py`, `broker.py`, `graph.py`, `app.py`, `leases.py`, README/DESIGN). It inherited Round 1's blind spot exactly: it re-audited the hardened surfaces and never opened the modules Round 2 didn't edit.

**Files no dimension named:** `blackboard/db.py`, `blackboard/schema.sql`, `classifier/store.py`, `classifier/indexer.py`, `server/events.py`, `agent/client.py`, `agent/mutators.py`, `server/serve.py`, `scripts/swarmsync-hook-guard`, `demo/`.

**Failure-mode categories nobody modeled:** upgrade/migration (none exists), resource exhaustion (unbounded `events`), operational (blocking I/O on the event loop), multi-user (unauthenticated read routes).

**Claim verification:** I spot-checked the `heartbeat()` no-TTL-predicate P1 — real, `UPDATE ... WHERE id AND agent_id AND status='active'` with no `ttl_expires_at > now`. The two P0s (hook fail-open, reaped-mid-integrate) are code-evident. The `decay`/`store` layer had no such scrutiny because nobody opened it.

## Concrete findings from the least-examined area

### P1 — `_validate_managed_path`'s symlink guard only covers the root; the walk escapes it (verified by execution)
`/home/keith/projects/swarm-sync/swarmsync/server/app.py:184` docstring claims:
> "Rejects both plainly-outside paths and symlink escapes (the symlink target is what `realpath` resolves to, so a link that points outside is caught here)."

False. It realpaths **only the root argument**. `indexer.index_repo` then does `root_path.rglob("*.py")` (`indexer.py:258`) and never re-validates each hit. A **file** symlink inside a managed root is followed and indexed. Executed against a temp tree with `root/link.py -> /outside/secret/private.py`:

```
PARCEL: link.py::exfiltrate_me | path= link.py
SIGNATURES: {'link.py::exfiltrate_me': ('exfiltrate_me(password, token)', '750bd899...')}
```

Out-of-allow-list symbol names, signatures and hashes land in `parcels`/`contracts` — and `GET /parcels`, `GET /contract/{symbol}`, `GET /events` carry **no `require_token`**, so they are readable by anyone who can reach the port. This is a Round 2 control (S3 realpath allow-list) with a hole that its own docstring denies. (Directory symlinks are safe — pathlib's `**` skips them — which is likely why this looked fine.)

### P2 — Zero schema versioning or migration path
`init_db` is `CREATE TABLE IF NOT EXISTS` only; no `PRAGMA user_version`, no `ALTER TABLE`, no migration code anywhere in the repo (grep for `user_version|migrat|ALTER TABLE` returns nothing). The blackboard is persistent state at a stable default path (`blackboard.db`). Any future column addition silently no-ops against an existing DB, and the first query referencing it fails at runtime with `OperationalError: no such column` — mid-lease, on a live route. There is no version check to fail loudly at boot.

### P2 — `decay_pheromone` is a non-transactional read-modify-write; concurrent drops are lost (verified)
`/home/keith/projects/swarm-sync/swarmsync/server/events.py:141` — `SELECT` all rows, compute in Python, then `executemany` UPDATE, with **no `db.transaction`**. The reaper runs this every 1s on its own connection while request threads run `drop_pheromone` on theirs. A drop landing between the SELECT and the UPDATE is clobbered by a value computed from the stale read. Reproduced:

```
after concurrent drop:            1.0
after decay write (lost update):  0.4999621116403392
```

A fresh full-strength "planned" signal is stomped to a half-life-old value. Advisory data, so P2 — but the module docstring's claim that repeated passes "compound correctly off real elapsed time" is only true single-threaded.

### P2 — Reaper does blocking SQLite on the asyncio event loop thread (amplifies the confirmed reaper P2)
`app.py:233` comments note "The reaper is a long-lived background task on the event loop thread" and then `reaper_mod.run` calls `reap_once`/`decay_once` synchronously — no `run_in_executor`. With `PRAGMA busy_timeout = 5000` (`db.py:64`), a contended write **blocks the entire ASGI server for up to 5 seconds**, including `/integrate`'s lock handoff and every other request. `decay_once` is also an O(all pheromone rows) full-table read-modify-write on the loop thread every 1s.

This composes with the confirmed "one transient exception kills the reaper forever" finding: on busy_timeout expiry the loop raises `OperationalError` and dies permanently. The audit rated that P2 as theoretical; blocking-on-the-loop makes it the *expected* outcome under load, not a fluke. The pair deserves a joint re-rating.

### P2 — `events` grows unbounded
Append-only, no pruning, no rotation, no retention. Every heartbeat (per agent, per interval) writes a row forever. The docs call `events` "the source of truth for recovery" and the recovery path is `tail(since_seq, limit=1000)` — replay from seq 0 is never bounded or checkpointed. Long-running deployment = monotonically growing DB and a replay that gets slower forever.