"""U2 — Classifier: parcel extraction. DESIGN.md §3 (steps 1-2).

Done when: parsing a fixture with 2 functions + 1 class-with-method yields exactly
those parcels plus one `<module>` interstitial, each with correct kind, non-overlapping
byte spans, and a stable sha256 content_hash.
"""
from __future__ import annotations

import hashlib
import textwrap

import pytest

from swarmsync.classifier.indexer import index_repo, parse_file

FIXTURE_SOURCE = textwrap.dedent(
    '''\
    """Module docstring for the fixture."""
    import os

    TOP_LEVEL_CONST = 1


    def alpha(x):
        """First top-level function."""
        return x + 1


    def beta(y):
        return y * 2


    class Greeter:
        """A greeter with one method."""

        greeting = "hi"

        def hello(self, name):
            return f"{self.greeting} {name}"
    '''
)


@pytest.fixture()
def fixture_file(tmp_path):
    f = tmp_path / "greet.py"
    f.write_text(FIXTURE_SOURCE, encoding="utf-8")
    return f


def _by_id(parcels):
    return {p.id: p for p in parcels}


def test_yields_exactly_expected_parcels_plus_module_interstitial(fixture_file):
    parcels = parse_file(fixture_file, rel_path="greet.py")
    ids = {p.id for p in parcels}
    assert ids == {
        "greet.py::alpha",
        "greet.py::beta",
        "greet.py::Greeter",
        "greet.py::Greeter.hello",
        "greet.py::<module>",
    }
    assert len(parcels) == 5


def test_kinds_are_correct(fixture_file):
    by_id = _by_id(parse_file(fixture_file, rel_path="greet.py"))
    assert by_id["greet.py::alpha"].kind == "function"
    assert by_id["greet.py::beta"].kind == "function"
    assert by_id["greet.py::Greeter"].kind == "class"
    assert by_id["greet.py::Greeter.hello"].kind == "method"
    assert by_id["greet.py::<module>"].kind == "module"


def test_path_and_symbol_fields(fixture_file):
    by_id = _by_id(parse_file(fixture_file, rel_path="greet.py"))
    for parcel in by_id.values():
        assert parcel.path == "greet.py"
    assert by_id["greet.py::alpha"].symbol == "alpha"
    assert by_id["greet.py::Greeter"].symbol == "Greeter"
    assert by_id["greet.py::Greeter.hello"].symbol == "Greeter.hello"
    assert by_id["greet.py::<module>"].symbol is None


def test_concrete_spans_are_non_overlapping_and_ordered(fixture_file):
    parcels = parse_file(fixture_file, rel_path="greet.py")
    spans = [
        (p.byte_start, p.byte_end, p.id)
        for p in parcels
        if p.byte_start is not None and p.byte_end is not None
    ]
    # alpha, beta, Greeter.hello all get concrete spans; class/module are glue (None).
    assert len(spans) == 3
    spans_sorted = sorted(spans)
    for (start, end, _id) in spans_sorted:
        assert start < end
    for i in range(len(spans_sorted) - 1):
        _, end_i, _ = spans_sorted[i]
        start_next, _, _ = spans_sorted[i + 1]
        assert end_i <= start_next, "concrete parcel spans must not overlap"


def test_glue_parcels_have_no_concrete_span(fixture_file):
    by_id = _by_id(parse_file(fixture_file, rel_path="greet.py"))
    assert by_id["greet.py::Greeter"].byte_start is None
    assert by_id["greet.py::Greeter"].byte_end is None
    assert by_id["greet.py::<module>"].byte_start is None
    assert by_id["greet.py::<module>"].byte_end is None


def test_content_hash_matches_exact_byte_slice(fixture_file):
    source = fixture_file.read_bytes()
    by_id = _by_id(parse_file(fixture_file, rel_path="greet.py"))
    for pid in ("greet.py::alpha", "greet.py::beta", "greet.py::Greeter.hello"):
        parcel = by_id[pid]
        sliced = source[parcel.byte_start : parcel.byte_end]
        assert parcel.content_hash == hashlib.sha256(sliced).hexdigest()
        # sanity: the slice really is that symbol's source text
        assert "def " in sliced.decode("utf-8")


def test_content_hash_is_stable_across_reparse(fixture_file):
    first = _by_id(parse_file(fixture_file, rel_path="greet.py"))
    second = _by_id(parse_file(fixture_file, rel_path="greet.py"))
    for pid in first:
        assert first[pid].content_hash == second[pid].content_hash
        assert first[pid].byte_start == second[pid].byte_start
        assert first[pid].byte_end == second[pid].byte_end


def test_content_hash_changes_when_source_changes(tmp_path):
    f = tmp_path / "greet.py"
    f.write_text(FIXTURE_SOURCE, encoding="utf-8")
    before = _by_id(parse_file(f, rel_path="greet.py"))

    mutated = FIXTURE_SOURCE.replace("return x + 1", "return x + 999")
    f.write_text(mutated, encoding="utf-8")
    after = _by_id(parse_file(f, rel_path="greet.py"))

    assert before["greet.py::alpha"].content_hash != after["greet.py::alpha"].content_hash
    # untouched symbols keep the same hash
    assert before["greet.py::beta"].content_hash == after["greet.py::beta"].content_hash
    assert (
        before["greet.py::Greeter.hello"].content_hash
        == after["greet.py::Greeter.hello"].content_hash
    )


def test_decorated_function_span_includes_decorator(tmp_path):
    src = textwrap.dedent(
        '''\
        import functools


        @functools.lru_cache
        def cached(x):
            return x
        '''
    )
    f = tmp_path / "dec.py"
    f.write_text(src, encoding="utf-8")
    by_id = _by_id(parse_file(f, rel_path="dec.py"))
    parcel = by_id["dec.py::cached"]
    source = f.read_bytes()
    sliced = source[parcel.byte_start : parcel.byte_end]
    assert sliced.startswith(b"@functools.lru_cache")


def test_unicode_source_byte_offsets_are_correct(tmp_path):
    src = 'GREETING = "héllo"\n\n\ndef greet(name):\n    return f"{name}"\n'
    f = tmp_path / "uni.py"
    f.write_text(src, encoding="utf-8")
    by_id = _by_id(parse_file(f, rel_path="uni.py"))
    parcel = by_id["uni.py::greet"]
    source = f.read_bytes()
    sliced = source[parcel.byte_start : parcel.byte_end]
    assert sliced.decode("utf-8").startswith("def greet(name):")


def test_index_repo_walks_all_py_files_and_skips_junk_dirs(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "b.py").write_text("def g():\n    return 2\n", encoding="utf-8")

    junk = tmp_path / "__pycache__"
    junk.mkdir()
    (junk / "c.py").write_text("def h():\n    return 3\n", encoding="utf-8")

    parcels = index_repo(tmp_path)
    ids = {p.id for p in parcels}
    assert "a.py::f" in ids
    assert "pkg/b.py::g" in ids
    assert not any(pid.startswith("__pycache__") for pid in ids)


def test_index_repo_skips_malformed_file_and_indexes_the_rest(tmp_path, caplog):
    """S2 regression: one unparseable `.py` must not abort the whole index.

    Before the guard, `index_repo` let the malformed file's `SyntaxError` (and,
    for bad bytes, `UnicodeDecodeError`) propagate out of `parse_file`, so a
    single broken file in the repo took the entire parcel map down with it.
    """
    (tmp_path / "good_a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (tmp_path / "good_b.py").write_text(
        "class B:\n    def m(self):\n        return 2\n", encoding="utf-8"
    )
    # syntactically broken Python
    (tmp_path / "broken_syntax.py").write_text("def oops(:\n    pass\n", encoding="utf-8")
    # invalid UTF-8 bytes -> UnicodeDecodeError inside parse_file
    (tmp_path / "broken_bytes.py").write_bytes(b"\xff\xfe def x(): pass\n")

    import logging

    with caplog.at_level(logging.WARNING):
        parcels = index_repo(tmp_path)  # must not raise

    ids = {p.id for p in parcels}
    # the good files are fully indexed...
    assert "good_a.py::a" in ids
    assert "good_b.py::B.m" in ids
    # ...and neither broken file contributed any parcels
    assert not any(pid.startswith("broken_syntax.py") for pid in ids)
    assert not any(pid.startswith("broken_bytes.py") for pid in ids)
    # each skip is logged (not swallowed silently)
    skipped = {rec.getMessage() for rec in caplog.records}
    assert any("broken_syntax.py" in m for m in skipped)
    assert any("broken_bytes.py" in m for m in skipped)


def test_index_repo_reindex_is_deterministic(tmp_path):
    (tmp_path / "a.py").write_text(FIXTURE_SOURCE, encoding="utf-8")
    first = {p.id: p.content_hash for p in index_repo(tmp_path)}
    second = {p.id: p.content_hash for p in index_repo(tmp_path)}
    assert first == second
