"""Shared pytest configuration for the swarm-sync suite.

S3 security introduced a managed-root allow-list on `POST /index` (`root`) and
`POST /integrate` (`repo`): a caller-supplied filesystem path must realpath to
somewhere under `SWARMSYNC_ROOTS` (default: the server's launch cwd). The whole
suite drives those endpoints against repos it builds under pytest's temp dir
(and the demo builds its copy under the system temp dir), neither of which is
the repo-root cwd pytest runs from. So, autouse, point the allow-list at the
system temp root for every test -- exactly the legitimate operator move a real
deployment makes (set SWARMSYNC_ROOTS to the directories it may touch). Tests
that specifically exercise the allow-list REJECT path override this with their
own narrower `SWARMSYNC_ROOTS` via `monkeypatch.setenv` (later setenv wins).
"""
from __future__ import annotations

import tempfile

import pytest


@pytest.fixture(autouse=True)
def _managed_roots_for_tests(monkeypatch):
    # gettempdir() is the common ancestor of both pytest's tmp_path tree and the
    # demo's own tempfile.mkdtemp() workdir, so this one setting covers every
    # endpoint-driven index/integrate the suite performs (including the demo run
    # as a child process, which inherits this env).
    monkeypatch.setenv("SWARMSYNC_ROOTS", tempfile.gettempdir())
    yield
