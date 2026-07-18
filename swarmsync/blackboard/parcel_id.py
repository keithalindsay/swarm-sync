"""The parcel-id scheme -- the ONE place it is defined (WP4.4, finding A5).

A parcel id is `<relpath>::<symbol>`: a POSIX-style repo-root-relative path,
the literal separator `::`, and a symbol name. The synthetic whole-file
"interstitial" parcel -- the id every file-granularity lease targets -- uses
the reserved symbol `MODULE_SYMBOL` (`"calc.py::<module>"`); real symbol
parcels use the qualified name (`"calc.py::Calculator.add"`).

Every producer or parser of parcel ids (classifier indexer/graph, coordinator
broker, hook adapter, lease repair, agent runner) imports from here, so a
future id-scheme change (the multi-root qualifier, symbol mode) is a one-file
edit instead of a hunt across six call sites.
"""
from __future__ import annotations

# THE definition. Everywhere else in `swarmsync/` must import this constant --
# `tests/test_architecture.py` guards that no other module re-hardcodes it.
MODULE_SYMBOL = "<module>"

_SEPARATOR = "::"


def make_id(relpath: str, symbol: str) -> str:
    """The parcel id for `symbol` inside `relpath` (`symbol` may be
    `MODULE_SYMBOL` for the whole-file interstitial)."""
    return f"{relpath}{_SEPARATOR}{symbol}"


def module_id(relpath: str) -> str:
    """The whole-file (interstitial) parcel id for a POSIX-style relpath:
    `"<relpath>::<module>"`."""
    return make_id(relpath, MODULE_SYMBOL)


def split(parcel_id: str) -> tuple[str, str]:
    """`(path, symbol)` for `parcel_id`, splitting at the FIRST `::`.

    An id containing no `::` yields `(parcel_id, "")` -- byte-for-byte what
    `parcel_id.partition("::")` gave the legacy call sites (`leases.
    _ensure_parcel`, `agent.runner`), both of which use only the path and
    ignore the symbol, so a separator-less id degrades to "the whole string is
    the path" there rather than raising.
    """
    path, _, symbol = parcel_id.partition(_SEPARATOR)
    return path, symbol
