# Symbol-Granularity Mode: The Recommendation

## 1. Is it feasible?

**Qualified yes — but not as the default, and not on the surface your README leads with.**

Two agents editing two different functions in the same file, concurrently, *can* be made genuinely safe. The mechanism is real and it's simpler than anyone expected going in: don't try to predict whether the edits will collide, just check afterward whether each agent stayed inside what it leased — by re-parsing the file before and after and comparing. That check is sound, cheap, and provable.

The catch is three-fold, and it's material. First, symbol mode only works for agents that get their own git worktree and go through the integrator — which means it does **not** work for Claude Code subagents under the hook adapter, the path your README leads with. Second, the concurrency it buys is narrower than the pitch: the moment either agent adds an import (which LLM agents do constantly), the file silently serializes back to file-granularity anyway. Third, the current lease store has no idea that a file has parts — it will happily hand out a whole-file lock and a symbol lock on the same file at the same time, to two different agents, right now. That last one is a bug that exists today and has to be fixed before symbol mode means anything at all.

So: feasible, worth building, worth building **in stages** — and the first two stages are worth shipping even if you never turn symbol mode on.

---

## 2. The recommended design

**Core: structural conformance checking at the integrator** (from Design 1), **built on top of a lease store that understands file hierarchy** (the diagnosis from Design 3), with **Design 2's diff-based approach explicitly rejected** — two independent investigations proved it can't work.

### How it works, in plain terms

The codebase already splits every Python file into *parcels*: one per function, one per class, and a "glue" parcel (`m.py::<module>`) that owns everything else — imports, module constants, blank lines between functions. Critically, **every byte of the file belongs to exactly one parcel's content hash.** That's not an aspiration; it's how the indexer was built, and it's the whole foundation.

So the check is:

1. Agent finishes work on its branch. It asks the integrator to land it.
2. The integrator takes the file as it was at the fork point, and the file as it is on the branch. Parses both.
3. Compares parcel content hashes. Any parcel whose hash changed — or that appeared or vanished — is a parcel this branch touched.
4. **If that set isn't a subset of what the agent actually leased, refuse the merge.** Before merging. Nothing touches trunk.

That's it. No diff parsing, no line arithmetic, no byte offsets.

### Why this specific shape

The obvious alternative — read `git diff`, check the changed line ranges fall inside the leased span — was built and tested, twice, by two different investigations. **It has an unfixable hole.** Adding a `@cache` decorator to a function you legitimately lease, and inserting an entirely new top-level function into the gap next to it, produce a **byte-identical** diff header (`@@ -11,0 +12,4 @@`). Textual diff cannot tell them apart. Allow it and an agent can smuggle arbitrary new code; deny it and you reject every decorator. Structural comparison separates them cleanly, because it's asking a different question: not "where did the lines move" but "which symbols are now different."

### What it catches that git doesn't

Measured, on real branches:
- Agent runs a formatter → flags `<module>` → **rejected** (git merges this cleanly today, and nothing notices)
- Agent adds an import while holding only a function lease → flags `<module>` → **rejected**
- Agent inserts a new function next to the one it leased → **rejected**
- Agent renames a symbol → flags both the old name (vanished) and the new (appeared) → **rejected** unless it holds both
- Agent legitimately edits its function's body, adds a decorator → **passes**

### The prerequisite nobody can skip

`leases.acquire`'s conflict rule is currently a single string comparison: `parcel_id = parcel_id`. Verified against the live code: agent A can hold `m.py::<module>` (the whole file) and agent B can hold `m.py::alpha` **simultaneously**, both write mode, both granted. The only code in the system that knows `<module>` overlaps everything in its file is `co_schedulable`, an advisory function inside the broker's scheduler that `POST /lease` never calls and the hook adapter never calls.

This means conformance alone is not enough. Conformance proves *"you stayed inside your leases."* It says nothing about *"your leases were disjoint from theirs."* Today, they aren't. **The lease store has to learn the containment relation before symbol mode is safe** — that's Design 3's diagnosis, and it survived every attack.

---

## 3. Why this and not the alternatives

**Design 2 (predict conflicts from line gaps + confine via diff hunks)** — rejected on mechanism. Its confinement gate reads `git diff -U0` hunk headers, and git's normalization makes "append a line to the end of my own function" and "write into the glue right after it" produce identical output. The permissive fix re-admits real conflicts; the strict fix rejects the most common legitimate edit there is. There's no third option, because the normalization it chose to trust is lossy exactly at the boundary that matters. Its spec also parses `@@ -s,n +@@`, and real git emits `@@ -2 +2 @@` for single-line changes — an implementer following the text would ship a gate that silently ignores the modal edit. Both problems were found by measurement, not argument.

*Worth keeping from it:* the finding that the line-gap rule needs ≥1 unchanged **line**, not byte-disjointness (`co_schedulable` currently returns `True` for two byte-disjoint edits separated by zero unchanged lines — that's a real 5-line bug), and the measured concurrency data: on this repo, 4,516 same-file symbol pairs, the gap rule loses exactly **1**. PEP 8 does the work.

**Design 3 (lease hierarchy + integrator confinement)** — its *diagnosis* is the best work in the set and I'm adopting it wholesale. Its *confinement mechanism* is Design 2's, and inherits the same hole. It also proposed promoting `parcels.path` to the lease store's join key while that field is derived from unvalidated client strings — reproduced: `m.py::<module>` and `./m.py::<module>` are two different locks on one inode.

**Design 1** wins because its soundness argument rests on a property the code already has (every byte in exactly one hashed parcel), rather than on arithmetic it has to get right.

---

## 4. Bug-proneness — the honest answer

You were right to suspect this. **It is bug-prone, and the attacks found the bugs in the "measured, verified" sections of every design.** That's the pattern here: each round's fix contains the next round's bug, and it hides inside the part the author was most confident about.

### The one that would have shipped

Design 1's soundness proof is **false as stated**, and it's false in the worst way — silently.

Parcels partition every byte *of the parcel list*. But the check reads them into a dict keyed by parcel id. `@overload` — idiomatic in typed Python, which this repo is — emits **three parcels sharing the id `m.py::foo`**. The dict keeps the last one. Measured: change the first overload's return annotation, and the changed-parcel set comes back **empty**. The branch merges holding **zero leases**. 76 bytes of source living in no hashed bucket.

The proposed property test ("flip a byte, assert a hash moves") would have **passed** on its stated corpus and missed this entirely.

**The fix:** any file whose parse emits a duplicate id is dropped to `<module>` (whole-file) granularity — fail closed. **The test that pins it:** the totality property test must include a duplicate-id corpus (`@overload`, method overloads, `class C` + `def C()`, `try/except ImportError` redefinition), and must fail when you delete the duplicate-id check.

### The others, with their pins

| Failure | Pin (test fails if mechanism deleted) |
|---|---|
| **`base_commit` is client-supplied and unvalidated.** Pass the branch's own tip → diff is empty → conformance passes vacuously → everything merges. Green integrate, green tests, unleased work on trunk. | Server derives `git merge-base` itself; a request supplying a bogus base_commit must reject. |
| **Duplicate parcel ids** (above) | Duplicate-id corpus in the totality property test |
| **Lease store grants `<module>` + `symbol` on one file concurrently** | Test asserting the two acquires conflict |
| **Path aliasing** (`./m.py` vs `m.py` = two locks, one file) | Parcel-id canonicalization at the API boundary; aliasing test |
| **Test files are parcels.** Measured: `index_repo` indexes `tests/*.py`. Under conformance, adding a test is a lease violation by default — and a new test function's id doesn't exist at the fork point, so it can't even be leased. | Explicit test: "agent edits function + adds a test" must land |
| **Schema change fails open on old DBs.** `CREATE TABLE IF NOT EXISTS` + table-name-only validation means new columns silently don't appear, then every `acquire` throws — and the hook's `except Exception: return 0` swallows it into *allow*. A broker-only feature would take out coordination on the shared tree. | `user_version` + real migration; test that an old DB upgrades rather than fails open |
| **New rejection route skips the rollback re-index** — exactly what happened in Round 3 | Parametrize the existing rejection test over `lease_violation` too |

### Residual risk no test will catch

1. **Semantics.** Two agents edit two disjoint functions. Both in-span. Clean merge. Conformance green. And A changed a return contract that B's function depends on. Conformance is *syntactic containment* — it has no opinion about correctness. The pytest gate is the only backstop, and it runs *impact-selected* tests over a best-effort dependency graph that swallows its own exceptions. **Do not let "verified" read as "correct."**

2. **The trust boundary is weaker than it sounds.** `agent_id` is a free-form request field, explicitly documented as *not* derived from the auth token. An agent that wanted to lie could claim to be whoever holds the lease. So this is real enforcement against **agent bugs and agent sloppiness** — which is the actual threat, and worth building — and advisory against **agent malice**. Say that in the docs; don't claim the boundary.

3. **Future parcel kinds.** Soundness rests entirely on partition totality. Anyone who adds a parcel kind without maintaining "every byte in exactly one hashed bucket" punches a silent hole. The property test is the only thing standing between "sound" and "sound until someone touches the indexer."

---

## 5. What it costs

Honestly, and larger than any of the three designs claimed.

**New code**
- `coordinator/spanguard.py` — the conformance check plus a parse-from-bytes helper. ~150 lines. Self-contained, pure, takes git refs and an id set.
- Parcel-id canonicalization/validation at the API boundary. ~40 lines. **Not optional** — the lease store's containment rule is meaningless if `./m.py` and `m.py` are different locks.
- Schema migration mechanism (`user_version` + `ALTER`) and column-level validation in `db.EXPECTED_TABLES`. This forces the open "no schema versioning" P2 to the front. ~80 lines.

**Modified**
- `server/leases.py` — the containment join in `acquire`. ~15 lines of SQL, and **the entire review burden of the change**. This is the system's atomic compare-and-swap, the most raced statement in the codebase, and a wrong predicate here fails *open* — two writers, silent corruption.
- `classifier/indexer.py` — `line_start`/`line_end` columns, duplicate-id detection. ~40 lines.
- `classifier/graph.py` — line-gap rule replaces byte-gap. ~10 lines. (This is a real bug fix regardless.)
- `coordinator/integrator.py` — conformance as a pre-merge precondition, `lease_violation` route. ~30 lines.
- `blackboard/models.py`, `schema.sql`, `server/app.py`, `agent/runner.py`, `hooks/adapter.py` — plumbing, ~60 lines.
- `grep byte_start` finds **28 sites across 10 files** including three separate column lists in `store.py`. The "+2 columns" estimate is wrong.
- 11 existing `mode='symbol'` call sites across four test files move.

**Tests** — the seven-branch conformance corpus, the duplicate-id corpus, the partition-totality property test, every fail-closed route (unparseable head, missing base, non-`.py`, new file, deleted file), the schema-migration test, the lease-containment test, `lease_violation` added to the rejection-route parametrization. **~600 lines.**

**Realistic total: ~450 lines production, ~600 test, across 4 stages, plus one forced prerequisite (schema versioning) that the project has been deferring.** Call it 3–4 focused work units, not one.

**The cost nobody measured, and it's the one that decides the feature's worth:** the rejection rate. The scheduling gain is basically free (1 lost pair out of 4,516). But the `<module>` glue parcel owns imports, and *any* edit that adds an import flags `<module>`, which overlaps the whole file. So the realistic agent workflow — edit a function, add an import, add a test — needs `{m.py::beta, m.py::<module>, tests/test_m.py::<module>, tests/test_m.py::test_beta}`. That's two whole files. **That's file mode, plus a rejection risk.** Nobody has measured how often real LLM edits are import-free, and that number is the feature's actual value.

---

## 6. What it does NOT deliver

**Symbol mode does not work on the Claude Code hook path. All three designs independently concluded this, and I agree.** This is the most important paragraph here, because it changes what you're buying.

Hook subagents share **one working tree**. There's no branch, no fork point, no merge, no integrator, no rollback. Every enforcement mechanism above works by *refusing to merge* — and there's nothing to refuse; the bytes hit the shared file the instant the tool runs. A `PostToolUse` hook could compute the same changed-parcel set, but it runs *after* the write, so its only options are to alarm (too late — a concurrent agent may already have read corrupted state) or auto-revert (which would destroy a co-resident agent's legitimate write to a *different* parcel in the same file — the exact thing symbol mode exists to permit). And it's moot regardless: `Bash` writes bypass the hook gate entirely (known-open P1-3).

**So: symbol mode is a worktree + integrator feature. Broker path only.** The hook adapter should *hard-error* on `mode="symbol"` at startup — not a doc note, a raised exception — because it hardcodes file mode by implementation accident today, and the next refactor that plumbs `mode` through generically will silently arm symbol mode on the one path where it cannot be enforced.

Plainly: **your README leads with the hook path, and the hook path stays a well-built file lock.** The powerful version lives on the broker path. If the goal was "my Claude Code session gets symbol concurrency," the answer is no — not because the arithmetic is hard, but because that path has no isolation, no gate, and no rejection point, and symbol concurrency is meaningless without at least one of them.

**Also not delivered:**
- **Non-Python files** — no parser, no parcels, file granularity forever.
- **Structural edits** — adding, renaming, deleting, or reordering a symbol all escalate to the whole-file lease. Symbol mode buys concurrency for edits that don't change the file's symbol structure. That's a much smaller promise than "two agents edit the same file," and it's the largest honest one this mechanism supports.
- **Imports and formatters** — same escalation. By design, and this is the ceiling on the headline feature.
- **Ghost contract rows on rename/delete** — narrowed (you can't land a rename holding only a symbol lease) but not fixed. Separate work item.
- **Rebase-and-resubmit** — `DESIGN §5.5` is a label; `grep -rn rebase --include=*.py` finds no implementation. `lease_violation` joins `needs_rebase` and `merge_rejected` as a terminal status nothing retries. Work is preserved on the branch but nobody picks it up. **Symbol mode makes rejections go up, so this stops being deferrable if symbol mode goes on.**

---

## 7. Staged plan

Each stage ships. Each is independently valuable. **You can stop after any of them.**

### Stage 1 — Revive frozen contracts at file granularity *(no symbol mode required)*
The whole frozen-contract subsystem is currently dead code: `extract_contracts` only emits contracts for function/class parcels, but file mode resolves every lease to `<file>::<module>`, so the sets never intersect. Measured on `sample_repo`: **10 contracts emitted, 0 of them reachable.**

Fix: derive `frozen_files` from the frozen symbol ids, and upgrade any file-mode task whose file contains a frozen symbol to `exclusive`. ~20 lines plus tests. **This works on the hook path** — the surface you lead with. It kills a dead subsystem and delivers a real product improvement.

Blocker inside it: `exclusive` and `write` are currently **indistinguishable** in the conflict predicate. Give `exclusive` real meaning or delete the mode and say plainly what contract protection is. Either way, this is where you find out.

### Stage 2 — Make the lease store correct *(no symbol mode required)*
Parcel-id canonicalization + validation. Schema versioning and real migration. `line_start`/`line_end` columns. The containment join in `acquire` so `m.py::<module>` and `m.py::alpha` actually conflict. Line-gap replaces byte-gap in `co_schedulable`.

This is a **bug fix, today, in file mode** — the hook adapter and the broker currently name different locks for the same bytes, and nothing stops them. It's also the highest-risk edit in the repo and deserves its own review. Ship it alone.

### Stage 3 — Conformance checking, symbol mode still off
`spanguard.py`, duplicate-id fail-closed, server-derived `base_commit`, the totality property test with the duplicate corpus, `lease_violation` wired into the rejection-route parametrization. Run it in **shadow mode** — compute the verdict, log it, don't enforce. That gives you the rejection-rate number that Section 5 says nobody has, on real traffic, at zero risk.

### Stage 4 — Turn symbol mode on, opt-in, broker-only
Only if Stage 3's shadow data says the rejection rate is tolerable. Hook adapter hard-errors on `mode="symbol"`. Documented as Python-only, no-imports, no-renames, no-formatters. And §5.5's retry path stops being a label first — because without it, every rejection is a dead branch.

---

**The bottom line:** it's feasible and the mechanism is sound. But Stages 1 and 2 fix real bugs that exist right now, on the surface you actually ship, and Stage 3 tells you — with data instead of argument — whether Stage 4 is worth it. Given this codebase's history, I'd rather you buy the answer than the feature.
---

## Verified independently before recording (not taken on the panel's word)

- **git's merge rule**: measured. 0 unchanged lines between edits → CONFLICT; 1 → CLEAN.
  ROUND4.md §1's "3 lines of context" is diff's display padding, not the merge rule. Corrected.
- **Parcel spans exclude the separators**: `alpha` = bytes[0:30], `beta` = bytes[33:62]; the two
  PEP 8 blank lines belong to no symbol parcel. So normally-formatted Python already satisfies
  git's rule for two-agents-one-file.
- **The lease-containment hole is REAL and live today**:
  ```
  A holds the WHOLE FILE m.py : True
  B holds m.py::alpha inside it: True
  *** BOTH GRANTED: two agents hold write access to the same file ***
  ```
  `leases.acquire`'s entire conflict rule is `WHERE l.parcel_id = :parcel_id` — a string match.
  Nothing in the lease store knows `m.py::<module>` contains `m.py::alpha`.
  **Reachability, checked:** file mode resolves every task to `<file>::<module>` and the hook
  hardcodes `<module>`, so both only ever compare same-shaped ids — **string matching is
  sufficient and file mode is safe by construction**. Symbol mode mixes the two shapes and is
  therefore exposed. `co_schedulable` (broker-only, advisory, never called by `POST /lease` or the
  hook) is the only code that understands containment — i.e. in symbol mode the invariant holds
  only because the scheduler happens to be right, which is this codebase's signature failure shape.
- **Contract detection is NOT dead in file mode** (contrary to how the audits' P1-8 reads): with a
  genuinely frozen contract, `integrate` emits `contract_change` on merge regardless of lease
  granularity — it diffs the contracts table before/after re-index. What IS dead in file mode is
  the *preventive* half: the exclusive-lease upgrade and `co_schedulable`'s frozen clause.
  Detection survives; prevention doesn't.
- **The demo runs `mode="symbol"`** (`demo/run_demo.py:300,432`), not the default file mode. So all
  five money shots currently demonstrate the ambitious mode, including money-shot #1's
  "two agents, one file, zero collisions".

## Consequence for an MVP

Shipping **file-granularity locking** as the MVP is safe from the lease-containment hole *by
construction*, keeps contract detection, and is where this campaign's hardening actually landed.
It costs: two-agents-one-file, and the preventive half of contracts. The demo would need to be
reframed to demonstrate what ships.

If symbol mode is later pursued, the containment hole must be closed FIRST — it is a hole in the
load-bearing primitive, not a symbol-mode feature gap.
