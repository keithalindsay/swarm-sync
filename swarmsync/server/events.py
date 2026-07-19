"""DEPRECATED re-export shim -- slated for removal.

The event-log / pheromone module moved to `swarmsync.blackboard.events` (WP4.1,
finding A2: it is pure SQLite domain logic and belongs inside the blackboard
layer, as ARCHITECTURE.md's diagram always said). This shim keeps the old
import path working for third-party callers only; in-repo code imports the new
home directly. Do not add anything here.
"""
from swarmsync.blackboard.events import (  # noqa: F401
    DEFAULT_EVENT_MAX_AGE,
    DEFAULT_HEARTBEAT_MAX_AGE,
    EVENT_MAX_AGE_ENV,
    EVENTS_COMPACTED,
    HEARTBEAT_EVENT_TYPES,
    HEARTBEAT_MAX_AGE_ENV,
    compact_events,
    decay_pheromone,
    drop_pheromone,
    emit,
    tail,
)
