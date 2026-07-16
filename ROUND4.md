# Round 4 — approach

Written at the end of Round 3 (2026-07-16), after the R3 re-audit (`AUDIT_R3.md`) and the
targeted fix round that followed it. Read `AUDIT_R3.md` first, then this.

## Where things stand

Fixed and defended by mutation-proof tests this round (see "The rule" below):

| ID | Defect | Fix |
|---|---|---|
| **P0-1** | `heartbeater.stop()` in the inner `finally` → `/parcel_update` + `/integrate` ran with **zero** beats against a 30s TTL; a gate slower than the TTL got the working agent's lease reaped and re-granted to a second agent | beat now stops in the outer `finally`, covering the gate |
| **P0-2** | Hook failed **open** for any unindexed file (all non-`.py`, all new files) — and hook subagents share ONE working tree, so the lease was the only protection | `ensure_parcel=True` on the hook's lease path auto-creates a coarse whole-file parcel; transient blackboard failure still fails open |
| **P1-1** | `heartbeat` had no `ttl_expires_at` predicate → an expired lease could be resurrected into a second live write lease on one parcel | added the clause, matching `acquire`'s lazy-expiry predicate |
| **P1-2** | `add_worktree`'s guard checked only a leading `-`; the real sink is `shutil.rmtree`, so `../..` deleted directories outside the repo | strict name allow-list + containment assert before any delete |
| **P1-4** | pytest gate had no timeout and ran under the global `integrate_lock` → one hanging test wedged ALL integration forever | `SWARMSYNC_GATE_TIMEOUT` (default 600s) + process-**group** kill; timeout = rejection |
| **P1-5** | Rollback reset git but not the blackboard → `parcels.content_hash` kept the rejected merge's values, which `_check_read_deps` reads | `_reject_and_reset` re-indexes from the restored trunk |
| **P1-6** | Worktree cleanup `git branch -D`'d the agent branch on `merge_rejected`/`needs_rebase`, destroying the only ref to the work | `delete_branch` only on `merged`; `remove_worktree` tolerates a missing worktree |
| **P1-10** | `SWARMSYNC_ROOTS` undocumented; getting it wrong silently disabled ALL leasing | documented in the quickstart, `--root` flag, managed roots printed at boot |
| *(docs)* | README overclaimed contract and integration guarantees against DESIGN + code; no trust model | guarantees table rewritten to be true; security/trust-model section added |

Gates: ruff clean · mypy clean · **274 tests green (3× no flakes)** · 95% coverage · demo 5/5 standalone.

**Explicitly still open** (deferred by scope decision, not fixed):
`P1-3` Bash-mediated edits bypass the gate · `P1-7` span-disjointness ≠ git-merge safety ·
`P1-8` frozen contracts inert at the default file granularity · `P1-9` renames/deletes emit no
`contract_change` and leave ghost contracts · the P2 backlog in `AUDIT_R3.md` §3 · the
completeness critic's symlink-escape P1 and its P2s in §6.

## The rule Round 4 runs on

> **A fix without a test that fails when you delete the fix is not a fix. It is a hope.**

R3's central finding was not any single bug — it was that Round 2's own headline fixes were
*undefended*: the atomic-integrate rollback and 3 of 7 auth guards could each be deleted with
248/248 still green. Every fix in the table above was verified by reverting it and watching the
suite go red (`P1-4` by watching it hang under an external bound).

Two things this round proved are worth carrying forward, because both would have silently
invalidated the work:

1. **A regression test can encode the bug.** `test_reap_once_excludes_lease_renewed_by_heartbeat_in_the_race_window`
   built its scenario with an already-expired lease and asserted the heartbeat revived it —
   i.e. it asserted P1-1's double-lease bug *as a requirement*. Round 2 shipped the hole with a
   regression test sitting on top of it. When a fix breaks an existing test, the test is a
   suspect, not an authority.
2. **A test can pass for the wrong reason.** The first two drafts of the P1-5 test passed with
   the fix deleted — one probed a path where `run_index` had not run yet, the other used a
   comment-only edit that the content hash does not see. Both looked right. Only the
   delete-the-fix check exposed them.

So: for every Round 4 change, state the mutation, run it, and record the result. If the suite
stays green, the test is wrong — fix the test before believing the fix.

## What Round 4 should do, in order

### 1. Fix the classes, not the four leftover findings

R3's verdict on R2 was "competent, targeted work that stopped at each bug's boundary" — it
treated R1's list as the specification. Do not repeat that with R3's list. Each remaining item is
an instance of a class; hunt the class:

- **P1-7 / P1-8 / P1-9 are one question wearing three hats:** *is the parcel/contract abstraction
  actually load-bearing, or is it decoration?* Symbol-granularity leases are unsafe (span
  disjointness is not git-merge safety — git merges line hunks with 3 lines of context), and the
  contract machinery is dead code at the file granularity that is the default *and* what the hook
  hardcodes. So today the system runs in the mode where contracts do nothing, and the mode where
  contracts work is the unsafe one. Decide the model before writing code:
  - require a ≥3-line gap between co-scheduled spans (makes symbol mode true of git's merge unit); **and/or**
  - implement the rebase-and-resubmit path DESIGN §5.5 already promises (makes conflict recoverable rather than fatal — note `grep -rn rebase` finds **no implementation**, only status literals and docstrings); **and/or**
  - map `frozen_ids` up to their owning `<file>::<module>` parcel in file mode so the exclusive upgrade and the `co_schedulable` frozen clause actually fire; and give `exclusive` a distinct meaning in `acquire` (it is currently indistinguishable from `write`) or delete the mode.
  Whatever you choose, DESIGN.md and README must say it. An honest "best-effort" beats a false guarantee.
- **P1-3 (Bash bypass) is the "enforcement surface" class.** The lease is enforced only where the
  agent happens to pick an Edit-family tool. Either gate `Bash` write-targets with the same lease
  check (deny on parse ambiguity against a leased path), or state plainly in DESIGN/README/SKILL
  that this is a cooperative protocol among well-behaved agents, not a sandbox. The README now
  says the latter — if Round 4 doesn't close it, that sentence is the deliverable, and
  `_is_active`'s marker file being deletable by the very agents it constrains should be fixed
  regardless.
- **The `heartbeat`/`acquire` predicate split (P1-1) is the "two predicates that must agree" class.**
  Audit every pair: `acquire` vs `heartbeat` vs `reap_once` vs `_find_lease`. Consider deriving
  them from one shared SQL predicate so they cannot drift again.

### 2. Close the audit's own blind spot

The R3 completeness critic's verdict: the audit was **diff-shaped, not codebase-shaped** — every
confirmed finding landed on a file Round 2 touched, inheriting R1's blind spots exactly. Files no
dimension opened: `blackboard/db.py`, `blackboard/schema.sql`, `classifier/store.py`,
`classifier/indexer.py`, `server/events.py`, `agent/client.py`, `agent/mutators.py`,
`scripts/swarmsync-hook-guard`, `demo/`. Its own spot-check of that territory immediately found a
**P1 symlink escape** (`_validate_managed_path` realpaths only the root; `rglob` then follows leaf
symlinks and indexes through them, and the read routes are unauthenticated) plus lost-update and
schema-versioning P2s. Round 4's audit must be scoped by *architecture*, not by the diff.

Unmodelled failure classes to cover: upgrade/migration (there is none — no `PRAGMA user_version`,
no migration code, against a persistent DB at a stable path), resource exhaustion (unbounded
`events` table), operational (the reaper does blocking SQLite on the asyncio event-loop thread
with a 5s busy_timeout — this makes the "one exception kills the reaper forever" P2 the *expected*
outcome under load, not a fluke), multi-user (unauthenticated read routes).

### 3. Re-audit — same harness, corrected scoping

Re-run the 7-dimension adversarial audit (concurrency, security, robustness, architecture, tests,
codequality, docs) with per-finding 3-lens verification (correctness / reachability /
already-handled). The workflow script is re-runnable by absolute path:

`~/.claude/projects/-home-keith-projects-swarm-sync/<session>/workflows/scripts/swarmsync-audit-r3-*.js`

Change three things when re-running it:

1. **Scope by architecture, not by the diff.** Name the unexamined files above explicitly in the
   dimension prompts and require each dimension to state which files it opened.
2. **Add a mutation dimension.** Point it at the fixes Round 4 lands *and* the ones this round
   landed: delete each fix, run the suite, report every fix the suite fails to defend. This is
   mechanical and it is the single highest-leverage check available — R3 estimates it would have
   caught roughly half the report.
3. **Keep the adversarial verifier.** It earned its cost: it refuted 4 findings outright and
   corrected several inflated severities. Do not accept a finding that hasn't survived it, and do
   not re-litigate `AUDIT_R3.md` §4's dismissed findings.

Ship bar, unchanged: ruff+mypy clean · suite green, meaningful coverage, no flakes · concurrency
invariants hold under real stress · demo money shots pass standalone · sound error handling · **no
confirmed P0/P1** · DESIGN.md matches the code · README genuinely usable by a stranger.

## ⚠️ Safety rule for autonomous agents (unchanged, still load-bearing)

A prior audit agent attempted `rm -rf * .[!.]*` while cwd was the user's `~/Documents`. It was
caught by a permission prompt; nothing was lost. Every agent that runs shell commands MUST be told:

- Operate ONLY within this project and `/tmp`. Never touch anything outside them — especially
  nothing under `~/Documents`.
- For scratch work use `mktemp -d` and reference it by **absolute** path.
- **NEVER** run a wipe-current-directory command (`rm -rf *`, `rm -rf .`, `rm -rf ./*`), and never
  build a destructive command from a possibly-unset variable. Every `rm` names an explicit
  absolute path.
- The audit is **read-only**: no commits, no pushes, no edits to the repo under test.

Run sessions from this directory, not `~/Documents`: the structural half of the fix is that the
worst case then hits a git-backed, remote-pushed repo.
