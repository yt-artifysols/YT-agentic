"""Single source of truth for video job states and allowed transitions.

After changing this file run `python ytcore/tools/gen_transitions_sql.py` to regenerate
ytcore/db/migrations/002_transitions_seed.sql. CI fails if the two drift apart.
"""

from __future__ import annotations

from enum import StrEnum


class JobState(StrEnum):
    PLANNED = "planned"
    RESEARCHING = "researching"
    SCRIPTING = "scripting"
    RENDERING = "rendering"
    QA = "qa"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.PUBLISHED, JobState.REJECTED, JobState.CANCELLED, JobState.FAILED}
)

# States from which a job may be cancelled (kill switch, missed approval deadline) or fail.
_ABORTABLE: frozenset[JobState] = frozenset(
    {
        JobState.PLANNED,
        JobState.RESEARCHING,
        JobState.SCRIPTING,
        JobState.RENDERING,
        JobState.QA,
        JobState.AWAITING_APPROVAL,
        JobState.APPROVED,
    }
)

_FORWARD: dict[JobState, frozenset[JobState]] = {
    JobState.PLANNED: frozenset({JobState.RESEARCHING}),
    JobState.RESEARCHING: frozenset({JobState.SCRIPTING}),
    JobState.SCRIPTING: frozenset({JobState.RENDERING}),
    JobState.RENDERING: frozenset({JobState.QA}),
    # QA can send work back for rework.
    JobState.QA: frozenset({JobState.AWAITING_APPROVAL, JobState.SCRIPTING, JobState.RENDERING}),
    JobState.AWAITING_APPROVAL: frozenset({JobState.APPROVED, JobState.REJECTED}),
    JobState.APPROVED: frozenset({JobState.PUBLISHING}),
    # Publishing can only fail; it is never cancelled mid-upload.
    JobState.PUBLISHING: frozenset({JobState.PUBLISHED, JobState.FAILED}),
}


def _build() -> dict[JobState, frozenset[JobState]]:
    table: dict[JobState, frozenset[JobState]] = {}
    for state in JobState:
        targets = set(_FORWARD.get(state, frozenset()))
        if state in _ABORTABLE:
            targets |= {JobState.CANCELLED, JobState.FAILED}
        table[state] = frozenset(targets)
    return table


TRANSITIONS: dict[JobState, frozenset[JobState]] = _build()

# Entering any of these states requires a valid approval bound to the job's content hash.
APPROVAL_GATED_STATES: frozenset[JobState] = frozenset({JobState.APPROVED, JobState.PUBLISHING})


def can_transition(src: JobState, dst: JobState) -> bool:
    return dst in TRANSITIONS[src]


def sorted_transitions() -> list[tuple[JobState, JobState]]:
    order = list(JobState)
    return sorted(
        ((s, d) for s, ds in TRANSITIONS.items() for d in ds),
        key=lambda p: (order.index(p[0]), order.index(p[1])),
    )
