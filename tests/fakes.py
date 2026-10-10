"""In-memory fakes for tests. Production config and the gateway refuse fakes."""

from __future__ import annotations

from dataclasses import dataclass, field

from ytcore.workflow.states import JobState, can_transition


@dataclass
class FakeYouTubeClient:
    """Records uploads instead of calling YouTube. The gateway refuses it in production."""

    is_fake: bool = True
    uploads: list[tuple[str, str]] = field(default_factory=list)

    def upload(self, *, channel_id: str, content_hash: str) -> str:
        self.uploads.append((channel_id, content_hash))
        return f"fake-video-{len(self.uploads)}"


@dataclass
class InMemoryJobStore:
    """Mirrors the DB state machine (the DB triggers remain the real enforcement)."""

    states: dict[str, JobState] = field(default_factory=dict)
    hashes: dict[str, str | None] = field(default_factory=dict)
    history: list[tuple[str, JobState]] = field(default_factory=list)

    def get_state(self, job_id: str) -> JobState:
        return self.states.get(job_id, JobState.PLANNED)

    def set_state(self, job_id: str, state: JobState, content_hash: str | None) -> None:
        current = self.get_state(job_id)
        if not can_transition(current, state):
            raise ValueError(f"illegal transition {current} -> {state}")
        self.states[job_id] = state
        if content_hash is not None:
            self.hashes[job_id] = content_hash
        self.history.append((job_id, state))
