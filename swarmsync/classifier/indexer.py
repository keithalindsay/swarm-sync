"""Parse a Python repo into parcels. DESIGN.md §3 (steps 1-2).

Built in Unit U2. Uses the stdlib `ast` module (no tree-sitter for the Python target;
the function boundary below is written so a tree-sitter backend can replace it later).

Core API:
  parse_file(path, rel_path=None) -> list[Parcel]
    - AST-walk one module; emit a Parcel for every top-level def/async def/class and
      every method inside a class, plus one synthetic module interstitial.
  index_repo(root) -> list[Parcel]        # walk all *.py under root; a single
                                          # unparseable file is skipped-and-logged
                                          # (OSError/SyntaxError/UnicodeDecodeError),
                                          # never aborting the whole index.

GRANULARITY: emit at symbol granularity, but the ENFORCED lease granularity is chosen
in graph.py / server config and defaults to FILE (see DESIGN §2 de-risking). Keep the
symbol spans so symbol-mode can be switched on per parcel for test case #1.

Span/hash design note (this unit's own decision, not spec-mandated in this much
detail): a top-level `def`/`async def` and a method inside a class get a concrete,
byte-exact `(byte_start, byte_end)` span (decorators included) and a content_hash of
that precise byte slice -- these are the parcels that are actually leased at symbol
granularity, so their spans must be mutually disjoint. A `class` parcel and the
per-file `<module>` interstitial are both "glue" buckets for source that is not
inside one of those concrete symbols (decorators/header/docstring/class-level
statements for a class; imports/module docstring/top-level statements for a module).
That glue is not generally contiguous (e.g. a docstring then a method then a trailing
class attribute), so rather than force a single misleading `(start, end)` range we
leave `byte_start`/`byte_end` as `None` for glue parcels (both fields are Optional on
`Parcel`) and hash the concatenation of the leftover byte ranges in file order --
still fully deterministic and stable, and it means "non-overlapping byte spans" is a
clean, unambiguous property of the concrete function/method parcels only.
"""
from __future__ import annotations

import ast
import hashlib
import logging
import time
from pathlib import Path
from typing import Optional, Union

from swarmsync.blackboard.models import Parcel

logger = logging.getLogger(__name__)

StrPath = Union[str, Path]

MODULE_SYMBOL = "<module>"

# Directory name fragments to never descend into when walking a repo tree.
_SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", ".pytest_cache", "node_modules"}

# S3 security: bound the `index_repo` walk so a caller cannot make `POST /index`
# spin forever / exhaust memory on a pathologically large (or symlink-inflated)
# tree. Generous enough that no real prototype repo (sample_repo, the fixtures,
# the demo copies) comes close, so this never changes existing behavior.
DEFAULT_MAX_INDEX_FILES = 5000
DEFAULT_MAX_INDEX_SECONDS = 30.0


class IndexLimitError(RuntimeError):
    """Raised when an `index_repo` walk exceeds its file-count or wall-clock cap."""


def _line_start_offsets(source: bytes) -> list[int]:
    """`offsets[i]` (0-indexed) = absolute byte offset of the start of line `i+1`."""
    offsets = [0]
    pos = 0
    for line in source.splitlines(keepends=True):
        pos += len(line)
        offsets.append(pos)
    return offsets


def _abs_offset(line_offsets: list[int], lineno: int, col_offset: int) -> int:
    """Absolute byte offset for an ast `(lineno, col_offset)` pair.

    `ast` documents `col_offset` as a UTF-8 *byte* offset within its line (verified
    empirically: a multi-byte character earlier on the line shifts later col_offsets
    by its extra byte count, not by 1), so this is a direct sum against line-start
    byte offsets computed from the raw source bytes -- no decode/re-encode needed.
    """
    return line_offsets[lineno - 1] + col_offset


def _decorated_start(node: ast.stmt, source_lines: list[bytes], line_offsets: list[int]) -> int:
    """Byte offset of the start of `node`, extended back over any decorators
    (including the leading '@') so a decorated def/class is one parcel.

    `ast` node.lineno/col_offset for a decorated def/class point at the `def`/
    `class` keyword, not the decorator, so decorators must be located separately.
    """
    decorators = getattr(node, "decorator_list", None) or []
    if not decorators:
        return _abs_offset(line_offsets, node.lineno, node.col_offset)
    first = decorators[0]
    line = source_lines[first.lineno - 1]
    at_idx = line.rfind(b"@", 0, first.col_offset + 1)
    if at_idx == -1:
        at_idx = first.col_offset
    return line_offsets[first.lineno - 1] + at_idx


def _node_span(node: ast.stmt, source_lines: list[bytes], line_offsets: list[int]) -> tuple[int, int]:
    start = _decorated_start(node, source_lines, line_offsets)
    # `ast.parse` always populates end positions on real statement nodes (Python 3.8+);
    # typeshed types them Optional, so pin the invariant explicitly for the byte-offset math.
    assert node.end_lineno is not None and node.end_col_offset is not None
    end = _abs_offset(line_offsets, node.end_lineno, node.end_col_offset)
    return start, end


def _leftover_bytes(
    source: bytes, covered: list[tuple[int, int]], span_start: int, span_end: int
) -> bytes:
    """Concatenate the parts of `source[span_start:span_end]` NOT covered by any
    `(start, end)` range in `covered`, in file order. This is the 'glue' bucket --
    module or class-level code not inside a named symbol."""
    pieces: list[bytes] = []
    cursor = span_start
    for start, end in sorted(covered):
        if start > cursor:
            pieces.append(source[cursor:start])
        cursor = max(cursor, end)
    if cursor < span_end:
        pieces.append(source[cursor:span_end])
    return b"".join(pieces)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_file(path: StrPath, rel_path: Optional[str] = None) -> list[Parcel]:
    """Parse one Python source file into its parcel list.

    Emits (DESIGN §3 step 2):
      - one parcel per top-level `def`/`async def` (kind="function")
      - one parcel per method inside a top-level class (kind="method")
      - one parcel per top-level `class` (kind="class") -- its own glue only,
        i.e. decorators/header/docstring/class-level statements, NOT its methods
      - exactly one synthetic `<path>::<module>` interstitial (kind="module")
        bucketing everything else (imports, module docstring, top-level
        statements outside any def/class)

    `rel_path` is the path recorded on each parcel's `id`/`path` (defaults to
    `str(path)`); callers indexing a repo should pass the path relative to the
    repo root so parcel ids are stable across machines.
    """
    p = Path(path)
    rel = Path(rel_path if rel_path is not None else str(p)).as_posix()
    source = p.read_bytes()
    text = source.decode("utf-8")
    tree = ast.parse(text, filename=rel)

    line_offsets = _line_start_offsets(source)
    source_lines = source.splitlines(keepends=True)

    now = time.time()
    parcels: list[Parcel] = []
    module_covered: list[tuple[int, int]] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start, end = _node_span(node, source_lines, line_offsets)
            module_covered.append((start, end))
            parcels.append(
                Parcel(
                    id=f"{rel}::{node.name}",
                    path=rel,
                    symbol=node.name,
                    kind="function",
                    blast_radius=0,
                    content_hash=_hash(source[start:end]),
                    byte_start=start,
                    byte_end=end,
                    updated_at=now,
                )
            )
        elif isinstance(node, ast.ClassDef):
            class_start, class_end = _node_span(node, source_lines, line_offsets)
            module_covered.append((class_start, class_end))

            method_covered: list[tuple[int, int]] = []
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m_start, m_end = _node_span(member, source_lines, line_offsets)
                    method_covered.append((m_start, m_end))
                    parcels.append(
                        Parcel(
                            id=f"{rel}::{node.name}.{member.name}",
                            path=rel,
                            symbol=f"{node.name}.{member.name}",
                            kind="method",
                            blast_radius=0,
                            content_hash=_hash(source[m_start:m_end]),
                            byte_start=m_start,
                            byte_end=m_end,
                            updated_at=now,
                        )
                    )

            glue = _leftover_bytes(source, method_covered, class_start, class_end)
            parcels.append(
                Parcel(
                    id=f"{rel}::{node.name}",
                    path=rel,
                    symbol=node.name,
                    kind="class",
                    blast_radius=0,
                    content_hash=_hash(glue),
                    byte_start=None,
                    byte_end=None,
                    updated_at=now,
                )
            )

    module_glue = _leftover_bytes(source, module_covered, 0, len(source))
    parcels.append(
        Parcel(
            id=f"{rel}::{MODULE_SYMBOL}",
            path=rel,
            symbol=None,
            kind="module",
            blast_radius=0,
            content_hash=_hash(module_glue),
            byte_start=None,
            byte_end=None,
            updated_at=now,
        )
    )
    return parcels


def _is_skipped(rel_parts: tuple[str, ...]) -> bool:
    return any(part in _SKIP_DIRS or part.startswith(".") for part in rel_parts)


def index_repo(
    root: StrPath,
    *,
    max_files: int = DEFAULT_MAX_INDEX_FILES,
    max_seconds: float = DEFAULT_MAX_INDEX_SECONDS,
) -> list[Parcel]:
    """Walk `root` for every `.py` file (skipping VCS/venv/cache dirs) and return
    the concatenation of `parse_file` over all of them, with each parcel's `path`
    recorded relative to `root` (POSIX-style, stable across machines).

    S3 security: the walk is bounded -- it raises `IndexLimitError` once it has
    considered more than `max_files` candidate `.py` files or spent more than
    `max_seconds` wall-clock, so a caller-supplied `root` cannot turn `POST /index`
    into an unbounded CPU/memory sink."""
    root_path = Path(root).resolve()
    deadline = time.monotonic() + max_seconds
    considered = 0
    parcels: list[Parcel] = []
    for py_file in sorted(root_path.rglob("*.py")):
        rel_path_obj = py_file.relative_to(root_path)
        if _is_skipped(rel_path_obj.parts[:-1]):
            continue
        # Skip a symlink that ALIASES another file in this same repo. Two paths that
        # name one inode are one file, and one file must be one parcel: indexing both
        # produced `real.py::helper` AND `link.py::helper` for a single inode, hence two
        # independent write leases on one physical file. On the hook path -- where
        # subagents share one working tree and the lease is the ONLY protection -- that
        # is a silently lost edit, exactly the collision this system exists to prevent.
        # The target is already indexed under its real name, so nothing is lost.
        #
        # A symlink pointing OUT of the repo is deliberately still indexed, under its own
        # in-repo name (S5): it is the only name this repo has for that file, and it must
        # stay leasable or an edit through it bypasses coordination entirely.
        # `hooks/adapter._relpath` maps the same way, so the two agree on identity.
        try:
            if py_file.is_symlink():
                target = py_file.resolve()
                if target.is_relative_to(root_path.resolve()):
                    continue
        except (OSError, ValueError):  # pragma: no cover - broken link / unreadable
            pass
        considered += 1
        if considered > max_files:
            raise IndexLimitError(
                f"index walk of {root_path} exceeded max_files={max_files}"
            )
        if time.monotonic() > deadline:
            raise IndexLimitError(
                f"index walk of {root_path} exceeded max_seconds={max_seconds}"
            )
        rel = rel_path_obj.as_posix()
        # Skip-and-log a single unreadable/unparseable file instead of aborting
        # the whole index, mirroring build_graph's per-file guard: one malformed
        # `.py` (syntax error, invalid encoding, vanished/permission-denied file)
        # must not stop the rest of the repo from being indexed. The test gate is
        # the backstop for a genuinely broken file (DESIGN §6 "classifier miss").
        try:
            parcels.extend(parse_file(py_file, rel_path=rel))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            logger.warning("skipping unparseable file %s: %s", rel, exc)
            continue
    return parcels
