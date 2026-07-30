"""Broker-path tests at realistic scale (a real ~10k-line repo, not `sample_repo`).

Every guarantee in the README is otherwise evidenced at 96 lines / 3 modules.
These tests drive the SAME broker/integrator path against a clone of a real
repository (`code-learner`: 34 source modules, 11 test files, 252 tests, 567
parcels, 86 frozen contracts) to find out whether the guarantees still hold when
the numbers are real.

`harness.py` owns all of the expensive, shared apparatus (clone, blackboard,
index, task construction, teardown) and is the module the per-hypothesis test
files import. See its docstring for the interface.
"""
