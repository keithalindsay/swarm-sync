export const meta = {
  name: 'swarmsync-audit-r4',
  description: 'Round 4 audit: architecture-scoped dimensions + mutation testing + adversarial verification',
  phases: [
    { title: 'Audit', detail: '8 dimensions incl. mutation + the unexamined modules' },
    { title: 'Verify', detail: '3-lens adversarial refutation of each serious finding' },
    { title: 'Synthesize', detail: 'prioritized report + completeness critic' },
  ],
}

const SAFETY = `
SAFETY RULES (mandatory, from a real near-miss where an agent tried to wipe the user's ~/Documents):
- Operate ONLY within /home/keith/projects/swarm-sync and /tmp. Never touch anything outside them, especially nothing under ~/Documents.
- For scratch work use \`mktemp -d\` and reference it by ABSOLUTE path only. Clean up by that absolute path.
- NEVER run a wipe-current-directory command (rm -rf *, rm -rf ., rm -rf ./*) and never build a destructive command from a possibly-unset variable. Every rm must name an explicit absolute path.
- This audit is READ-ONLY on the repo: no commits, no pushes, no edits to tracked files. You MAY run the suite and read-only git commands. If you need to mutate source to test something (the mutation dimension does), COPY the repo to an absolute mktemp -d dir and mutate the COPY -- never the real working tree. If you somehow must touch the real tree, restore it byte-exactly and verify with \`git status --porcelain\` before you finish.
- Activate the venv: \`source /home/keith/projects/swarm-sync/.venv/bin/activate\` before python/pytest/ruff/mypy.
`

const CONTEXT = `
PROJECT: swarm-sync ("Pheromesh") at /home/keith/projects/swarm-sync -- lets multiple AI agents edit ONE codebase concurrently without colliding.
Architecture: a classifier splits the repo into leasable *parcels*; a SQLite-WAL *blackboard* holds CAS leases + frozen interface contracts + an event log; each agent works in its own git worktree; a serial, test-gated integrator lands branches; a TTL reaper reclaims crashed agents' leases. A Claude Code hook adapter (swarmsync/hooks/adapter.py + scripts/swarmsync-hook-guard) enforces leases for Claude subagents -- NOTE those hook subagents share ONE working tree, so there is no worktree isolation on that surface.

READ FIRST: ROUND4.md (the plan for this round), AUDIT_R3.md (last round's full report), DESIGN.md (architecture). HARDENING.md is the older fix log.

HISTORY. Round 1 found 1 P0 / 8 P1 / 20 P2. Round 2 closed them but stopped at each bug's boundary and did not defend its own fixes with tests. Round 3 audited the hardened code and found 2 P0 + 8 P1 despite ALL GATES BEING GREEN. Round 3's fix round (HEAD = commit 4edbbe6) closed both P0s and 6 P1s, each with a test verified to fail when the fix is deleted:
 - P0-1 runner.py: heartbeater.stop() moved to the OUTER finally so the lease survives /parcel_update + /integrate.
 - P0-2 adapter.py/leases.py: hook lease path passes ensure_parcel=True, auto-creating a coarse whole-file parcel for unindexed files; transient blackboard failure still fails open.
 - P1-1 leases.py: heartbeat gained the "AND ttl_expires_at > :now" predicate.
 - P1-2 git_ops.py: _reject_unsafe_name allow-list + a containment assert before shutil.rmtree.
 - P1-4 integrator.py: SWARMSYNC_GATE_TIMEOUT (default 600s) + process-group kill on the pytest gate.
 - P1-5 integrator.py: _reject_and_reset re-indexes from the restored trunk so the blackboard rolls back with git.
 - P1-6 runner.py: delete_branch only on 'merged'.
 - P1-10 README/serve.py: SWARMSYNC_ROOTS documented, --root flag, managed roots printed at boot; trust-model section added; two overclaiming README guarantee rows rewritten.

CURRENT GATES (verified): ruff clean, mypy clean, 274 tests green (3x, no flakes), 95% coverage, demo 5/5 standalone.

KNOWN-OPEN, deliberately deferred -- report these ONLY if you have something NEW and specific (a sharper mechanism, a worse consequence, a concrete design resolution). Do not re-report them as discoveries:
 - P1-3 Bash-mediated edits bypass the lease gate (EDIT_TOOLS covers Edit/Write/MultiEdit/NotebookEdit only).
 - P1-7 span-disjointness (co_schedulable symbol mode) is not git-merge safety; git merges line hunks with 3 lines of context.
 - P1-8 the frozen-contract subsystem is inert at the DEFAULT file granularity (frozen_ids never match <file>::<module>).
 - P1-9 renames/deletes emit no contract_change and leave ghost contract rows served as truth.
 - The completeness critic's symlink escape: _validate_managed_path realpaths only the ROOT, then rglob follows leaf symlinks.
 - AUDIT_R3.md section 4 lists findings already investigated and DISMISSED. Do not re-litigate them.

YOUR JOB: find what Rounds 1-3 all MISSED, and what Round 3's fixes BROKE. A fix that looks right but has a hole is the most valuable thing you can find. Read the actual code; never trust a doc's claim about the code.

SEVERITY: P0 = data loss / corruption / broken core guarantee / security hole reachable in normal use. P1 = serious bug, wrong behavior under realistic conditions, or a guarantee that holds only by luck. P2 = quality/maintainability/polish.
Judge security against the REAL trust model (localhost dev tool, semi-trusted agents, the gate runs repo code by design) -- say so if a finding is out of scope under it.
`

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    files_opened: { type: 'array', items: { type: 'string' }, description: 'Every source file you actually READ this run' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          severity: { type: 'string', enum: ['P0', 'P1', 'P2'] },
          file: { type: 'string' },
          line: { type: 'number' },
          claim: { type: 'string' },
          failure_scenario: { type: 'string', description: 'Concrete inputs/interleaving -> wrong outcome' },
          evidence: { type: 'string', description: 'Code quoted, or command/test output you actually ran' },
        },
        required: ['title', 'severity', 'file', 'claim', 'failure_scenario', 'evidence'],
      },
    },
  },
  required: ['findings', 'files_opened'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    refuted: { type: 'boolean' },
    confidence: { type: 'string', enum: ['low', 'medium', 'high'] },
    reasoning: { type: 'string' },
    corrected_severity: { type: 'string', enum: ['P0', 'P1', 'P2', 'none'] },
  },
  required: ['refuted', 'confidence', 'reasoning', 'corrected_severity'],
}

// ROUND4.md item 2: scope by ARCHITECTURE, not by the diff. R3 was diff-shaped --
// every finding landed on a file Round 2 touched. These files were never opened:
const UNEXAMINED = '`blackboard/db.py`, `blackboard/schema.sql`, `classifier/store.py`, `classifier/indexer.py`, `server/events.py`, `agent/client.py`, `agent/mutators.py`, `server/serve.py`, `scripts/swarmsync-hook-guard`, `demo/`'

const DIMENSIONS = [
  {
    key: 'mutation',
    prompt: `You are running MUTATION TESTING -- ROUND4.md calls this the single highest-leverage check available. The rule: "a fix without a test that fails when you delete the fix is not a fix, it is a hope."

METHOD: copy the repo to an absolute mktemp -d dir and work ONLY in the copy (\`cp -a /home/keith/projects/swarm-sync /tmp/xxx/repo\`). For each safety mechanism below: delete or neuter it in the copy, run the FULL suite (\`python -m pytest -q\`), and record whether the suite goes red. A mechanism whose deletion leaves the suite GREEN is an undefended fix -- report it as a finding (P1 if the mechanism guards a P0/P1-class defect, else P2).

Round 3 verified ITS OWN eight fixes this way, so those should all be caught -- SPOT-CHECK two of them to confirm the harness works, then spend your time on everything else:
- The reaper's atomic \`UPDATE ... RETURNING\` (coordinator/reaper.py) -- revert it to SELECT-then-UPDATE.
- The events \`seq\` via RETURNING (server/events.py).
- The classifier's per-file parse guard (classifier/graph.py).
- Token auth: delete \`dependencies=[Depends(require_token)]\` from EACH of the 7 mutating routes in server/app.py, one at a time. R3 found 3 of 7 were undefended -- confirm whether that is still true.
- The managed-roots check (\`_validate_managed_path\`) in server/app.py.
- The pytest gate's sandbox env flags (PYTEST_DISABLE_PLUGIN_AUTOLOAD etc) in coordinator/integrator.py.
- \`acquire\`'s conflict predicate: try weakening \`AND l.ttl_expires_at > :now\`, and the mode clause.
- \`release\`/\`heartbeat\` ownership scoping (\`AND agent_id = :agent_id\`).
- The integrator's conflict/misprediction rejection path.
- Anything else you judge load-bearing.

Also try SEMANTIC mutations, not just deletions: off-by-one a comparison (\`>\` -> \`>=\`), invert a boolean, swap an ordering. A suite that can't tell \`>\` from \`>=\` on a TTL predicate is not testing the predicate.

Report each undefended mechanism with: what you deleted, the exact suite result (e.g. "274 passed"), and what the missing test should assert. Be precise and mechanical; this dimension's value is in its completeness, not its prose.`,
  },
  {
    key: 'unexamined',
    prompt: `You are auditing the modules EVERY prior round skipped. R3's completeness critic found the whole audit was "diff-shaped, not codebase-shaped": every confirmed finding landed on a file Round 2 had touched, inheriting Round 1's blind spots exactly.

These files have NEVER been opened by any audit dimension: ${UNEXAMINED}

Its own 10-minute spot-check of that territory immediately found a P1 (leaf-symlink escape) and two P2s (lost update in decay_pheromone, zero schema versioning) -- so there is more there. Go through them properly:
- \`blackboard/db.py\` + \`schema.sql\`: connection config, WAL, busy_timeout, foreign_keys, the transaction() contextmanager's nesting refusal, isolation_level=None semantics. Is the schema right? Indices? Constraints that should exist and don't?
- \`classifier/store.py\` + \`indexer.py\`: run_index's whole-repo re-parse, upsert semantics, the monotonic contracts.version bump, "no stale-row pruning", hashing (what does content_hash actually cover? R3 learned a comment-only edit does NOT change it -- what else is invisible to it?).
- \`server/events.py\`: emit/tail/seq, decay_pheromone, drop_pheromone.
- \`agent/client.py\`: error handling, raise_for_status vs not, timeouts (is there ANY http timeout? what happens when the server hangs?).
- \`agent/mutators.py\`, \`server/serve.py\`, \`scripts/swarmsync-hook-guard\`, \`demo/\`.

Report real defects with concrete triggers. Do not pad with style nits.`,
  },
  {
    key: 'regressions',
    prompt: `You are reviewing Round 3's fix commit as a hostile PR reviewer. \`git show 4edbbe6\` (and \`git log -p -1\`). Round 2's history is the warning: its worktree-cleanup "fix" REGRESSED the system by deleting rejected agents' branches, and its asyncio.Lock fix created a new P1 by making the gate a global chokepoint with no timeout.

So: what did Round 3 break, and where are its fixes' holes? Attack each specifically:
- \`_ensure_parcel\` (server/leases.py): it INSERTs a parcel row inside acquire(). Is that safe under concurrency? What if a real classifier parcel for that id appears later -- does the coarse 'file' row collide, shadow it, or get overwritten? Does INSERT OR IGNORE inside acquire break acquire's single-statement atomicity claim (it is now TWO statements -- what happens if they interleave)? Can a caller smuggle a junk parcel_id and pollute the parcels table? Does the hook now create parcels for files it shouldn't (.git/, node_modules/, binary files, huge files)?
- The heartbeat TTL predicate (leases.py): does anything now RELY on reviving an expired lease that silently broke? Trace every heartbeat caller, including the hook keepalive (_keepalive in adapter.py) -- what happens when a hook agent's think time exceeds the TTL and its keepalive now legitimately fails? Is THAT a new user-visible failure?
- The gate timeout + process-group kill (integrator.py): start_new_session=True changes signal/process semantics -- does it break pytest capture, or the demo, or CI? Is 600s sane? Does communicate() after killpg ever hang? Does the timeout path leave a merge on trunk (it returns False -> the caller rejects -> reset_hard, but VERIFY that end-to-end)?
- The re-index in _reject_and_reset (integrator.py): it now runs run_index on the ERROR path, inside the global lock. Cost? Can it raise (it catches Exception -- is rollback_error surfaced correctly)? Does it interact with the monotonic contracts.version bump -- does a rejected merge now leave a permanent version gap? Does re-indexing the restored trunk ever produce DIFFERENT rows than were there before the merge?
- delete_branch=landed (runner.py): does anything leak now that branches survive rejection -- unbounded branch accumulation across runs? Does the rerun-idempotency test still hold if a prior REJECTED run left the branch?
- \`_reject_unsafe_name\` (git_ops.py): is the allow-list too strict for real agent ids the broker generates? Check what broker/demo actually pass. Does remove_worktree's new check=False hide real failures?

Reproduce anything you claim.`,
  },
  {
    key: 'concurrency',
    prompt: `You are a concurrency and distributed-systems expert. Focus on the lease fabric's safety under real interleavings, INCLUDING the surfaces R3 fixed (verify the fixes are actually airtight, not just better).

- The "two predicates that must agree" class (ROUND4.md item 1): \`acquire\` vs \`heartbeat\` vs \`reap_once\` vs \`adapter._find_lease\` vs \`release\`. R3 fixed heartbeat's missing TTL clause. Are the four now genuinely consistent? Diff them line by line. Any OTHER pair that can disagree? (\`_find_lease\` returning the FIRST matching row was noted in R3 -- is it still reachable?)
- Can two agents still ever hold overlapping parcels? Try hard, with real threads against a real DB. Include the hook path (shared working tree, no worktree isolation).
- \`_ensure_parcel\` made acquire TWO statements. Prove or refute that concurrent acquires on a new parcel id are still correct.
- The integrator's global asyncio.Lock: multi-worker/multi-process deployment (uvicorn --workers N) defeats an in-process lock entirely. Is that reachable/documented? What actually happens?
- Reaper: it runs blocking SQLite on the asyncio event-loop thread with busy_timeout=5000, and its loop body has no try/except (R3 rated this P2). Under load does a contended write block the whole ASGI server, and does the reaper die permanently? If the reaper is dead, which invariants degrade -- and does anything notice?
- Crash-consistency: SIGKILL between reap_once's UPDATE and its emit; between run_index's COMMIT and the merged event; during the gate.
Write throwaway stress scripts in an absolute mktemp -d dir to prove races. Measured evidence only.`,
  },
  {
    key: 'security',
    prompt: `You are a security engineer. Trust model: localhost dev tool, semi-trusted agents, and the gate runs repo code BY DESIGN (README now says so explicitly). Findings must be judged against that -- but the model has edges worth probing.

- The leaf-symlink escape in \`_validate_managed_path\` is KNOWN (realpaths only the root; rglob follows leaf symlinks). Do NOT just re-report it: determine its real blast radius now, and whether the same pattern (validate the root, then walk) recurs anywhere else -- integrator's repo path, worktree ops, the hook's cwd handling.
- Token auth: is it enforced on every mutating route TODAY? Timing-safe? R3 noted a non-ASCII SWARMSYNC_TOKEN raises TypeError -> 500. Read routes (/parcels, /leases, /events, /contract) are unauthenticated and leak code structure -- is that stated honestly now that README has a trust-model section?
- \`_ensure_parcel\` is NEW attack surface: an unauthenticated (default) POST /lease with ensure_parcel=true now WRITES a row to the parcels table with a caller-controlled id. Unbounded? Poisonable? Does it let a caller shadow/lock a real parcel, or DoS via table growth? Can parcel_id contain path traversal, NUL, or absurd length?
- \`SWARMSYNC_GATE_TIMEOUT\` is read from the env of a process whose env the gate's own subprocess inherits (env = {**os.environ, ...}) -- and that env still carries SWARMSYNC_TOKEN into agent-authored test code. Round 3 documented the gate runs repo code; is handing it the auth token consistent with that?
- The hook guard (scripts/swarmsync-hook-guard) and \`_is_active\`'s marker file: still bypassable/deletable by the agents it constrains?
- git argument handling across ALL of git_ops after R3's changes.`,
  },
  {
    key: 'robustness',
    prompt: `You are a reliability engineer hunting failure modes and blackboard-vs-git divergence (the highest-value class: any operation that leaves the DB and git state disagreeing).

- R3 fixed the rejected-merge drift by re-indexing in _reject_and_reset. Find the OTHER divergences: runner.py posts /parcel_update BEFORE integrate (so a rejected merge leaves the agent's self-reported hash?), the monotonic contracts.version bump, the crash-during-integrate window (SIGKILL/OOM/BaseException -- except Exception misses KeyboardInterrupt/SystemExit, e.g. Ctrl-C or uvicorn shutdown during the gate). Is there ANY startup reconciliation? What does a restart resume onto?
- Resource exhaustion (nobody has modeled this): the events table is append-only with no pruning/rotation/retention and every heartbeat writes a row -- quantify the growth for a realistic session. Branches now survive rejection (R3's P1-6 fix) -- do they accumulate unboundedly? Worktrees? WAL growth/checkpointing?
- Upgrade/migration: there is NO schema versioning (no PRAGMA user_version, no migration code) against a persistent DB at a stable default path. What happens to an existing DB when the schema changes? R3 rated this P2 -- test what actually happens and judge for yourself.
- Kill things: agent dies holding a lease; server dies mid-integrate; reaper dies; SQLite locked/corrupted; disk full; a worktree dir deleted out from under the system.
- Error handling quality: bare excepts, swallowed errors, leases held forever, retries without backoff, missing http timeouts in agent/client.py.
Induce the failures where you can, inside an absolute mktemp -d workspace.`,
  },
  {
    key: 'architecture',
    prompt: `You are a staff architect. ROUND4.md's central claim is that P1-7/P1-8/P1-9 are ONE question wearing three hats: **is the parcel/contract abstraction load-bearing, or decoration?** Today the DEFAULT mode (file granularity) is the one where contracts do nothing, and the mode where contracts work (symbol) is the unsafe one (span disjointness is not git-merge safety). Your job is to RESOLVE that question with a concrete recommendation, not to restate it.

Evaluate the three candidate resolutions ROUND4.md names, with evidence:
 (a) require a >=3-line gap between co-scheduled spans, making symbol mode true of git's merge unit -- what does that cost in practice? How often would real code have co-schedulable spans at all? Measure against a real repo (this one).
 (b) implement the rebase-and-resubmit path DESIGN 5.5 already promises (NOTE: \`grep -rn rebase --include=*.py\` finds NO implementation -- only status literals and docstrings). What would it take? Is it the honest fix?
 (c) map frozen_ids up to their owning <file>::<module> parcel in file mode so the exclusive upgrade and co_schedulable's frozen clause actually fire; and give 'exclusive' a distinct meaning in acquire (it is currently indistinguishable from 'write') or delete the mode.
Recommend a combination and say what DESIGN.md/README must then say.

Also assess honestly: does the system deliver on its own thesis? The serial integrator re-indexes the WHOLE repo per merge (O(repo), not O(change)) inside the global lock, alongside pytest -- is that a throughput cliff exactly at the scale where the machinery earns its keep? Is there a realistic multi-agent scenario this architecture simply cannot handle correctly? That last one is the most valuable finding available to you.`,
  },
  {
    key: 'docs',
    prompt: `You are auditing the docs against the CODE (README.md, DESIGN.md, ROUND4.md, AUDIT_R3.md, HARDENING.md, HANDOFF.md, schema.sql headers, module docstrings). An overclaimed guarantee is P1, not P2: it makes users trust something that isn't there.

Round 3 just rewrote README's guarantees table and added a trust-model section, and changed behavior in serve.py (--root, boot line) and integrator.py (SWARMSYNC_GATE_TIMEOUT). So:
- Verify the NEW README claims are actually true of the code -- especially the rewritten guarantee rows, the security section (is SWARMSYNC_TOKEN really enforced on exactly the 7 routes it names? is 600s really the default? is the Bash-bypass sentence accurate?), and the SWARMSYNC_ROOTS section.
- Go claim by claim through DESIGN.md. Known: 5.5's "forced rebase" has no implementation; 3's "incremental per-changed-file re-index" is a whole-repo re-parse; 3 step 6's Territories are never computed (Parcel.territory is always None). CONFIRM these still stand and find the rest. Does DESIGN mention the hook path's shared working tree (which voids 5.1's isolation there)? Does it document ensure_parcel / coarse 'file' parcels now that they exist?
- Module docstrings that lie about their own code: R3 found leases.py's docstring promised an expired-heartbeat no-op the code didn't implement (now fixed). Sweep for others -- especially any docstring R3's changes made stale (runner.py's _Heartbeater, git_ops' _reject_option_like, integrator's atomicity contract, adapter's fail-open note, store.py, schema.sql's header which HARDENING claims was corrected but was only fixed in DESIGN.md).
- README quickstart: FOLLOW it verbatim as a stranger in an absolute mktemp -d clone. Known: \`python\` doesn't exist on a stock box (only python3); the requires-python >=3.11 floor is undocumented. Does the NEW SWARMSYNC_ROOTS guidance actually work as written? Does swarmsync-serve --root behave as documented?
Quote the doc and the contradicting code for each.`,
  },
]

phase('Audit')
const results = await pipeline(
  DIMENSIONS,
  d => agent(`${CONTEXT}\n${SAFETY}\n\nYOUR DIMENSION: ${d.key}\n\n${d.prompt}\n\nReturn every defect you can substantiate with evidence you actually gathered. Quality over quantity: one substantiated P1 beats ten speculative P2s. Also return \`files_opened\`: every source file you genuinely READ -- this is checked against the codebase to detect blind spots, so be truthful.`,
    { label: `audit:${d.key}`, phase: 'Audit', schema: FINDINGS_SCHEMA, effort: 'high' }),
  (res, d) => {
    if (!res || !res.findings?.length) { log(`${d.key}: no findings`); return [] }
    const serious = res.findings.filter(f => f.severity === 'P0' || f.severity === 'P1')
    const minor = res.findings.filter(f => f.severity === 'P2').map(f => ({ ...f, dimension: d.key, verdict: null }))
    log(`${d.key}: ${res.findings.length} findings (${serious.length} serious), opened ${res.files_opened?.length ?? 0} files`)
    return parallel(serious.map(f => () =>
      parallel(['correctness', 'reachability', 'already-handled'].map(lens => () =>
        agent(`${CONTEXT}\n${SAFETY}\n\nYou are an adversarial verifier. Your job is to REFUTE this finding. Default to refuted=true unless the evidence is solid. A plausible-but-wrong finding sends the next round chasing ghosts -- that is the cost you exist to prevent.\n\nFINDING (${f.severity}) from the ${d.key} audit:\nTitle: ${f.title}\nFile: ${f.file}${f.line ? ':' + f.line : ''}\nClaim: ${f.claim}\nFailure scenario: ${f.failure_scenario}\nEvidence offered: ${f.evidence}\n\nYOUR LENS: ${lens}.\n- correctness: is the claim technically right about what the code ACTUALLY does? Read it. Re-run their evidence.\n- reachability: can this happen in normal operation given the real trust model and usage? A theoretical race a lock upstream prevents is refuted. A "vulnerability" that requires what the trust model already grants is refuted.\n- already-handled: does another layer (a guard, a check, a transaction, a test, one of Round 3's fixes) already prevent it?\n\nReproduce it if you can -- a finding you can trigger is confirmed; one you cannot is suspect. Judge severity honestly: set corrected_severity to what it really is (findings are routinely inflated).`,
          { label: `verify:${f.severity}:${lens}`, phase: 'Verify', schema: VERDICT_SCHEMA, effort: 'high' })))
        .then(votes => {
          const v = votes.filter(Boolean)
          const refuted = v.filter(x => x.refuted).length
          const sevs = v.map(x => x.corrected_severity).filter(s => s !== 'none')
          return { ...f, dimension: d.key, votes: v, survives: refuted < 2,
                   final_severity: sevs.sort()[0] || f.severity }
        })
    )).then(verified => [...verified.filter(Boolean), ...minor])
  }
)

const all = results.flat().filter(Boolean)
const confirmed = all.filter(f => f.survives === true)
const refuted = all.filter(f => f.survives === false)
const p2s = all.filter(f => f.verdict === null)
log(`Verification done: ${confirmed.length} serious confirmed, ${refuted.length} refuted, ${p2s.length} P2s`)

phase('Synthesize')
const [report, critic] = await parallel([
  () => agent(`${CONTEXT}\n${SAFETY}\n\nYou are synthesizing the Round 4 audit report for swarm-sync.

CONFIRMED SERIOUS FINDINGS (survived 3-lens adversarial verification):
${JSON.stringify(confirmed.map(f => ({ dimension: f.dimension, title: f.title, severity: f.final_severity, file: f.file, line: f.line, claim: f.claim, scenario: f.failure_scenario, evidence: f.evidence })), null, 2)}

REFUTED (majority refuted -- list briefly under "considered and dismissed" with the reason, so Round 5 doesn't re-litigate):
${JSON.stringify(refuted.map(f => ({ title: f.title, severity: f.severity, why_refuted: f.votes?.filter(v => v.refuted).map(v => v.reasoning).join(' | ').slice(0, 400) })), null, 2)}

P2 FINDINGS (unverified; dedupe and merge near-duplicates):
${JSON.stringify(p2s.map(f => ({ dimension: f.dimension, title: f.title, file: f.file, claim: f.claim })), null, 2)}

Gates measured independently this round: ruff clean, mypy clean, 274 tests green (3x, no flakes), 95% coverage, demo 5/5 standalone. HEAD = 4edbbe6.

Write a prioritized markdown report:
1. **Verdict** -- does the code meet the "proud to ship" bar? Bar: ruff+mypy clean; suite green, MEANINGFUL coverage, no flakes; concurrency invariants hold under real stress; demo money shots pass standalone; sound error handling; NO confirmed P0/P1; DESIGN.md matches the code; README usable by a stranger. State plainly whether it passes and exactly what blocks it. A table is fine.
2. **Confirmed P0/P1** -- each with file:line, failure scenario, and a concrete recommended fix.
3. **Mutation results** -- which safety mechanisms are NOT defended by the suite. This is its own section because it is the round's highest-leverage output: name each undefended mechanism and the test that should exist.
4. **The architecture question** -- the architecture dimension's recommendation on whether the parcel/contract abstraction is load-bearing or decoration, and which resolution to take (with what DESIGN/README must then say).
5. **P2 backlog** -- deduped, grouped by theme, one line each.
6. **Considered and dismissed** -- refuted findings + why.
7. **Round 3 vs Round 4 delta** -- did Round 3's fixes hold? Did any of them REGRESS anything? Was the "fix the class, not the bug" instruction actually followed, or did Round 3 stop at each bug's boundary the way Round 2 did? Be direct.

Dedupe aggressively: the same defect found by several dimensions is ONE finding. Be honest and specific; do not pad; do not invent. If the code is genuinely in good shape, say so plainly -- an audit that manufactures findings to look thorough is worse than useless. Return ONLY the markdown.`,
    { label: 'synthesize:report', phase: 'Synthesize', effort: 'high' }),
  () => agent(`${CONTEXT}\n${SAFETY}\n\nYou are the completeness critic for Round 4. Round 3's critic caught that the R3 audit was "diff-shaped, not codebase-shaped" -- every finding landed on a file Round 2 touched. Round 4 explicitly added an 'unexamined' dimension and a 'mutation' dimension to fix that. Your job: did it work, and what is STILL missing?

Files each dimension reported opening:
${JSON.stringify(results.flat().filter(Boolean).length ? 'see per-dimension files_opened in the journal' : 'none', null, 2)}

Confirmed findings:
${JSON.stringify(confirmed.map(f => ({ dimension: f.dimension, title: f.title, severity: f.final_severity, file: f.file })), null, 2)}

Assess:
- Enumerate the actual source files in the repo (\`find /home/keith/projects/swarm-sync/swarmsync /home/keith/projects/swarm-sync/scripts /home/keith/projects/swarm-sync/demo -type f\`) and compare against where findings landed. Is coverage now codebase-shaped, or did it drift back to the diff again?
- Is any confirmed finding unverified by execution -- i.e. asserted from reading alone?
- Which failure modes STILL went unmodeled (operational, upgrade/migration, multi-user, resource exhaustion, human error)?
- Did the mutation dimension actually run mutations, or just describe them? That distinction matters.
- Do your OWN quick spot-check of whatever looks least examined and report anything real.
Return concise markdown. If coverage was genuinely adequate this time, say so plainly rather than inventing gaps -- that is a legitimate and useful answer.`,
    { label: 'critic:completeness', phase: 'Synthesize', effort: 'high' }),
])

return { report, critic, counts: { confirmed: confirmed.length, refuted: refuted.length, p2: p2s.length } }
