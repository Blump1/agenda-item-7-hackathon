"""Pipeline orchestration.

process_watch() is the unit of work: one watch, from discovery through to a
mutated docket tracker. The CLI runs it directly; Cloud Run will call the same
function from a Pub/Sub handler, so the autonomous path and the debuggable path
are the same code.
"""

from agenda7.pipeline.process import (
    DocOutcome,
    DocResult,
    RunStats,
    comment_window_open,
    process_document,
    process_watch,
)

__all__ = [
    "DocOutcome",
    "DocResult",
    "RunStats",
    "comment_window_open",
    "process_document",
    "process_watch",
]
