# Round 5 — what was closed, and what is left

Written after working `AUDIT_R4.md`'s blocking list. Read `AUDIT_R4.md` first for the
findings themselves; this is the disposition.

## Closed this round (each mutation-tested: revert the fix, the suite fails)

| From | Defect | Fix |
|---|---|---|
| R4 P0-1 | **Crash mid-integrate left an un-gated merge on trunk forever.** `integrate` merges then gates, so trunk carries an un-gated merge for up to 600s. SIGKILL/OOM — or Ctrl-C/uvicorn shutdown, which `except Exception` does not catch — left it there with no event, no rollback, and nothing on restart able to detect it. "Trunk is always test-green" silently false from then on. | `integrate_started` emitted **before** the merge carrying `trunk_sha_before`; `except BaseException` rolls back and re-raises; `reconcile_orphaned_integrations` at startup resets any start with no terminal event and emits `integrate_orphaned`. Verified end-to-end with a real `kill -9`. |
| R4 P1-2 | **The merge-conflict path skipped R3's rollback re-index** — R3's own fix applied to the path it was looking at, not its sibling — leaving the agent's self-reported `content_hash` describing code in no git ref. | Routed through `_reject_and_reset`, and asserted as a **class**: the test is parametrised over every rejection route (conflict / red gate / post-merge error). |
| R4 P1-1 | **Multi-root silently corrupts.** Parcel ids are root-relative with no repo qualifier, so two roots sharing a filename collide on one id — rows overwrite, leases conflate. R3 had shipped a repeatable `--root`, which is exactly how you'd hit it. | **Hard error.** `check_single_root` refuses to start (in `lifespan`, so it covers uvicorn-direct too); `--root` no longer repeatable; README says one server, one repo. |
| R4 P1-3 | **An in-repo symlink alias took two write leases on one inode** — and the root cause was the *indexer*, which gave the alias its own parcels. | Both sides now agree the canonical file is the parcel: `index_repo` skips in-repo aliases, `_relpath` collapses them onto the target. Out-of-repo symlinks keep S5's behaviour (own name, still leased). |
| R4 M-1 | `build_graph`'s parse guard was deletable with the suite green, and reachable: readers build parcels from the **DB** and re-read the file from **disk**, where agents are mid-edit. | Both arms pinned. |
| R4 docs | DESIGN stated worktree isolation as unconditional and never mentioned the hook path (where it is false); documented none of the operational surface. README's first command didn't exist on a stock box. | §5.1 now states the hook-path exception and its two consequences; new §7a documents every env knob, both launchers' differing ports, the `ensure_parcel` caveat, and the crash-recovery contract. Quickstart leads with the version check and `python3`. |

Gates: ruff clean · mypy clean · **307 tests green (3× no flakes)** · 95% coverage · demo 5/5 standalone.

## Still open — the honest list

**Architectural (deferred by scope since R3, never contested):**
- **P1-7 / P1-8 / P1-9 are one question**: is the parcel/contract abstraction load-bearing or
  decoration? Today the *default* mode (file granularity) is the one where frozen contracts do
  nothing, and the mode where they work (symbol) is the unsafe one. Nothing confines an edit to
  its leased span, so span-disjointness is mutator etiquette, not an invariant. Renames emit no
  `contract_change` and leave ghost rows served as truth. **⚠️ ROUND4.md §1's option (a) is
  built on a wrong model of git** — the 3-way merge unit is "one unchanged line", not "3 lines
  of context". Re-derive before implementing.
- **DESIGN §5.5 promises rebase-and-resubmit; there is no implementation.** `needs_rebase` and
  `merge_rejected` are terminal — the broker only retries on `lease_denied`. R3's P1-6 fix
  preserves the branch, so the work survives, but nothing picks it up.
- **P1-3 Bash writes bypass the lease gate.** README and DESIGN now say so plainly; that is the
  deliverable unless someone gates `Bash` write-targets.

**Known-open, with a decision to make:**
- **The managed-root leaf-symlink escape** (R3's completeness critic): an out-of-repo symlink is
  still indexed and read through, *by design* (S5 keeps it leasable). Is that a legitimate repo
  layout or an allow-list hole? The two goals genuinely conflict; pick one and write it down.
- **M-2**: the allow-list's `root + os.sep` boundary check is deletable with the suite green.

**Operational (all P2, all real):**
No schema versioning or migration path against a persistent DB · `events` grows unbounded and is
simultaneously the claimed replay source of truth · the reaper does blocking SQLite on the
asyncio loop thread (a 5s `busy_timeout` stalls the whole ASGI server) and dies permanently on
one transient exception · `/parcel/update` has no lease-ownership check (verifiers refuted the
P1 impact story; the missing check is still real).

## The rule this campaign actually produced

> **A fix without a test that fails when you delete the fix is not a fix. It is a hope.**

Every round has proven it again, including this one: my crash-recovery fix keyed reconciliation
on `(repo, branch, into)` while the terminal events carried no `repo` — a *completed* merge
looked like an orphan, and reconciliation would have reset landed work, the exact data loss it
exists to prevent. My own test caught it before it shipped. Separately, my first symlink pass
regressed S5 exactly (out-of-repo link → no lease → silent bypass) and the existing test caught
that. Neither would have been found by review.

Corollaries, all earned the hard way:
1. **When a fix breaks an existing test, the test is a suspect — but so are you.** R2's reaper
   test encoded the double-lease bug as a requirement. My symlink change broke a test that was
   right.
2. **A test can pass for the wrong reason.** The test proving the `kind` P0 fix used
   `package.json` — the one shape that worked — while every new `.py` file still bricked the
   broker.
3. **~50% of unverified findings die on contact.** Six of eleven in R4. Never act on an
   unverified finding; reproduce it first.
4. **Fix the class.** "Every reader of the parcel map re-parses the world and dies on one bad
   file" appeared three times as three exception types (ValidationError, KeyError, SyntaxError)
   before it was fixed as a shape.
