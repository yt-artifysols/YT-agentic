"""Activities for VideoJobWorkflow. Store and gateway are injected; test fakes: tests/fakes.py."""

from __future__ import annotations

from typing import Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError

from ytcore.approvals.hashing import content_hash, file_digest
from ytcore.approvals.token import ApprovalError
from ytcore.gateway.youtube import YouTubeGateway
from ytcore.workflow.models import PublishRequest, QAReport, StageResult
from ytcore.workflow.states import JobState


class JobStore(Protocol):
    def get_state(self, job_id: str) -> JobState: ...
    def set_state(self, job_id: str, state: JobState, content_hash: str | None) -> None: ...


class JobActivities:
    def __init__(self, store: JobStore, gateway: YouTubeGateway) -> None:
        self._store = store
        self._gateway = gateway
        self.replan_requests: list[tuple[str, str, str]] = []

    @activity.defn
    async def transition(self, job_id: str, to_state: str, content_hash: str | None) -> None:
        try:
            self._store.set_state(job_id, JobState(to_state), content_hash)
        except ValueError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc

    @activity.defn
    async def run_stage(self, job_id: str, channel_id: str, stage: str) -> StageResult:
        # Placeholder for the specialist agent pools; produces a deterministic fake artifact.
        payload = f"{channel_id}/{job_id}/{stage}".encode()
        return StageResult(stage=stage, artifacts={f"{stage}.out": file_digest(payload)})

    @activity.defn
    async def run_qa(self, job_id: str, channel_id: str) -> QAReport:
        return QAReport(passed=True)

    @activity.defn
    async def compute_content_hash(
        self, job_id: str, channel_id: str, artifacts: dict[str, str]
    ) -> str:
        return content_hash(
            {"metadata": {"job_id": job_id, "channel_id": channel_id}, "assets": artifacts}
        )

    @activity.defn
    async def publish(self, request: PublishRequest) -> str:
        try:
            return self._gateway.publish(
                job_id=request.job_id,
                channel_id=request.channel_id,
                content_hash=request.content_hash,
                token=request.token,
            )
        except ApprovalError as exc:
            raise ApplicationError(
                f"publish refused: {exc}", type="ApprovalError", non_retryable=True
            ) from exc

    @activity.defn
    async def request_replan(self, job_id: str, channel_id: str, reason: str) -> None:
        self.replan_requests.append((job_id, channel_id, reason))

    def all(self) -> list[object]:
        return [
            self.transition,
            self.run_stage,
            self.run_qa,
            self.compute_content_hash,
            self.publish,
            self.request_replan,
        ]
