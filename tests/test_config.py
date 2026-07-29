"""`swarmsync.config` -- the one home for the env-var surface (WP4.2, A4/U9).

Every accessor must:
  * read the environment AT CALL TIME (the suite monkeypatches env constantly);
  * apply exactly the parse/fallback semantics its pre-WP4.2 point-of-use copy
    had: unset/garbage values fall back to the documented default, never raise;
  * honor `SWARM_SYNC_DB` as a deprecated alias of `SWARMSYNC_DB` with a
    one-line stderr warning (`db_path`).

`tests/test_architecture.py::test_only_config_reads_the_environment` is the
companion guard proving nothing else under `swarmsync/` reads the env.
"""
from __future__ import annotations

import os

import pytest

from swarmsync import config
import contextlib

@contextlib.contextmanager
def mock_environ(values):
    """Swap os.environ wholesale, then restore. Lets a test assert on what
    `subprocess_env` REMOVES without leaking vars into the rest of the run."""
    original = dict(os.environ)
    os.environ.clear()
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)



@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every knob unset unless the test sets it (conftest sets SWARMSYNC_ROOTS)."""
    for var in (
        config.TOKEN_ENV,
        config.ROOTS_ENV,
        config.GATE_TIMEOUT_ENV,
        config.LEASE_TTL_ENV,
        config.ACTIVE_ENV,
        config.URL_ENV,
        config.DB_ENV,
        config.DB_ENV_DEPRECATED,
        config.MAX_LEASES_PER_AGENT_ENV,
        config.MAX_BODY_BYTES_ENV,
        config.EVENTS_COMPACT_INTERVAL_ENV,
        config.EVENTS_HEARTBEAT_MAX_AGE_ENV,
        config.EVENTS_MAX_AGE_ENV,
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# --- reads happen at call time, never cached at import ------------------------------


def test_accessors_read_env_at_call_time(monkeypatch):
    """The same accessor must observe a monkeypatched change between two calls --
    an import-time cache would freeze the first value and break half the suite."""
    assert config.gate_timeout() == config.DEFAULT_GATE_TIMEOUT_SECONDS
    monkeypatch.setenv(config.GATE_TIMEOUT_ENV, "12.5")
    assert config.gate_timeout() == 12.5
    monkeypatch.setenv(config.GATE_TIMEOUT_ENV, "99")
    assert config.gate_timeout() == 99.0


# --- token / url / active: raw string knobs ----------------------------------------


def test_token_defaults_to_none_and_returns_raw_value(monkeypatch):
    assert config.token() is None
    monkeypatch.setenv(config.TOKEN_ENV, "s3cr3t")
    assert config.token() == "s3cr3t"


def test_url_default_matches_the_one_launcher_port(monkeypatch):
    assert config.url() == "http://127.0.0.1:8787" == config.DEFAULT_URL
    monkeypatch.setenv(config.URL_ENV, "http://10.0.0.2:9999")
    assert config.url() == "http://10.0.0.2:9999"


def test_active_only_on_exactly_1(monkeypatch):
    assert config.active() is False
    for wrong in ("0", "true", "yes", ""):
        monkeypatch.setenv(config.ACTIVE_ENV, wrong)
        assert config.active() is False, wrong
    monkeypatch.setenv(config.ACTIVE_ENV, "1")
    assert config.active() is True


def test_lease_ttl_is_deliberately_raw(monkeypatch):
    """The adapter's `_hook_lease_ttl` owns parse/clamp/warn (C9/C13); config
    hands it the raw string, or None when unset."""
    assert config.lease_ttl() is None
    monkeypatch.setenv(config.LEASE_TTL_ENV, "not-a-number")
    assert config.lease_ttl() == "not-a-number"
    monkeypatch.setenv(config.LEASE_TTL_ENV, "120")
    assert config.lease_ttl() == "120"


# --- gate_timeout: the unified pair ------------------------------------------------


def test_gate_timeout_default_and_valid_override(monkeypatch):
    assert config.gate_timeout() == 600.0
    monkeypatch.setenv(config.GATE_TIMEOUT_ENV, "900")
    assert config.gate_timeout() == 900.0


@pytest.mark.parametrize("junk", ["", "abc", "12.5.7", "0", "-3", "nan"])
def test_gate_timeout_garbage_and_non_positive_fall_back(monkeypatch, junk):
    """The exact input classes both pre-WP4.2 copies (gate.py / client.py) sent
    to the default -- the unification must preserve every one of them."""
    monkeypatch.setenv(config.GATE_TIMEOUT_ENV, junk)
    assert config.gate_timeout() == config.DEFAULT_GATE_TIMEOUT_SECONDS


# --- roots + set_roots -------------------------------------------------------------


def test_roots_default_is_realpathed_cwd(monkeypatch, tmp_path):
    real = tmp_path.resolve()
    monkeypatch.chdir(real)
    assert config.roots() == [os.path.realpath(str(real))]


def test_roots_splits_on_pathsep_drops_empties_and_realpaths(monkeypatch, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    link = tmp_path / "link"
    link.symlink_to(a)
    monkeypatch.setenv(
        config.ROOTS_ENV, os.pathsep.join([str(link), "", str(tmp_path / "b")])
    )
    got = config.roots()
    assert got[0] == os.path.realpath(str(a)), "symlinked root must canonicalize"
    assert got[1] == os.path.realpath(str(tmp_path / "b"))
    assert len(got) == 2, "empty entries must be dropped"


def test_set_roots_is_the_one_env_write(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ROOTS_ENV, "placeholder")  # ensures restore on teardown
    config.set_roots(str(tmp_path))
    assert os.environ[config.ROOTS_ENV] == str(tmp_path)
    assert config.roots() == [os.path.realpath(str(tmp_path))]


# --- db_path: SWARMSYNC_DB + the deprecated SWARM_SYNC_DB alias --------------------


def test_db_path_default_is_the_unified_launcher_default(capsys):
    assert config.db_path() == "swarmsync.db" == config.DEFAULT_DB_PATH
    assert capsys.readouterr().err == ""  # no warning on the clean path


def test_db_path_prefers_swarmsync_db(monkeypatch, capsys):
    monkeypatch.setenv(config.DB_ENV, "/tmp/new.db")
    assert config.db_path() == "/tmp/new.db"
    assert capsys.readouterr().err == ""


def test_db_path_honors_deprecated_alias_with_stderr_warning(monkeypatch, capsys):
    monkeypatch.setenv(config.DB_ENV_DEPRECATED, "/tmp/legacy.db")
    assert config.db_path() == "/tmp/legacy.db"
    err = capsys.readouterr().err
    assert err.count("\n") == 1, "the deprecation warning is ONE line"
    assert "SWARM_SYNC_DB" in err and "deprecated" in err and "SWARMSYNC_DB" in err


def test_db_path_new_name_beats_deprecated_alias_silently(monkeypatch, capsys):
    monkeypatch.setenv(config.DB_ENV, "/tmp/new.db")
    monkeypatch.setenv(config.DB_ENV_DEPRECATED, "/tmp/legacy.db")
    assert config.db_path() == "/tmp/new.db"
    assert capsys.readouterr().err == "", "no nagging when the new name is in use"


# --- int knobs: max_leases_per_agent / max_body_bytes ------------------------------


def test_max_leases_per_agent_default_garbage_and_valid(monkeypatch):
    assert config.max_leases_per_agent() == 256
    monkeypatch.setenv(config.MAX_LEASES_PER_AGENT_ENV, "not-an-int")
    assert config.max_leases_per_agent() == config.DEFAULT_MAX_LEASES_PER_AGENT
    monkeypatch.setenv(config.MAX_LEASES_PER_AGENT_ENV, "1024")
    assert config.max_leases_per_agent() == 1024


def test_max_body_bytes_default_garbage_and_valid(monkeypatch):
    assert config.max_body_bytes() == 10 * 1024 * 1024
    monkeypatch.setenv(config.MAX_BODY_BYTES_ENV, "ten megs")
    assert config.max_body_bytes() == config.DEFAULT_MAX_BODY_BYTES
    monkeypatch.setenv(config.MAX_BODY_BYTES_ENV, "1024")
    assert config.max_body_bytes() == 1024


# --- positive-float knobs: the events trio -----------------------------------------


@pytest.mark.parametrize(
    ("env_var", "accessor", "default"),
    [
        (
            config.EVENTS_COMPACT_INTERVAL_ENV,
            config.events_compact_interval,
            config.DEFAULT_EVENTS_COMPACT_INTERVAL,
        ),
        (
            config.EVENTS_HEARTBEAT_MAX_AGE_ENV,
            config.events_heartbeat_max_age,
            config.DEFAULT_EVENTS_HEARTBEAT_MAX_AGE,
        ),
        (
            config.EVENTS_MAX_AGE_ENV,
            config.events_max_age,
            config.DEFAULT_EVENTS_MAX_AGE,
        ),
    ],
)
def test_events_knobs_default_garbage_nonpositive_and_valid(
    monkeypatch, env_var, accessor, default
):
    assert accessor() == default
    for junk in ("garbage", "0", "-1"):
        monkeypatch.setenv(env_var, junk)
        assert accessor() == default, junk
    monkeypatch.setenv(env_var, "5.5")
    assert accessor() == 5.5


def test_events_defaults_are_the_documented_values():
    assert config.DEFAULT_EVENTS_COMPACT_INTERVAL == 60.0
    assert config.DEFAULT_EVENTS_HEARTBEAT_MAX_AGE == 3600.0
    assert config.DEFAULT_EVENTS_MAX_AGE == 7 * 86400.0


# --- subprocess_env: the sanctioned passthrough ------------------------------------


def test_subprocess_env_copies_environment_and_applies_overrides(monkeypatch):
    monkeypatch.setenv("WP42_MARKER", "present")
    env = config.subprocess_env(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    assert env["WP42_MARKER"] == "present"
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    # a COPY: mutating it must not touch the real environment
    env["WP42_MARKER"] = "mutated"
    assert os.environ["WP42_MARKER"] == "present"


def test_subprocess_env_strips_pytest_vars_so_the_gate_cannot_be_hijacked():
    """The merge gate runs `pytest` in a subprocess to decide whether a branch
    lands. If the operator's shell exports PYTEST_ADDOPTS, that subprocess
    inherits it and the gate runs something other than the repo's own suite --
    observed: `PYTEST_ADDOPTS="--cov=swarmsync"` makes the gate's run fail, so a
    green branch is REJECTED. The verdict must depend on the repo under test,
    not on the shell that launched the server."""
    hijacks = {
        "PYTEST_ADDOPTS": "--cov=swarmsync",
        "PYTEST_PLUGINS": "some_plugin",
        "PYTEST_CURRENT_TEST": "leaked::test",
    }
    real = dict(os.environ)
    real.update(hijacks)
    with mock_environ(real):
        env = config.subprocess_env()
    for var in hijacks:
        assert var not in env, f"{var} must not reach the gate's subprocess"
    # still a passthrough for everything else
    assert env.get("PATH") == real.get("PATH")


def test_subprocess_env_strip_does_not_mutate_the_real_environment():
    with mock_environ({**os.environ, "PYTEST_ADDOPTS": "--cov=swarmsync"}):
        config.subprocess_env()
        assert os.environ["PYTEST_ADDOPTS"] == "--cov=swarmsync"
