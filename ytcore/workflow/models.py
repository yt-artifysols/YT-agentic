from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VideoJobInput:
    job_id: str
    channel_id: str
    origin: str  # owner | planner | trigger | experiment | derived
    approval_timeout_seconds: int = 48 * 3600
    max_qa_rework: int = 2
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


@dataclass(frozen=True)
class PublishRequest:
    job_id: str
    channel_id: str
    content_hash: str
    token: str


@dataclass(frozen=True)
class VideoJobResult:
    job_id: str
    final_state: str
    content_hash: str | None = None
    video_id: str | None = None
    replanned: bool = False
