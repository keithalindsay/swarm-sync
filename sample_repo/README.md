# sample_repo

A small, self-contained set of Python modules that swarm-sync's agents edit concurrently in
the demo. Built in Unit U13. Not a package (no `__init__.py` at this level, deliberately —
`calc`/`formats`/`api` import each other as flat top-level modules); `tests/conftest.py`
puts this directory on `sys.path` so both `pytest sample_repo/tests` (this unit's own
done-when) and the integrator's `pytest tests` run from inside a git worktree checkout of
this same tree (`swarmsync/coordinator/integrator.py::run_impact_tests`, cwd=repo) resolve
those imports the same way.

Shape:
  calc.py      — add/sub/mul/div: four independent top-level functions, none call each
                 other. add/sub (or mul/div) is money-shot #1's target: two agents editing
                 two different functions in the SAME file, concurrently, at symbol
                 granularity.
  formats.py   — money(), percent(), total_with_tax(): depend on calc.add/mul/div (named
                 imports).
  api.py       — summarize(), apply_discount(), report(): the public surface, importing
                 both calc (module-level, attribute calls) and formats (named). Between
                 formats.py, api.py, and their own tests all calling it, calc.py::add's
                 blast_radius clears FREEZE_THRESHOLD (3) — it's sample_repo's
                 frozen-contract candidate (money-shot #3, DESIGN §3 step 5): api.py's/
                 formats.py's call sites are what a dependent agent re-plans if add's
                 signature ever changes.
  tests/       — pytest covering each module; this is the integrator's merge gate for
                 money-shots #1 and #5 (a deliberately test-breaking edit here must make
                 the gate reject).

Verified in `tests/test_sample_repo.py` (project root): >=3 modules with a real
import/call graph, calc.py's two-independent-functions property, >=1 frozen contract with
blast_radius >= 3, and `pytest sample_repo/tests` green.
