"""Deterministic scripted edits standing in for LLM agents. DESIGN.md §2 (de-risking).

Unit U9. Agents are deterministic scripted mutators for the prototype, not live
LLMs -- this makes the demo reproducible and removes API-key/nondeterminism risk
from the overnight build (DESIGN §2). Each mutator below is a pure function that
edits one file inside a worktree by `path` (relative to the worktree root, same
string as a parcel's `path`) and returns nothing; the caller (`runner.run_agent`)
re-derives the touched parcel's new `content_hash` from disk afterwards rather
than trusting anything self-reported here.

A real Claude Agent SDK worker replaces these by producing a diff; the
surrounding `runner.py` protocol (lease -> worktree -> commit -> parcel/update
-> integrate -> release) is unchanged either way -- only "how the edit gets
decided" swaps.

All five mutators use the stdlib `ast` module (same tool the classifier uses)
to locate the target `def` by symbol -- a bare name (`"helper"`) for a
top-level function, or `"Class.method"` for a method -- and then a line-range
replace via `ast.FunctionDef.lineno`/`end_lineno`. They deliberately do NOT
touch anything outside the named symbol's own span, so two mutators targeting
two different symbols in the same file (money-shot #1) produce non-overlapping
textual hunks by construction.
"""
from __future__ import annotations

import ast
import textwrap
import time
from pathlib import Path
from typing import Union

StrPath = Union[str, Path]


def _find_def(
    tree: ast.Module, symbol: str
) -> Union[ast.FunctionDef, ast.AsyncFunctionDef]:
    """Locate a top-level function (`"helper"`) or method (`"Class.method"`)."""
    if "." in symbol:
        class_name, _, method_name = symbol.partition(".")
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for member in node.body:
                    if (
                        isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and member.name == method_name
                    ):
                        return member
        raise ValueError(f"no method {symbol!r} found")
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == symbol
        ):
            return node
    raise ValueError(f"no top-level function {symbol!r} found")


def _read(worktree: StrPath, path: str) -> tuple[Path, str]:
    file_path = Path(worktree) / path
    return file_path, file_path.read_text(encoding="utf-8")


def edit_function_body(worktree: StrPath, path: str, symbol: str, new_body: str) -> None:
    """Replace `symbol`'s body (everything after its `def ...:` header, keeping
    the signature untouched) with `new_body` (a flush-left Python snippet,
    `textwrap.dedent`-ed and re-indented to match the original body's indent).

    This is the money-shot #1 mutator: two agents calling this on two
    different `symbol`s in the same file touch disjoint line ranges.
    """
    file_path, text = _read(worktree, path)
    tree = ast.parse(text)
    node = _find_def(tree, symbol)
    lines = text.splitlines(keepends=True)

    first_stmt = node.body[0]
    indent = " " * first_stmt.col_offset
    start_line = first_stmt.lineno  # 1-indexed, inclusive
    end_line = node.end_lineno  # 1-indexed, inclusive
    assert end_line is not None

    new_lines = [
        f"{indent}{line}\n" if line.strip() else "\n"
        for line in textwrap.dedent(new_body).strip("\n").splitlines()
    ]
    lines[start_line - 1 : end_line] = new_lines
    file_path.write_text("".join(lines), encoding="utf-8")


def change_signature(worktree: StrPath, path: str, symbol: str, new_sig: str) -> None:
    """Rewrite `symbol`'s header line to `new_sig` (e.g. `"def helper(x, y=1, z=0)"`),
    leaving its body and any decorators untouched.

    Money-shot #3's mutator: an agent deliberately breaks a frozen contract's
    signature under an exclusive lease so a dependent can observe the
    `contract_change` event and re-plan.

    Assumes (as every mutator here does, per the prototype's de-risking notes)
    a single-line `def ...:` header -- true for every symbol these mutators
    are exercised against in `sample_repo`/the test fixtures.
    """
    file_path, text = _read(worktree, path)
    tree = ast.parse(text)
    node = _find_def(tree, symbol)
    lines = text.splitlines(keepends=True)

    header_start = node.lineno  # 1-indexed; the `def` line itself, not decorators
    header_end = max(header_start, node.body[0].lineno - 1)
    indent = " " * node.col_offset

    sig = new_sig.strip()
    if not sig.endswith(":"):
        sig += ":"
    lines[header_start - 1 : header_end] = [f"{indent}{sig}\n"]
    file_path.write_text("".join(lines), encoding="utf-8")


def fix_call_site(worktree: StrPath, path: str, symbol: str, old: str, new: str) -> None:
    """Replace the first textual occurrence of `old` with `new` inside `symbol`'s
    body only (never outside it, so an unrelated call to the same name
    elsewhere in the file is untouched).

    Money-shot #3's follow-up mutator: the dependent agent that observed a
    `contract_change` event uses this to fix its own call site after
    re-reading the new signature.
    """
    file_path, text = _read(worktree, path)
    tree = ast.parse(text)
    node = _find_def(tree, symbol)
    lines = text.splitlines(keepends=True)

    start_line = node.lineno
    end_line = node.end_lineno
    assert end_line is not None

    for i in range(start_line - 1, end_line):
        if old in lines[i]:
            lines[i] = lines[i].replace(old, new, 1)
            file_path.write_text("".join(lines), encoding="utf-8")
            return
    raise ValueError(f"{old!r} not found inside {symbol!r} in {path}")


def break_a_test(
    worktree: StrPath,
    path: str,
    symbol: str,
    message: str = "mutator: intentionally broken for test-gate rejection (money-shot #5)",
) -> None:
    """Rewrite `symbol` to unconditionally raise -- guaranteed to fail any test
    that exercises it, for the integrator's pytest gate to catch and reject
    (money-shot #5). Built on `edit_function_body` so it produces the same
    kind of disjoint, symbol-scoped hunk as any other edit."""
    edit_function_body(worktree, path, symbol, f"raise RuntimeError({message!r})")


def slow_edit(
    worktree: StrPath,
    path: str,
    symbol: str,
    new_body: str,
    hang: bool = True,
    delay: float = 5.0,
) -> None:
    """Apply `new_body` via `edit_function_body`, then either hang forever
    (`hang=True`, the default) or sleep `delay` seconds before returning.

    Money-shot #4's mutator: the edit lands on disk first (so if the agent
    process is SIGKILLed while this call is hanging, there is real
    uncommitted work sitting in the worktree -- proving the crash truly
    happened *mid-edit*, not before it started), then the call blocks so an
    external harness has a window to kill the process while it still holds
    its lease. Because nothing past this point ever runs (`commit_all`,
    `parcel/update`, `release`), the reaper is the only thing that reclaims
    the lease -- exactly DESIGN §6's crash-recovery path. `hang=False` is the
    deterministic/testable variant: it returns normally after `delay`.
    """
    edit_function_body(worktree, path, symbol, new_body)
    if hang:
        while True:
            time.sleep(1.0)
    else:
        time.sleep(delay)
