from __future__ import annotations

from ytcore.tools import gen_transitions_sql
from ytcore.workflow.states import (
    APPROVAL_GATED_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    JobState,
    can_transition,
)


def test_terminal_states_have_no_exits() -> None:
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()


def test_publishing_only_reachable_from_approved() -> None:
    sources = {s for s, ds in TRANSITIONS.items() if JobState.PUBLISHING in ds}
    assert sources == {JobState.APPROVED}
    approved_sources = {s for s, ds in TRANSITIONS.items() if JobState.APPROVED in ds}
    assert approved_sources == {JobState.AWAITING_APPROVAL}


def test_publish_path_is_approval_gated() -> None:
    assert {JobState.APPROVED, JobState.PUBLISHING} <= APPROVAL_GATED_STATES


def test_publishing_cannot_be_cancelled() -> None:
    assert not can_transition(JobState.PUBLISHING, JobState.CANCELLED)


def test_awaiting_approval_can_be_cancelled_on_deadline() -> None:
    assert can_transition(JobState.AWAITING_APPROVAL, JobState.CANCELLED)


def test_seed_sql_is_up_to_date() -> None:
    assert gen_transitions_sql.main(["--check"]) == 0
