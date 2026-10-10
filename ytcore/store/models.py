from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ytcore.workflow.states import JobState


@dataclass(frozen=True)
class PublishContext:
    job_id: str
    channel_id: str
    state: JobState
    content_hash: str | None
    manifest: dict[str, Any] | None
    storage_prefix: str
    channel_status: str
    cluster_status: str | None
    autonomy_tier: str
    google_project_id: str | None
    credentials_ref: str | None
    oauth_client_ref: str | None
    # The global kill switch (global_controls.publishing_paused).
    global_paused: bool = False

    @property
    def publishable(self) -> bool:
        return (
            not self.global_paused
            and self.channel_status == "active"
            and self.cluster_status == "active"
            and self.autonomy_tier == "gated"
            and self.google_project_id is not None
            and self.credentials_ref is not None
            and self.oauth_client_ref is not None
        )


@dataclass(frozen=True)
class UploadRecord:
    id: str
    job_id: str
    idempotency_key: str
    status: str  # pending | uploaded | live | failed; "in_progress" = lost a concurrent race
    youtube_video_id: str | None
