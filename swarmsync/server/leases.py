"""DEPRECATED re-export shim -- slated for removal.

The lease manager moved to `swarmsync.blackboard.leases` (WP4.1, finding A2:
it is pure SQLite domain logic and belongs inside the blackboard layer, as
ARCHITECTURE.md's diagram always said). This shim keeps the old import path
working for third-party callers only; in-repo code imports the new home
directly. Do not add anything here.
"""
from swarmsync.blackboard.leases import (  # noqa: F401
    DEFAULT_MAX_LEASES_PER_AGENT,
    DEFAULT_TTL_SECONDS,
    MAX_LEASES_PER_AGENT_ENV,
    MAX_PARCEL_ID_LENGTH,
    acquire,
    heartbeat,
    release,
)
