"""Activities for VideoJobWorkflow.

Dependencies are injected: a Backend (ytcore/store/postgres.py in production, a fake in
tests/fakes.py) and a Publisher (the YouTube gateway, ytcore/gateway/service.py). Blocking I/O
runs in a worker thread so the activity event loop stays free.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError

from ytcore.approvals.hashing import file_digest
from ytcore.approvals.token import ApprovalClaims
from ytcore.gateway.assets import AssetSink
from ytcore.gateway.service import (
    PublishRefused,
    PublishResult,
    UploadFailed,
    UploadInProgress,
)
from ytcore.store.errors import (
    ApprovalGateError,
    ApprovalInvalid,
    ApprovalReplayError,
    BudgetExceeded,
    ChannelNotPublishable,
    IllegalTransition,
    QuotaExhausted,
    StoreError,
)
from ytcore.workflow.models import PublishOutcome, QAReport, StageResult
from ytcore.workflow.states import TERMINAL_STATES, JobState, can_transition

ACTOR = "orchestrator"
# Fake per-stage costs (cents) until real agents report their own.
STAGE_COST_CENTS = {"render": 25}


class Backend(Protocol):
    def get_state(self, job_id: str) -> JobState: ...

    def transition(self, job_id: str, to_state: JobState, *, actor: str) -> None: ...

    def submit_for_approval(
        self,
        job_id: str,
        manifest: dict[str, Any],
        *,
        actor: str,
        deadline: datetime | None = None,
    ) -> str:
        """Store the manifest, set its content hash and move to awaiting_approval."""
        ...

    def approval_on_record(self, job_id: str, token: str) -> ApprovalClaims | None:
        """Claims if the approval hub already consumed this token for the current content."""
        ...

    def record_qa(
        self,
        job_id: str,
        *,
        passed: bool,
        risk_score: int,
        flags: list[dict[str, Any]],
        sources: list[dict[str, Any]],
        actor: str,
    ) -> None: ...

    def verify_approval(self, job_id: str, token: str, *, actor: str) -> ApprovalClaims:
        """Verify with the PUBLIC key, consume the jti and approve, atomically."""
        ...

    def record_cost(self, job_id: str, kind: str, cost_cents: int, *, actor: str) -> None: ...

    def storage_prefix(self, channel_id: str) -> str: ...


class Publisher(Protocol):
    def publish(self, job_id: str, token: str, *, actor: str = ...) -> PublishResult: ...


def _non_retryable(exc: Exception, kind: str) -> ApplicationError:
    return ApplicationError(f"{kind}: {exc}", type=kind, non_retryable=True)


class JobActivities:
    def __init__(self, backend: Backend, publisher: Publisher, assets: AssetSink) -> None:
        self._backend = backend
        self._publisher = publisher
        self._assets = assets
        self.replan_requests: list[tuple[str, str, str]] = []

    @activity.defn
    async def transition(self, job_id: str, to_state: str) -> None:
        try:
            await asyncio.to_thread(
                self._backend.transition, job_id, JobState(to_state), actor=ACTOR
            )
        except (IllegalTransition, ChannelNotPublishable, ApprovalGateError) as exc:
            raise _non_retryable(exc, type(exc).__name__) from exc

    @activity.defn
    async def run_stage(self, job_id: str, channel_id: str, stage: str) -> StageResult:
        # Placeholder for the specialist agent pools; produces a deterministic fake artifact.
        payload = f"{channel_id}/{job_id}/{stage}".encode()
        cost = STAGE_COST_CENTS.get(stage, 0)
        if cost:
            try:
                await asyncio.to_thread(self._backend.record_cost, job_id, stage, cost, actor=ACTOR)
            except BudgetExceeded as exc:
                raise _non_retryable(exc, "BudgetExceeded") from exc
        if stage == "render":
            # Fake render output, stored content-addressed under the channel's own prefix.
            prefix = await asyncio.to_thread(self._backend.storage_prefix, channel_id)
            digest = await asyncio.to_thread(self._assets.put, prefix, b"FAKE-MP4\0" + payload)
            return StageResult(stage=stage, artifacts={"video": digest})
        return StageResult(stage=stage, artifacts={f"{stage}.out": file_digest(payload)})

    @activity.defn
    async def run_qa(self, job_id: str, channel_id: str) -> QAReport:
        # Placeholder for the QA agent pool: fake report with a low risk score.
        report = QAReport(passed=True, risk_score=10)
        await asyncio.to_thread(
            self._backend.record_qa,
            job_id,
            passed=report.passed,
            risk_score=report.risk_score,
            flags=list(report.flags),
            sources=list(report.sources),
            actor=ACTOR,
        )
        return report

    @activity.defn
    async def seal_package(
        self,
        job_id: str,
        channel_id: str,
        artifacts: dict[str, str],
        approval_timeout_seconds: int = 48 * 3600,
    ) -> str:
        """Freeze the publish package and submit it for human approval."""
        assets = dict(artifacts)
        manifest = {
            "metadata": {
                "job_id": job_id,
                "channel_id": channel_id,
                "title": f"Video {job_id[:8]}",
                "description": "",
                "tags": [],
                "category_id": "22",
                "privacy_status": "private",
                "made_for_kids": False,
                # AI-produced content is always disclosed as synthetic.
                "contains_synthetic_media": True,
            },
            "assets": assets,
        }
        deadline = datetime.now(UTC) + timedelta(seconds=approval_timeout_seconds)
        return await asyncio.to_thread(
            self._backend.submit_for_approval, job_id, manifest, actor=ACTOR, deadline=deadline
        )

    @activity.defn
    async def approve(self, job_id: str, token: str) -> str:
        """Consume the approval token and move the job to approved. Returns the approver."""
        try:
            claims = await asyncio.to_thread(
                self._backend.verify_approval, job_id, token, actor=ACTOR
            )
        except (ApprovalInvalid, ApprovalReplayError, ApprovalGateError) as exc:
            # The approval hub consumes the token and approves before signalling; accept that
            # exact token for the current content, and nothing else.
            recorded = await asyncio.to_thread(self._backend.approval_on_record, job_id, token)
            if recorded is None:
                raise _non_retryable(exc, "ApprovalRejected") from exc
            claims = recorded
        return claims.approver

    @activity.defn
    async def publish(self, job_id: str, token: str) -> PublishOutcome:
        try:
            result = await asyncio.to_thread(self._publisher.publish, job_id, token)
        except UploadInProgress as exc:
            # Retryable: a later attempt sees the finished upload and returns it.
            raise ApplicationError(str(exc), type="UploadInProgress") from exc
        except (PublishRefused, UploadFailed, QuotaExhausted, StoreError) as exc:
            raise _non_retryable(exc, type(exc).__name__) from exc
        return PublishOutcome(result.video_id, result.state.value, result.duplicate)

    @activity.defn
    async def fail(self, job_id: str) -> str:
        """Move the job to failed if it can still fail; return its final state."""
        state = await asyncio.to_thread(self._backend.get_state, job_id)
        if state in TERMINAL_STATES or not can_transition(state, JobState.FAILED):
            return state.value
        await asyncio.to_thread(self._backend.transition, job_id, JobState.FAILED, actor=ACTOR)
        return JobState.FAILED.value

    @activity.defn
    async def request_replan(self, job_id: str, channel_id: str, reason: str) -> None:
        self.replan_requests.append((job_id, channel_id, reason))

    def all(self) -> list[Callable[..., Any]]:
        return [
            self.transition,
            self.run_stage,
            self.run_qa,
            self.seal_package,
            self.approve,
            self.publish,
            self.fail,
            self.request_replan,
        ]
