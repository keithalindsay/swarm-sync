# Round 4 Audit Report — swarm-sync (Pheromesh)

**Measured at HEAD `576c73b`** (the brief said `b383008`; the repo advanced three commits mid-round — `15730cf`, `027df7c`, `576c73b`. Those commits close several findings below, and that is reflected in the status of each. All re-measurement in this report is against `576c73b`.)

**Gates re-verified now, independently:** ruff clean · mypy clean (25 source files) · **292 tests green** in 42.95s · demo **5/5** standalone · `git status` clean.

---

## 1. Verdict

**Not yet at the "proud to ship" bar. Two things block it, and only two.**

| Bar | Status |
|---|---|
| ruff + mypy clean | ✅ |
| suite green, no flakes | ✅ 292 green, stable across ~15 full runs this round |
| suite has **meaningful** coverage | ⚠️ materially better than R3 (auth + gate-kill now mutation-pinned) but **two live guards are still deletable green** |
| concurrency invariants under real stress | ✅ demo 5/5; heartbeat's SQL-clock fix survived every attack |
| demo standalone | ✅ |
| sound error handling | ❌ **blocked** — see P0/P1 below |
| **no confirmed P0/P1** | ❌ **blocked** — 1 P0 + 3 P1 carried, none adversarially verified |
| DESIGN matches code | ❌ **blocked** — DESIGN documents worktree isolation as unconditional and never mentions the hook path, where it is false |
| README usable by a stranger | ❌ **blocked** — the quickstart's first token (`python`) does not exist on a stock box, and the repair (`python3` → 3.10) is refused by an undocumented `requires-python >=3.11` |

**The plain statement:** the *code* is in better shape than any prior round — the newest fixes survived hostile review on their own merits, and for the first time a round's fixes were pinned by tests that fail when reverted. What blocks shipping is (a) a carried P0 (crash-mid-integrate leaves an un-gated merge on trunk) that **no round has ever actually reproduced**, and (b) the docs, which are now materially behind the code on the single most consequential caveat in the system. Neither is a mystery. Both are a day's work.

**The honest caveat on the carried set:** the five `r4-carried` findings survived a verification pass, but four of them were raised by dimensions that ran *before* the session limit and were never driven end-to-end. They are credible, not proven. R5 should treat "reproduce these four" as cheap, high-value work — R4's own experience is that ~50% of unverified findings die on contact (six of eleven, below).

---

## 2. Confirmed P0 / P1

Ordered by severity, then blast radius.

### P0-1 — Crash mid-integrate leaves an un-gated (red) merge permanently on trunk
`swarmsync/coordinator/integrator.py` — `integrate()`

**Scenario.** `integrate` merges to trunk, *then* runs the gate. Atomicity is in-process only. SIGKILL/OOM between `merge_branch` and the gate verdict — or any `BaseException` (`except Exception` misses `KeyboardInterrupt`/`SystemExit`, i.e. **Ctrl-C or a uvicorn shutdown during the gate**) — leaves the merge commit on trunk with no `merged` event and no rollback. There is no startup reconciliation, so a restart resumes onto a trunk that never passed the gate. The "trunk is always test-green" guarantee — the product's headline claim — is silently false from that moment on, forever.

**Why P0 and not P2** (R3 rated a version of this P2): an operator Ctrl-C during a 600s gate is *ordinary*, not exotic. The damage is silent, persistent, and corrupts the exact invariant the system exists to provide. That is the P0 definition verbatim ("broken core guarantee").

**Concrete fix.** Two parts, both required:
1. **Write intent before mutating git.** Emit an `integrate_started{branch, base_commit, trunk_sha_before}` event *before* `merge_branch`, and the terminal `merged`/`merge_rejected` after. That makes the in-flight window a durable fact, not an in-memory one.
2. **Reconcile at startup.** On server start, find any `integrate_started` with no terminal event; `git reset --hard` trunk to its recorded `trunk_sha_before` and emit `integrate_orphaned`. Also broaden the handler to `except BaseException:` → rollback → `raise`, so Ctrl-C rolls back rather than abandoning.

**Test that must exist:** kill -9 a real server between merge and verdict; assert trunk still carries the merge (the bug), then assert that after restart trunk is back at `trunk_sha_before` and an `integrate_orphaned` event exists.

---

### P1-1 — Multi-root is unsound: parcel ids have no repo qualifier
`swarmsync/classifier/store.py` (id construction) · `swarmsync/server/app.py` (`_managed_roots`)

**Scenario.** Parcel ids are `<relpath>::<symbol>` **relative to the indexed root**, with no repo qualifier. `SWARMSYNC_ROOTS` accepts multiple roots and R3 *shipped and documented* `swarmsync-serve --root` (repeatable). Index `/repoA` and `/repoB` where both contain `utils.py`: `utils.py::helper` is the *same id*. The upsert overwrites one repo's rows with the other's; a lease on `utils.py::helper` locks **both** repos' files; `integrate` re-indexes one root and clobbers the other's parcels.

**Reachability is the point:** this is not a hypothetical config. R3 added the flag *for this*, and README tells operators to set multiple roots.

**Concrete fix.** Qualify the id with the root: `<root_key>|<relpath>::<symbol>`, where `root_key` is a stable digest of the realpath'd root. Add a `root` column to `parcels` and make it part of the primary key. Alternatively — cheaper, honest, and arguably better for a v1 — **make multi-root a hard error**: reject >1 root at startup, remove the repeatable `--root`, and say in README that one server coordinates one repo. Do not ship a documented flag whose use corrupts the blackboard.

**Test:** two real repos each containing `utils.py::helper`; index both; assert the two parcels are distinct rows and that a write lease on A's does not block B's.

---

### P1-2 — Merge-conflict rejection skips the re-index; blackboard keeps a hash for code in no git ref
`swarmsync/coordinator/integrator.py` — the conflict branch of `integrate()`

**Scenario.** R3's P1-5 fix re-indexes inside `_reject_and_reset`. The **merge-conflict** rejection path emits `merge_rejected` directly and never reaches `_reject_and_reset`, so it never re-indexes. `runner.py` posts `/parcel/update` *before* `integrate`, so the agent's self-reported `content_hash` for a version of the file that **exists in no git ref** stays in the blackboard indefinitely. Every subsequent read-dep reasoning (and any future `expected_read_deps` re-check) is then based on a hash of code that never existed on trunk.

**This is the round's signature pattern:** R3 fixed the bug, not the class. It fixed the path it was looking at and left the sibling path that reaches the same sink.

**Concrete fix.** Route the conflict rejection through `_reject_and_reset` (or, better, hoist the re-index into a `finally` on the rejection paths so *no* rejection route can skip it). Then assert the class, not the instance: a test parametrized over **every** rejection reason (`conflict`, `gate_red`, `needs_rebase`) asserting the blackboard's `content_hash` matches `git show trunk:<file>` after rejection.

---

### P1-3 — Hook lease keys on the unresolved leaf name: a symlink alias yields two write leases on one inode
`swarmsync/hooks/adapter.py` — `_parcel_id`

**Scenario.** `_parcel_id` builds `<relpath>::<module>` from the payload's `file_path` relative to cwd **without realpath-resolving it**. Repo contains `link.py -> real.py`. Agent A leases `real.py::<module>`, agent B leases `link.py::<module>` — two different ids, two independent write leases, **one physical inode**, in the ONE working tree hook subagents share. Last writer wins silently. This is the precise collision the product exists to prevent, on the surface where — per `leases.py:76-78`'s own docstring — "the lease is the only collision protection".

**Severity honesty:** P1 not P0 because an in-repo symlink to a source file is uncommon in Python repos. But the blast radius is total (silent same-file clobber, zero signal), and the fix is three lines.

**Concrete fix.** `os.path.realpath()` the file_path before computing the relpath in `_parcel_id`. Note this composes with the deliberately-deferred leaf-symlink escape in `_validate_managed_path` — fix them together, and put the realpath in *one* shared helper so the next path-derived id can't reintroduce it.

**Test:** end-to-end through `adapter.main` — create `link.py -> real.py`, lease via `real.py`, then precheck via `link.py`, assert DENY.

---

## 3. Mutation Results — the round's headline

This is the check R4 attempted twice and never finished, and the one **R3 said would have caught half its own report**. It ran to completion this round. Every mutation below was applied one at a time to a `/tmp` copy against a 278-test baseline.

### What the mutation run found, and what happened to it

**CLOSED THIS ROUND (`576c73b`, `027df7c`) — verified fail-on-revert:**

| Mechanism | Mutation result | Now |
|---|---|---|
| `require_token` on `/heartbeat`, `/release`, `/parcel/update`, `/integrate` | **all 4 SURVIVED** (278 green each). R3 reported three of these undefended; still undefended **two rounds later**. | ✅ Fixed *at the class level*: one parametrized test over every mutating route, **plus** a test pinning the route list against the app's real POST routes — so a new unguarded route cannot escape by not being listed. All 7 now fail the suite when deleted. |
| `_kill_process_group(proc)` → `pass` | **SURVIVED.** The test literally named `kills_a_hanging_gate` asserted only the verdict and elapsed bound — never the kill. **The bounded drain added in `8f1a449` masked it**: with no kill, `communicate(timeout=5.0)` just expires, every assertion still holds, and a pytest tree running the branch's arbitrary test code survives forever reparented to init. Measured: 1 leaked orphan (pid 331847, ppid 1059). | ✅ Now asserts the process group is actually gone. |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD`, `-p no:cacheprovider`, `--import-mode=importlib` | **all SURVIVED.** Nothing asserted the gate's command line or environment *at all*. | ✅ Pinned. |
| `start_new_session=True` | "Caught" — but by the test runner **SIGKILLing itself**, with zero output. Not an assertion; a crash that reads as flaky infra. | ✅ Pinned — and it exposed **a real latent defect**: `_kill_process_group` blindly SIGKILLed whatever group `getpgid(child)` reported with no check that it isn't *our own*. A hanging test in any deployment where that flag regressed would kill the coordinator. Now refuses to signal its own group. |
| `local_symbols[def_node.name]` KeyError (graph.py:201) | Found via `newfixes`, see §6. | ✅ Fixed at the class level in `027df7c`. |

**STILL OPEN — verified still deletable-green at `576c73b` just now:**

#### M-1 · `build_graph`'s SyntaxError guard — deletable green, and reachable in the swarm's *normal* state
`swarmsync/classifier/graph.py:191` — `except (OSError, SyntaxError, UnicodeDecodeError): continue`

Removing `SyntaxError` → **278 green, SURVIVED**. I first checked whether the guard is dead code. **It is not.** `index_repo` skips unparseable files, so via *that* path no parcel exists to re-parse. But `broker.load_scheduling_graph` (broker.py:208) and `integrator._reverse_dep_files` (integrator.py:179) build parcels **from the DATABASE** — rows indexed when the file was valid — then re-read that file **from disk**. In a swarm, agents are editing that tree: a file that parsed at index time is *routinely* broken on disk right now.

Proven empirically (guard removed, /tmp scratch repo): index a valid `mod_a.py` → 2 parcels; rewrite it on disk as `def helper(x:`; `build_graph(parcels, root)` → **`SyntaxError: '(' was never closed`**, uncaught. Broker schedules **no task for any file**; `/integrate` 500s. Structurally identical to the `kind='file'` P0 fixed in `8f1a449` and the KeyError P0 fixed in `027df7c`: **one transiently-broken file bricks the whole coordinator.** This is the same class, a third time.

**Test needed:** index a repo → corrupt one indexed `.py` **on disk** → assert `build_graph(parcels_from_index, root)` does not raise and still returns the graph for the *other* files. Same for `load_scheduling_graph`: it must still schedule tasks for unaffected parcels.

#### M-2 · The managed-root allow-list's `root + os.sep` boundary — deletable green
`swarmsync/server/app.py:191` — `if real == root or real.startswith(root + os.sep)`

Weakening to `real.startswith(root)` → **278 green, SURVIVED**. The `+ os.sep` is the *only* thing separating "inside the managed root" from "shares a name prefix with it". Confirmed just now: `tests/test_security.py:100`'s sibling case uses a dir named `outside` — a plainly-outside path, which the coarse check catches. Nothing tests a **name-prefix sibling**.

Escape semantics confirmed directly: root `/home/keith/projects/swarm-sync`, attack `/home/keith/projects/swarm-sync-evil/secrets` — real code `False` (rejected), mutant `True` (**escape**). `POST /index` then walks it and `POST /integrate` runs `git merge` **plus the pytest gate** inside it: the exact "point git at any path on the host" escape the allow-list exists to block.

*Scoping against the real trust model:* the guard is present in source, so this is **not a live hole** — it is a hole in the net meant to keep it present. But `SWARMSYNC_ROOTS` is the one setting README (:123-128) calls a *silent total-enforcement-loss footgun*, and its boundary is the thing nothing pins. Contrast: `path-validator-neutered` → CAUGHT; `path-validator-no-realpath` → CAUGHT. The coarse checks are pinned; only the boundary is not.

**Test needed:** with `SWARMSYNC_ROOTS` set, `POST /index` **and** `POST /integrate` on a sibling whose name has the managed root as a string prefix (`<root>-evil`) → 403.

### The finding behind the findings

Of nine mechanisms probed, **eight were deletable with a fully green suite**, and three of those (`/release`, `/integrate`, `/parcel/update` auth) had been *reported by R3 two rounds earlier and were still undefended*. The suite was, until this round, largely testing **the coarse presence of clauses**, not their semantics — deleting a guard outright is caught; weakening its boundary is not. `576c73b` fixed that for the mechanisms it touched, and did so correctly (route-list pinning, not four one-offs). M-1 and M-2 are the remainder.

---

## 4. P2 Backlog (deduped, grouped)

**Test-quality / boundary-insensitivity**
- Every TTL boundary predicate survives a `>`↔`>=` flip: `acquire` (`ttl_expires_at > :now`), `heartbeat` (`> _NOW_SQL`), reaper (`<= ?` → `<`). Deleting the clause outright *is* caught in all three — so the suite tests the clause's presence, not its semantics.
- `release()`'s two ownership checks (guard SELECT + UPDATE WHERE) are **mutually masking**: either alone is deletable green; only deleting both is caught. Redundancy no test can distinguish from necessity. Not an undefended mechanism — verified by running the both-deleted case.
- `require_token` accepts **any auth scheme** once the `scheme != "Bearer"` check is removed — no test presents a correct token under a wrong scheme.

**Robustness**
- `POST /intent` 500s and leaves a **half-written blackboard** when a target parcel isn't indexed: event written, then the pheromone FK to `parcels(id)` raises IntegrityError mid-route, with no enclosing transaction and no structured error. (`app.py` `post_intent`.) *Carried, unverified.*
- The gate's escaped-descendant timeout path returns `stdout=stderr=''`, so the operator gets boilerplate and no test output — a deliberate trade (verdict already decided), but undocumented.

**DESIGN.md is stale on the shipped surface** (one class of defect, five instances)
- §5.5's optimistic read-dep re-check is documented as **live integrate-time behavior**; it is opt-in (`expected_read_deps=None` by default), **unreachable over HTTP** (`IntegrateBody` has no such field; no production caller anywhere — only `tests/test_integrator.py:282,305,337,358`), and its documented **"forced rebase" does not exist** (`grep` finds no `git rebase` in `swarmsync/`; the code emits a `needs_rebase` event and bounces). §5.3:246-247 tells dependents to rely on it. `agent/client.py:230` documents a `"needs_rebase"` status it cannot return.
- §3:86-88 claims **incremental per-changed-file re-indexing**; `integrator` calls `run_index(conn, repo)` with no filter, and `store.py`'s own docstring says "**Full rebuild every call** … always re-parses every `.py` file under `root`". Code and its docstring agree with each other and disagree with the spec.
- DESIGN documents **none** of the shipped operational surface: `ensure_parcel` (a `POST /lease` that *creates* parcels the classifier never emitted — contradicting §3's "parcels are the classifier's product" and §4.1's "derived facts about current source on disk"), `SWARMSYNC_TOKEN`, `SWARMSYNC_ROOTS`, `SWARMSYNC_GATE_TIMEOUT`, `swarmsync-serve`.
- `run_impact_tests`' docstring documents its return contract with **no mention of the gate timeout**, so `ok=False` conflates "tests failed" with "SIGKILLed at 600s". The module docstring's step 3 presents a binary that no longer covers the timeout third case. DESIGN §5.4 likewise; `SWARMSYNC_GATE_TIMEOUT` is in README only.
- `schema.sql:3-4` still claims "parcels/leases/pheromone are projections **replayable** from [events]" — the **exact overclaim DESIGN:169-173 was corrected to remove** ("**`parcels` and `contracts` are NOT event-replayable**"). schema.sql is the file an operator reads when they open the DB with `sqlite3` — which DESIGN §4 explicitly advertises as the inspection path.

**Stale docstrings adjacent to live code**
- `leases.py:8-11` says `acquire` is a **single SQL statement** ("atomic … since a single statement is its own implicit transaction"). Under `ensure_parcel=True` — the mode the hook adapter uses on **every** precheck — it is **two statements in two implicit transactions** (`isolation_level=None`, autocommit). The docstring never mentions `ensure_parcel` or that `acquire` can write to `parcels` at all. Its whole atomicity argument describes a path the hook path never takes.
- `git_ops.py:10-16`'s SECURITY header names only `_reject_option_like`; the actual guard on the `shutil.rmtree` sink is the unmentioned, stricter `_reject_unsafe_name` allow-list. Behavior correct, header stale.
- ~~`_ensure_parcel`'s "indistinguishable from the indexer's row" docstring is false on `symbol`~~ — **closed in `027df7c`** (row now matches `parse_file` field-by-field; test asserts against real indexer output rather than what the author happened to write).

**README**
- The quickstart **cannot be executed as written**: `which python` → None on a stock box (only `/usr/bin/python3`), so README:39 and README:43 fail at the first token; the obvious repair (`python3`) builds **3.10.12** and `pip install -e ".[dev]"` is refused by `pyproject.toml:12`'s `requires-python = ">=3.11"`. **No Python version appears anywhere in README or DESIGN.** `/usr/bin/python3.11` exists and is what the project's own venv uses (3.11.0rc1) — the fix is `python3.11 -m venv`, the one thing the docs never say. *(Grouped as one item; it is one broken block.)*
- README's `SWARMSYNC_ROOTS` section — titled "get this wrong and you get *silent* zero enforcement" — is env-only and **never mentions `--root`**, the flag R3 shipped to solve that exact footgun.
- `HANDOFF.md`, the file every new session is told to read **first**, is two rounds stale on every load-bearing fact: "HEAD = the Round 2 hardening commit" (actually `576c73b`, six commits on), "248 tests green" (292), "NEXT STEP: Round 3 re-audit (not yet done)" (R3 audit+fixes, R4 audit+fixes all shipped since). It points at an unresolved placeholder path under `~/.claude/projects/-home-keith-Documents/<session>/` — **the very directory its own safety section says agents must never touch** — while the real workflow lives in-repo at `scripts/audit-r4-workflow.js`. It is also untracked in git despite `4cc67e5` claiming to add it, so it has no history to date it.

---

## 5. Considered and Dismissed

Recorded so R5 does not re-spend budget. **Six of eleven unverified findings died on contact** — the verification pass is earning its keep.

| Finding | Why dismissed |
|---|---|
| **Duplicate parcel ids collapse `@property`/`@x.setter`** (P1) | Mechanism **confirmed and reproduced** — `parse_file` keys `<rel>::<name>` with no dedup; a redefined symbol loses a code region. Downgraded, not eliminated: real but narrow. Keep as a P2 for R5 if it resurfaces; do not re-report as P1. |
| **`POST /parcel/update` has no lease-ownership check** (P1) | Mechanical claim **TRUE and still true** (`app.py:407-427`; `body.agent_id` used only for the pheromone drop, never in the UPDATE predicate). Refuted **as a P1 under the real trust model**: localhost dev tool, semi-trusted agents, `/integrate` runs repo code by design. An agent that wants to poison a hash has a dozen easier routes. Legitimate P2 hardening; not a security hole. |
| **index_repo's 5000-file/30s cap turns every merge into a bogus rejection** (P0) | **Refuted.** The cap exists (`indexer.py:58-59`) and `integrate` does call `run_index` on every merge — but the claimed bogus-rejection chain does not occur when driven. |
| **One transient exception permanently kills the reaper loop** (P1) | Mechanism **confirmed** (`reaper.py:166-173` has no try/except in the loop body); **impact claim refuted**. R3's P2 rating was correct; the P1 escalation rested on a factually wrong premise. |
| **The reaper's blocking SQLite stalls the entire ASGI server 5s** (P1) | **Refuted as P1.** Every individual code fact is accurate; the chain's trigger is unreachable in this system, and the gap was measured. |
| **Hook `_keepalive` discards heartbeat's False → lapsed agent still ALLOWed** (P1) | **Refuted — premise factually wrong about current code.** `GET /leases` (`app.py:311-318`) selects `WHERE status='active' AND ttl_expires_at > ?`, so a lapsed lease is **never returned to the hook at all**, independent of `_keepalive`. |
| **Fix #3 — heartbeat on SQLite's write-time clock** | **Attacked on six axes, could not break it.** Epoch agreement within −0.25..−0.6 ms (pure ms truncation of SQLite's integer `iJD`; the julianday float round-trip adds ~8 µs). TZ/DST-invariant (`'now'` is UTC; identical deltas under UTC/New_York/Kolkata/Chatham, max 0.4 ms). The three textual occurrences evaluate to **one** value per statement (per-VDBE `iCurrentTime` cache) and are **not** cached across statements — exactly the required semantics. The load-bearing claim holds: `OP_Transaction` takes the write lock **before** the julianday `OP_Function` runs, so the clock is read *after* the busy_timeout wait. Mixed clocks with `acquire` (still Python `time.time()`) **fail safe** — a stale Python `now` only makes a lease expire *earlier*. The f-string interpolates a module constant, never caller input. |
| **Fix #2 — bounded gate drain** | **Attacked, could not break it.** `_DRAIN_TIMEOUT_SECONDS` is unreachable on the happy path (it lives only inside `except TimeoutExpired`). No zombie persists despite the abandoned `communicate()`: `Popen.__del__` → `_internal_poll(_deadstate=maxsize)` reaps the already-SIGKILLed child before it reaches `subprocess._active`; only a **cosmetically wrong** ResourceWarning is emitted. fd count unchanged across the path. `getpgid` racing the child's exit is covered. Timeout path leaves trunk clean end-to-end. Only real cost: empty stdout on the escaped-descendant path (P2, above). |

---

## 6. Delta — did R3's fixes hold? Did R4's?

**The definitive answer, now that mutation and newfixes have both run.**

### R3's fixes: no, and the pattern held exactly as predicted

R4's audit found three real defects in R3's fixes, twice self-inflicted at P0/P1. This round adds a fourth: **P1-2** — R3's P1-5 rollback re-index fix covers `_reject_and_reset` and **misses the merge-conflict path**, which reaches the same sink by a different route. And the mutation run's most damning result: **three auth guards R3 itself reported as undefended were still undefended two rounds later.** R3 wrote the finding, R3's fix round did not close it, R4 re-derived it from scratch.

### R4's own fixes (`8f1a449`): one of three was wrong, and it was wrong in the *characteristic* way

- **Fix #3 (SQL clock):** sound. Attacked hard, survived everything.
- **Fix #2 (bounded drain):** sound *on its own terms* — **but it silently disabled a sibling test.** With the kill removed, `communicate(timeout=5.0)` just expires, the verdict is still False, every assertion still passes, and the runaway tree lives forever. A defensive fix **masked the mechanism next to it.** Nobody would have noticed without mutation.
- **Fix #1 (`kind='file'`→`'module'`):** **wrong, at P0.** It closed the P0 **only for non-`.py` files** — and the author's own new test used `package.json`, *the one case that passes*, manufacturing false confidence while the common case (`new .py` with a top-level def) still bricked all broker dispatch with `KeyError: 'new_feature'`. Its docstring claimed the row was "indistinguishable from what a later `POST /index` produces"; it differed on `symbol` and `content_hash`, and the test asserted `symbol == MODULE_SYMBOL` — **pinning the wrong value** against both the indexer and `schema.sql`.

### "Fix the class, not the bug" — the answer changed *inside this round*

**For every round through R4's first fix pass: no.** Each round stopped at the last bug's boundary. The evidence is a single defect class appearing **four times in four rounds**, each time at P0/P1, each time as "one transiently-inconsistent file bricks the whole coordinator":
1. R4 audit → `kind='file'` → ValidationError → all dispatch dead.
2. R4b → the *same* P0 unfixed for `.py` → `KeyError` at `graph.py:201` → all dispatch dead, "just raised one frame lower with a different exception type."
3. **M-1, still open** → `SyntaxError` from a mid-edit file → all dispatch dead.

Fixes 1 and 2 each patched their instance. The class — *`build_graph` re-parses live disk against a DB parcel map that is only as fresh as the last `POST /index`, and every disagreement between them is fatal to the whole coordinator* — went unnamed for two rounds.

**Then, at the end of this round, it changed.** `027df7c` is the first commit in six that fixes a class: it names the root cause ("`local_symbols` comes from the passed-in DB parcels but `def_node` comes from the file on disk. Those legitimately disagree… there is no incremental indexing"), makes any symbol-without-a-parcel simply have no signature entry, **closes R3's separate P2 at `AUDIT_R3.md:321` as the same defect**, and rewrites the tests to cover the shapes that actually break — each verified to fail on revert. `576c73b` does the same for auth: not four one-off tests, but one parametrized test **plus a test that pins the route list against the app's real POST routes**, so a *new* unguarded route cannot escape by not being listed. That is the correct shape. It also caught a real latent defect by accident (`_kill_process_group` would SIGKILL its own group if `start_new_session` ever regressed — a hanging test killing the coordinator).

**So: are the current fixes sound, and is the suite defending them?** The three newest fixes (`15730cf`, `027df7c`, `576c73b`) are sound as far as this round could attack them, and — for the first time in the project's history — **each is pinned by a test verified to fail when the fix is reverted.** The suite is defending them. But note the meta-lesson: *this round's own fix (#2) broke this round's own test, and only mutation found it.* The instruction is now being followed. It has been followed for exactly two commits. Assume `576c73b` and `027df7c` are R5's bugs and attack them accordingly — that heuristic has been right five rounds running.

---

## 7. Round 5 — the single highest-leverage thing

**Reproduce and fix P0-1 (crash mid-integrate → un-gated merge on trunk), with a durable `integrate_started` event and startup reconciliation.**

Why this and nothing else:

1. **It is the only thing that can silently falsify the product's headline claim.** Every other open finding degrades the system loudly — the broker stops dispatching, a route 500s, a doc misleads. P0-1 leaves the system *looking* healthy while "trunk is always test-green" is permanently false. That is the one failure mode a coordination fabric cannot have.
2. **It has never been reproduced.** R3 rated it P2, R4 escalated to P0 without driving it, and this round could only carry it. Given that **six of eleven** unverified findings died on contact this round, the *first* action is `kill -9` a real server mid-gate and look at trunk. That either kills a P0 or proves the ship-blocker in twenty minutes. Nothing else in the backlog has that expected value.
3. **It forces the durability work the whole system is missing.** `integrate` is the only place swarm-sync mutates state it cannot re-derive (git trunk). Everything else self-heals via re-index. An `integrate_started`/terminal-event pair plus startup reconciliation is the one piece of crash-consistency the design assumes and does not implement — and it is what makes the `events` log actually load-bearing rather than an audit trail, which is what `schema.sql`'s header has been *claiming* for the parcels table it can't recover.

**Then, in the same round and only after:** M-1 (`SyntaxError` guard) — because it is the fourth appearance of a class the last two commits finally started fixing, and leaving it open means the class-level fix stopped one instance short *again*. Then the docs (P0-1's fix will change DESIGN §5 anyway; batch the §5.5/§5.1/schema.sql/README-quickstart corrections into that edit).

**Do not** spend R5 on the P2 mutation backlog (TTL boundaries, auth scheme, masking release checks). Those are real, and they are cheap, and they will still be there. The bar is blocked by a P0 nobody has looked at.

---

# Completeness critic

Repo clean, no scratch left behind.

# R4 Completeness Critique

## Verdict: coverage is NOT adequate. The dominant problem is not the audit's reach — it's that the audit's own output was silently truncated between runs.

## 1. The crack between the two runs swallowed most of the audit

This is the finding that matters. `AUDIT_R4_PARTIAL.md` §"Still open — Round 5 must re-verify" lists **13 P0/P1 candidates + 9 P2s**. The confirmed list carries **5** of them (`r4-carried`). The other **8 P0/P1s were dropped with no recorded verdict** — not verified, not dismissed, not deferred. Just gone. They are absent from the confirmed list, from the "known-open/deliberately deferred" exclusion list, and from AUDIT_R3 §4's dismissals.

Dropped without adjudication:
- **P0** `index_repo`'s 5000-file / 30s cap turns every merge into a bogus rejection on any repo big enough to need this product
- **P1** `POST /parcel/update` has no lease-ownership check — any agent can poison any parcel's `content_hash`
- **P1** duplicate parcel ids collapse (`@property`/`@x.setter`) — a code region vanishes from the parcel map
- **P1** hook `_keepalive` discards `heartbeat`'s `False`
- **P1** reaper: transient exception kills TTL reclaim + decay; blocking SQLite on the loop thread
- **P1** symbol-span leases unenforced; **P1** ROUND4 §1's wrong git model
- All 9 P2s (schema versioning, unbounded `events`, `_find_lease` ignores `mode`, `release()` has no ttl clause, …)

The second run answered follow-ups for the 5 it carried and **asked none for the rest**. R3's critic said the audit was "diff-shaped." R4's is *carry-shaped*: the filter wasn't severity or evidence, it was which findings survived the handoff.

## 2. The confirmed list is stale against HEAD

Brief says `b383008`; HEAD is **`576c73b`**. Three commits landed after the list was frozen:
- `15730cf` — fixed the **P0** agent-client 5s timeout (was in the partial's still-open list, never in my confirmed list)
- `027df7c` — fixed the `newfixes` **P0** that my confirmed list still reports as open
- `576c73b` — closed the four `mutation` P2s

So the one P0 I was asked to assess is already fixed, and **the three newest commits are now the least-reviewed code in the repo — audited by nobody.** That is precisely the "every round's fixes are the next round's bugs" pattern, reproducing inside Round 4 itself.

## 3. Executed spot-check of the least-examined area — the dropped reaper P1 is real, and worse than rated

`swarmsync-hook-guard`: **0** mentions in the R4 audit. `demo/run_demo.py` (739 lines, the largest file in the repo): **1** mention. `events.py`: 1. I took the reaper (5 mentions, zero findings carried) and ran it:

```
reaper task done? True
exception: OperationalError('database is locked')
passes completed before death: 2
=> loop kept running? False
```

`swarmsync/coordinator/reaper.py:165-173` — `while True:` with **no try/except**. One `sqlite3.OperationalError("database is locked")` — the single most expected error in a WAL database with concurrent writers, i.e. the product's normal operating state — permanently kills the TTL reaper *and* pheromone decay for the server's lifetime. `swarmsync/server/app.py` `lifespan` does `create_task` with **no `add_done_callback`, no supervision, no logging**, so it dies as an unretrieved task exception: silently.

Consequence: TTL reclaim of crashed agents' leases is an advertised core guarantee. When it dies, crashed agents' leases are never reclaimed and the swarm deadlocks permanently — with no event, no log, and all gates green. **I'd rate this P0, not the P1 the partial gave it.** It was dropped from the confirmed list anyway.

Also confirmed by read (`adapter.py:262-267`): `_keepalive` calls `client.heartbeat(...)` — declared `-> bool` — and discards the return; `cmd_precheck` then returns `None` (ALLOW). An agent whose lease was lawfully reaped and re-granted keeps editing **the shared hook working tree**. That's the one surface where the lease is the only protection.

## 4. Coverage shape

~52% of source (≈3,100 of ~6,000 lines) carries zero findings: `broker.py` (411), `git_ops.py` (338), `runner.py` (324), `indexer.py` (282), `client.py` (265), `mutators.py` (184), `events.py` (177), `models.py` (175), `reaper.py` (173), `db.py` (139), `serve.py` (56), `demo/run_demo.py` (739), `swarmsync-hook-guard`.

The R3 diff-shape test can't discriminate here (R2..HEAD touched nearly every file). The real shape: findings concentrate on `app.py`/`integrator.py`/`leases.py` — **the same three hot files every round has relitigated**. The `unexamined` dimension *did* work — it reached the reaper, the client, `parcel/update`, multi-root. Then the carry-over dropped most of what it found. The instrument was fixed; the recording wasn't.

## 5. Reading-only findings

The four `mutation` P2s are execution-backed by construction (deletability was measured). The `docs` findings are read-based, which is appropriate — except the README quickstart claim, which asserts runtime facts (`python` absent, `python3` is 3.10 vs `requires-python >=3.11`) that are box-specific and should have been executed on a stock container, not the dev box.

## 6. Still-unmodeled failure modes

- **The demo itself** — the product's own proof, largest file, effectively unaudited.
- **Upgrade/migration** — raised (no schema versioning) then dropped. `blackboard.db` is a persistent file at a stable path; `init_db` no-ops on an existing DB, so a schema change surfaces as a per-query `no such column` at runtime.
- **Resource exhaustion** — raised (unbounded `events`, unbounded `_ensure_parcel` rows) then dropped.
- **Operational/supervision** — the reaper death above. No dimension owned "what happens when a background task dies."
- **Human error** — nothing at all (wrong `CLAUDE_PROJECT_DIR`, stale `.swarmsync-active` marker, two servers on one DB).

## Recommendation for R5

Do not start from the confirmed list — it is a lossy subset of an audit that already ran. Start by **re-adjudicating the 8 dropped P0/P1s + 9 P2s in `AUDIT_R4_PARTIAL.md`** (each gets an explicit verdict: confirmed / refuted / deferred-with-reason), then audit `15730cf`/`027df7c`/`576c73b`, which no one has looked at. The reaper's unsupervised `while True` is the highest-value single fix I found and it is sitting in the discard pile.