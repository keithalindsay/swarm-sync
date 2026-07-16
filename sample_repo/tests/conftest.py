"""Make sample_repo/ (this conftest's parent directory) importable as bare
`calc`/`formats`/`api` regardless of the cwd/invocation pytest is run from --
`pytest sample_repo/tests` from the swarm-sync project root (this unit's own
done-when), `pytest tests` with `cwd=sample_repo` (the integrator's own test
gate, `swarmsync/coordinator/integrator.py::run_impact_tests`), or a plain
`pytest` run from inside sample_repo/ itself.

sample_repo has no `__init__.py` (it's a flat set of top-level modules on
purpose -- see its own README), so without this, `from calc import add` only
resolves when the cwd happens to already be on `sys.path`. Anchoring on
`__file__` instead of cwd makes every one of the above invocations agree.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SAMPLE_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_SAMPLE_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_SAMPLE_REPO_ROOT))
