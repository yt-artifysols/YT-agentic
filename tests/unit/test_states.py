from __future__ import annotations

from ytcore.tools import gen_transitions_sql
from ytcore.workflow.states import (
    APPROVAL_GATED_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    JobState,
    can_transition,
)


def _sources(target: JobState) -> set[JobState]:
    return {s for s, ds in TRANSITIONS.items() if target in ds}


def test_terminal_states_have_no_exits() -> None:
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()


def test_publish_path_is_linear_and_gated() -> None:
    assert _sources(JobState.APPROVED) == {JobState.AWAITING_APPROVAL}
    assert _sources(JobState.SCHEDULED) == {JobState.APPROVED}
    assert _sources(JobState.PUBLISHING) == {JobState.SCHEDULED}
    assert _sources(JobState.PUBLISHED) == {JobState.PUBLISHING}
    assert _sources(JobState.LIVE) == {JobState.PUBLISHED}
    assert {JobState.APPROVED, JobState.SCHEDULED, JobState.PUBLISHING} <= APPROVAL_GATED_STATES


def test_live_is_the_only_successful_terminal_state() -> None:
    assert {
        JobState.LIVE,
        JobState.REJECTED,
        JobState.CANCELLED,
        JobState.FAILED,
    } == TERMINAL_STATES


def test_publishing_cannot_be_cancelled() -> None:
    assert not can_transition(JobState.PUBLISHING, JobState.CANCELLED)
    assert not can_transition(JobState.PUBLISHED, JobState.CANCELLED)


def test_waiting_states_can_be_cancelled() -> None:
    for state in (JobState.AWAITING_APPROVAL, JobState.APPROVED, JobState.SCHEDULED):
        assert can_transition(state, JobState.CANCELLED)


def test_seed_sql_is_up_to_date_and_repeatable() -> None:
    assert gen_transitions_sql.main(["--check"]) == 0
    assert gen_transitions_sql.render().startswith("-- repeatable\n")
