from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VideoJobInput:
    job_id: str
    channel_id: str
    origin: str  # owner | planner | trigger | experiment | derived
    approval_timeout_seconds: int = 48 * 3600
    max_qa_rework: int = 2
    # A worker that dies mid-activity is detected after this long without a heartbeat, instead
    # of waiting for the activity timeout (up to 2 hours for a publish).
    heartbeat_timeout_seconds: int = 120
    # Production stages may wait in line for a fair-share pool slot before they run.
    stage_timeout_seconds: int = 2700
    # Absolute approval deadline (unix seconds) set by the planner: it does NOT move when
    # production takes longer than expected. None = now + approval_timeout_seconds at sealing.
    approval_deadline_epoch: float | None = None
    # Set by a cluster supervisor: the job tells it when production is done (it then waits on a
    # human, which must not occupy a production slot).
    supervisor_id: str | None = None
    parent_job_id: str | None = None


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    approver: str
    token: str | None = None  # Ed25519 approval token from the approval hub
    reason: str = ""


@dataclass(frozen=True)
class StageResult:
    stage: str
    artifacts: dict[str, str] = field(default_factory=dict)  # name -> sha256 digest


@dataclass(frozen=True)
class QAReport:
    passed: bool
    rework_stage: str | None = None  # "scripting" | "rendering" when not passed
    notes: str = ""
    risk_score: int = 0  # 0..100; the approval hub sorts and fast-lanes by it
    flags: list[dict[str, str]] = field(default_factory=list)  # flagged claims (untrusted data)
    sources: list[dict[str, str]] = field(default_factory=list)  # cited sources (untrusted data)


@dataclass(frozen=True)
class HubDecision:
    """Signal from the approval hub. The hub has already written the DB side of the decision."""

    action: str  # approve | edit | reject
    actor: str
    content_hash: str
    token: str | None = None  # approve: token already consumed by the hub
    rework_stage: str | None = None  # reject: research | script | render | discard
    reason: str = ""


@dataclass(frozen=True)
class PublishOutcome:
    video_id: str
    state: str  # published | live
    duplicate: bool = False


@dataclass(frozen=True)
class VideoJobResult:
    job_id: str
    final_state: str
    content_hash: str | None = None
    video_id: str | None = None
    replanned: bool = False
    reason: str = ""
