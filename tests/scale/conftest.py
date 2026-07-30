"""Skip the whole scale package, with a reason, when its external fixture is absent.

These tests clone a real multi-module Python repository (see `harness.py` on why one
cannot be synthesized), so they are the only tests in swarm-sync that need something
this repo does not ship. Before this file existed, a fresh clone following the README's
`pytest tests/scale/` got 32 errors out of a missing absolute path in another
developer's home directory -- an environment gap presented as 32 failures.

Skipped at COLLECTION, not inside each fixture, for two reasons: pytest reports one
reason per test instead of a stack trace per test, and `-x` does not stop the run on
what is not a failure. The check itself is cheap (three `stat`s) and does no I/O
against the fixture.
"""
from __future__ import annotations

import pytest

from tests.scale import harness


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    reason = harness.fixture_unavailable()
    if reason is None:
        return
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        item.add_marker(skip)
