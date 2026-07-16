# HANDOFF — where the quality campaign stands

Context for continuing work on swarm-sync in a **new session started from this
directory** (`~/projects/swarm-sync`). Read this first, then `DESIGN.md`.

## What this project is

swarm-sync ("Pheromesh") lets multiple AI agents edit ONE codebase concurrently
without colliding: a classifier splits the repo into leasable *parcels*; a
SQLite-WAL *blackboard* holds CAS leases + frozen interface contracts + an event
log; each agent works in its own git worktree; a **serial, test-gated integrator**
lands branches; a TTL reaper reclaims crashed agents' leases. A Claude Code hook
adapter (`swarmsync/hooks/adapter.py` + `scripts/swarmsync-hook-guard`) enforces
the leases transparently for Claude subagents. Architecture: `DESIGN.md`.
Build history: `MEMORY.md`. Hardening history: `HARDENING.md`.

## Current state (verified)

- Private repo: `github.com/Aigeninc/swarm-sync` (branch `master`). HEAD = the
  Round 2 hardening commit.
- **248 tests green**, ~95% coverage (core modules 92-100%). ruff clean. mypy clean.
- `python demo/run_demo.py` exits 0 standalone with all 5 money shots passing.
- Three concurrency regression tests exist and were each verified to FAIL when
  their fix is reverted (CAS race, reaper-vs-heartbeat TOCTOU, concurrent /integrate).

## The campaign so far

1. **Round 1 audit** (7 dimensions, adversarially verified): found **1 P0, 8 P1, 20 P2**.
   Verdict: "strong prototype, not proud-to-ship". Core story: guarantees that held
   inside the in-process broker evaporated on the networked/hook path.
2. **Round 2 hardening**: closed them all — serialized `/integrate` (asyncio.Lock) +
   atomic integrate (reset-hard on failure); atomic reaper `UPDATE...RETURNING`;
   events seq via RETURNING; classifier per-file parse guard; git arg-injection
   guards; optional token auth + realpath managed-roots allow-list + localhost bind +
   sandboxed pytest; hook lease keepalive; worktree cleanup; ruff/mypy wired + 13 type
   errors fixed; the missing concurrency regression tests; DESIGN/README reconciled.
   A self-inflicted P1 (managed-roots broke the standalone demo) was found and fixed
   (demo now self-configures `SWARMSYNC_ROOTS`; its subprocess test scrubs the env so
   it can't mask an out-of-box break again).

## NEXT STEP: Round 3 re-audit (not yet done)

Re-run the same 7-dimension adversarial audit against the **hardened** code as an
independent second opinion. If it returns no P0/P1 and green gates, the bar is met;
otherwise do another targeted fix round.

The audit workflow script (re-runnable by absolute path):
`~/.claude/projects/-home-keith-Documents/<session>/workflows/scripts/swarmsync-audit-r1-*.js`
— or simply re-author it: parallel expert agents per dimension (concurrency,
architecture, code quality, security, robustness, tests, docs) -> adversarially
verify each serious finding -> synthesize a prioritized report.

## The quality bar ("proud to ship")

ruff + mypy clean · suite green, meaningful coverage, no flakes · concurrency
invariants hold under real stress (no double-leases, no lost work, trunk always
test-green) · demo money shots pass standalone · sound error handling · no confirmed
P0/P1 · DESIGN.md matches the code · README genuinely usable by a stranger.

## ⚠️ SAFETY RULE FOR AUTONOMOUS AGENTS (learned the hard way)

A prior audit agent attempted `rm -rf * .[!.]*` (a wipe-current-directory command)
while the session's cwd was the user's `~/Documents`. It was caught and rejected by
a permission prompt — no data was lost — but it would have destroyed personal files.

**Therefore, any agent that runs shell commands MUST be told:**
- Operate ONLY within this project and `/tmp`. Never touch anything outside them —
  especially nothing under `~/Documents`.
- For scratch work use `mktemp -d` and reference it by **absolute** path. Clean up with
  `rm -rf "$THAT_ABSOLUTE_PATH"`.
- **NEVER** run a wipe-current-directory command (`rm -rf *`, `rm -rf .`, `rm -rf ./*`)
  and never a destructive command built from a possibly-empty/unset variable.
- Every `rm` must name an explicit absolute path.

Running sessions from THIS directory (rather than `~/Documents`) is the structural
half of that fix: the worst case then hits a git-backed, remote-pushed repo.
