"""Dependency graph, blast radius, and frozen-contract extraction. DESIGN.md §3 (steps 3-6).

Built in Unit U3. Consumes the parcels from `indexer.py`.

Core API:
  build_graph(parcels, root) -> DepGraph
    - import edges: module A imports module B (resolved against the repo's own module set)
    - call edges: symbol A references name B (name-resolution pass over each function body)
  blast_radius(graph) -> dict[parcel_id, int]
    - size of the transitive reverse-dependency set (BFS on the reversed graph)
  extract_contracts(parcels, graph, blast, threshold=FREEZE_THRESHOLD) -> list[Contract]
    - a parcel with blast_radius >= threshold that is imported across module boundaries
      becomes a frozen contract: record signature (name + params + defaults for functions;
      public method signatures for classes) + type_hash.
  co_schedulable(a, b, mode="file", graph=None, frozen_ids=None) -> bool
    - file mode: file(a) != file(b); symbol mode: spans disjoint; AND (when graph/frozen_ids
      are supplied) neither is a frozen contract the other depends on. This is the
      parallel-safety relation (DESIGN §3).

FREEZE_THRESHOLD default 3. Fall back to FILE granularity when name resolution is
uncertain (dynamic dispatch / string imports) -- conservative, backstopped by the test gate.

Design notes (this unit's own decisions, not spelled out byte-for-byte in DESIGN.md):

- **Graph direction.** `edges[a]` = the set of parcel ids `a` *depends on* (dependent ->
  dependency). `reverse_edges[b]` = the set of parcel ids that depend on `b`. Blast radius of
  `b` is the transitive closure over `reverse_edges` starting at `b` -- "how many parcels break
  if b changes."
- **Import edges** resolve `import X[.Y]` and `from X import name` against the repo's own
  dotted-module namespace (derived from every parcel's `path`, since `indexer.index_repo`
  already emits a parcel -- at minimum the `<module>` interstitial -- for every `.py` file).
  Anything that doesn't resolve to a file in the repo (stdlib, third-party, or an
  unresolvable relative import) is silently skipped -- conservative, matches DESIGN §6's
  "classifier miss -> fall back, let the test gate backstop it."
- **`from X import name`** resolves to the *specific* top-level function/class parcel in `X`
  when `name` matches one (this is what lets a heavily-imported function accumulate blast
  radius as an individual symbol, not just as "someone imports module X"). Otherwise (a
  submodule import, or a name we can't resolve -- e.g. a re-exported constant) it falls back
  to a module-granularity edge against `X`'s `<module>` parcel.
  **`import X`** always adds a module-granularity edge up front; a subsequent `X.name(...)`
  attribute-call inside a function body additionally adds a symbol-precise edge if `name`
  resolves, on top of (not instead of) the module edge -- both edges are harmless to keep
  (they're deduplicated by set membership) and the symbol-precise one is what blast radius
  and contract extraction actually key off of.
- **Call edges** are attributed to whichever top-level `def`/`async def` or class-method body
  contains the reference (scanned via `ast.walk` over each def's subtree -- nested defs are
  folded into their enclosing top-level scope, a deliberate simplification for the prototype).
  A bare `Name` reference is resolved against (1) this file's own top-level symbols (a local
  call), then (2) the file's import-alias table built while scanning that file's imports.
  An `Attribute` access (`alias.attr`) additionally tries to resolve `attr` against the
  aliased module's top-level symbols.
- **Frozen contracts** use the *parcel id* (e.g. `"mod_a.py::helper"`) as `Contract.symbol`,
  not the bare name -- the schema's `symbol` column is a `PRIMARY KEY`, and bare names collide
  across files (two modules can both define `helper`). Signatures are computed once, in
  `build_graph`, from the same parse pass that resolves imports/calls (no second file read).
- **`co_schedulable`'s frozen-contract clause** is intentionally conservative and symmetric:
  if either parcel is a frozen contract that the *other* parcel's edges show a dependency on,
  the pair is not co-schedulable, regardless of which one a caller intends to write vs. read --
  the broker (U12) is expected to only call this with the coarser mode/inputs it actually needs;
  callers that don't pass `graph`/`frozen_ids` get pure structural disjointness (this is what
  the done-when test exercises).
"""
from __future__ import annotations

import ast
import hashlib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from swarmsync.blackboard.models import Contract, Parcel

StrPath = Union[str, Path]

FREEZE_THRESHOLD = 3
MODULE_SYMBOL = "<module>"


@dataclass
class DepGraph:
    """Directed dependency graph over parcel ids, plus the bookkeeping
    `extract_contracts` needs (cross-module usage, precomputed signatures)."""

    parcels_by_id: dict[str, Parcel] = field(default_factory=dict)
    edges: dict[str, set[str]] = field(default_factory=dict)
    reverse_edges: dict[str, set[str]] = field(default_factory=dict)
    # dependency_id -> set of *other files* that reference it (import or call).
    cross_module_files: dict[str, set[str]] = field(default_factory=dict)
    # parcel_id -> (signature, type_hash) for top-level function/class parcels.
    signatures: dict[str, tuple[str, str]] = field(default_factory=dict)

    def add_edge(self, dependent: str, dependency: str, dependent_file: str) -> None:
        if dependent == dependency:
            return
        self.edges.setdefault(dependent, set()).add(dependency)
        self.reverse_edges.setdefault(dependency, set()).add(dependent)
        target = self.parcels_by_id.get(dependency)
        if target is not None and target.path != dependent_file:
            self.cross_module_files.setdefault(dependency, set()).add(dependent_file)

    def is_cross_module(self, parcel_id: str) -> bool:
        return bool(self.cross_module_files.get(parcel_id))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _function_signature(node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> str:
    """`name(params...)` with defaults rendered, e.g. `helper(x, y=1, *args, z, **kw)`."""
    args = node.args
    parts: list[str] = []

    positional = args.posonlyargs + args.args
    pad = len(positional) - len(args.defaults)
    defaults = [None] * pad + list(args.defaults)
    for arg, default in zip(positional, defaults):
        parts.append(arg.arg if default is None else f"{arg.arg}={ast.unparse(default)}")

    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    elif args.kwonlyargs:
        parts.append("*")

    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        parts.append(arg.arg if default is None else f"{arg.arg}={ast.unparse(default)}")

    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")

    return f"{node.name}({', '.join(parts)})"


def _class_signature(node: ast.ClassDef) -> str:
    """`class Name(public_method_sig, ...)` -- public (non-underscore-prefixed) methods only."""
    method_sigs = [
        _function_signature(member)
        for member in node.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not member.name.startswith("_")
    ]
    return f"class {node.name}(" + ", ".join(method_sigs) + ")"


def _module_namespace(files: list[str]) -> dict[str, str]:
    """dotted module name -> file path, for every `.py` file in the repo."""
    file_by_dotted: dict[str, str] = {}
    for f in files:
        dotted = f[:-3] if f.endswith(".py") else f
        dotted = dotted.replace("/", ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        file_by_dotted[dotted] = f
    return file_by_dotted


def _resolve_relative_module(file_path: str, node: ast.ImportFrom) -> str:
    """Best-effort dotted-name resolution for `from .foo import bar` / `from ..pkg import baz`."""
    pkg_parts = file_path[:-3].split("/")[:-1]  # this file's own package path, minus filename
    level = node.level or 0
    if level > 1:
        pkg_parts = pkg_parts[: len(pkg_parts) - (level - 1)]
    if node.module:
        return ".".join([*pkg_parts, node.module])
    return ".".join(pkg_parts)


def build_graph(parcels: list[Parcel], root: StrPath) -> DepGraph:
    """Parse every file `parcels` came from and build import/call edges between parcels.

    `root` must be the same repo root `index_repo`/`parse_file` used to produce `parcels`
    (their `path` fields are resolved against it to re-read source for AST analysis)."""
    root_path = Path(root).resolve()
    graph = DepGraph(parcels_by_id={p.id: p for p in parcels})

    files = sorted({p.path for p in parcels})
    file_by_dotted = _module_namespace(files)
    module_parcel_id = {f: f"{f}::{MODULE_SYMBOL}" for f in files}

    top_level_symbols: dict[str, dict[str, str]] = {}
    for p in parcels:
        if p.kind in ("function", "class") and p.symbol and "." not in p.symbol:
            top_level_symbols.setdefault(p.path, {})[p.symbol] = p.id

    for f in files:
        source_path = root_path / f
        try:
            text = source_path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=f)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue

        this_module_id = module_parcel_id[f]
        local_symbols = top_level_symbols.get(f, {})

        # Precompute signatures for this file's top-level function/class parcels.
        #
        # `local_symbols` comes from the PASSED-IN parcels (the blackboard's map),
        # while `def_node` comes from the file as it is on disk RIGHT NOW. Those two
        # legitimately disagree: there is no incremental indexing, so the map is only
        # ever as fresh as the last `POST /index`, and `broker.load_scheduling_graph`
        # deliberately passes the current DB parcels. Any symbol added since -- an
        # agent's new function, a brand-new file the hook auto-created a coarse
        # `<path>::<module>` parcel for -- is therefore on disk with no parcel row.
        # That is an ORDINARY state, not a broken one.
        #
        # This used to subscript `local_symbols[def_node.name]` directly, so such a
        # symbol raised KeyError out of `build_graph` -- uncaught by the guard above
        # (it catches OSError/SyntaxError, not KeyError) and uncaught by
        # `load_scheduling_graph`, killing dispatch for EVERY task, on every file,
        # until the next re-index. A symbol with no parcel simply has no signature
        # entry, exactly as if its file had not been indexed at all.
        for def_node in tree.body:
            if isinstance(def_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                parcel_id = local_symbols.get(def_node.name)
                if parcel_id is None:
                    continue
                sig = (
                    _class_signature(def_node)
                    if isinstance(def_node, ast.ClassDef)
                    else _function_signature(def_node)
                )
                graph.signatures[parcel_id] = (sig, _hash(sig))

        # local_names: name visible in this file -> ("module", file) or ("symbol", parcel_id)
        local_names: dict[str, tuple[str, str]] = {}

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target_file = file_by_dotted.get(alias.name)
                    if target_file is None:
                        continue
                    local_name = alias.asname or alias.name.split(".")[0]
                    local_names[local_name] = ("module", target_file)
                    graph.add_edge(this_module_id, module_parcel_id[target_file], f)
            elif isinstance(node, ast.ImportFrom):
                dotted = (
                    _resolve_relative_module(f, node)
                    if node.level
                    else (node.module or "")
                )
                target_file = file_by_dotted.get(dotted)
                for alias in node.names:
                    local_name = alias.asname or alias.name
                    if target_file is None:
                        continue  # external/stdlib/unresolvable -- ignore (conservative)
                    target_symbols = top_level_symbols.get(target_file, {})
                    if alias.name in target_symbols:
                        target_id = target_symbols[alias.name]
                        local_names[local_name] = ("symbol", target_id)
                        graph.add_edge(this_module_id, target_id, f)
                    else:
                        local_names[local_name] = ("module", target_file)
                        graph.add_edge(this_module_id, module_parcel_id[target_file], f)

        _link_call_edges(tree, f, graph, local_names, top_level_symbols, module_parcel_id)

    return graph


def _link_call_edges(
    tree: ast.Module,
    file_path: str,
    graph: DepGraph,
    local_names: dict[str, tuple[str, str]],
    top_level_symbols: dict[str, dict[str, str]],
    module_parcel_id: dict[str, str],
) -> None:
    """Attribute every Name/Attribute reference inside each top-level def/method body
    to that def's parcel, resolving against same-file symbols then the import table."""

    def link_name(scope_id: str, name: str) -> None:
        target_id = top_level_symbols.get(file_path, {}).get(name)
        if target_id is not None:
            graph.add_edge(scope_id, target_id, file_path)
            return
        entry = local_names.get(name)
        if entry is None:
            return
        kind, target = entry
        if kind == "symbol":
            graph.add_edge(scope_id, target, file_path)
        elif kind == "module":
            graph.add_edge(scope_id, module_parcel_id[target], file_path)

    def scan_body(scope_id: str, node: ast.AST) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                link_name(scope_id, sub.id)
            elif isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                entry = local_names.get(sub.value.id)
                if entry is not None and entry[0] == "module":
                    target_file = entry[1]
                    target_id = top_level_symbols.get(target_file, {}).get(sub.attr)
                    if target_id is not None:
                        graph.add_edge(scope_id, target_id, file_path)
                    else:
                        graph.add_edge(scope_id, module_parcel_id[target_file], file_path)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scan_body(f"{file_path}::{node.name}", node)
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scan_body(f"{file_path}::{node.name}.{member.name}", member)


def blast_radius(graph: DepGraph) -> dict[str, int]:
    """For every known parcel id, the size of its transitive reverse-dependency set
    (BFS over `reverse_edges`) -- how many parcels break if this one changes."""
    result: dict[str, int] = {}
    for start in graph.parcels_by_id:
        seen: set[str] = set()
        queue: deque[str] = deque(graph.reverse_edges.get(start, set()))
        seen.update(queue)
        while queue:
            node = queue.popleft()
            for nxt in graph.reverse_edges.get(node, set()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        seen.discard(start)  # a dependency cycle could otherwise loop back to start
        result[start] = len(seen)
    return result


def extract_contracts(
    parcels: list[Parcel],
    graph: DepGraph,
    blast: dict[str, int],
    threshold: int = FREEZE_THRESHOLD,
) -> list[Contract]:
    """A top-level function/class parcel with `blast_radius >= threshold` that is also
    imported/called across a module boundary becomes a frozen contract (DESIGN §3 step 5)."""
    contracts: list[Contract] = []
    for p in parcels:
        if p.kind not in ("function", "class"):
            continue
        if blast.get(p.id, 0) < threshold:
            continue
        if not graph.is_cross_module(p.id):
            continue
        sig_info = graph.signatures.get(p.id)
        if sig_info is None:
            continue
        signature, type_hash = sig_info
        contracts.append(
            Contract(symbol=p.id, signature=signature, type_hash=type_hash, frozen=1, version=1)
        )
    return sorted(contracts, key=lambda c: c.symbol)


def co_schedulable(
    a: Parcel,
    b: Parcel,
    mode: str = "file",
    graph: Optional[DepGraph] = None,
    frozen_ids: Optional[set[str]] = None,
) -> bool:
    """The parallel-safety relation (DESIGN §3): structurally disjoint (per `mode`) AND
    (when `graph`/`frozen_ids` are supplied) neither is a frozen contract the other depends on."""
    if mode == "file":
        disjoint = a.path != b.path
    elif mode == "symbol":
        if a.path != b.path:
            disjoint = True
        elif a.byte_start is None or a.byte_end is None or b.byte_start is None or b.byte_end is None:
            # glue parcels (module/class) have no concrete span -- conservatively treat
            # as overlapping with anything else in their own file.
            disjoint = False
        else:
            disjoint = a.byte_end <= b.byte_start or b.byte_end <= a.byte_start
    else:
        raise ValueError(f"unknown granularity mode: {mode!r}")

    if not disjoint:
        return False

    if frozen_ids and graph is not None:
        if a.id in frozen_ids and b.id in graph.reverse_edges.get(a.id, set()):
            return False
        if b.id in frozen_ids and a.id in graph.reverse_edges.get(b.id, set()):
            return False

    return True
