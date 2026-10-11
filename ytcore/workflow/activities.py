"""Activities for VideoJobWorkflow.

Dependencies are injected: a Backend (ytcore/store/postgres.py in production, a fake in
tests/fakes.py) and a Publisher (the YouTube gateway, ytcore/gateway/service.py). Blocking I/O
runs in a worker thread so the activity event loop stays free.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
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
from ytcore.observability.metrics import observed
from ytcore.scheduling.pools import PoolLimiter
from ytcore.secrets.provider import SecretsError
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
log = logging.getLogger(__name__)
STAGE_COST_CENTS = {"render": 25}
# Fair-share pools per stage; several pools are always taken in this order.
STAGE_POOLS: dict[str, tuple[str, ...]] = {
    "research": ("research",),
    "script": ("script_seo",),
    "render": ("media", "gpu_render"),
    "qa": ("qa",),
}


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


class Production(Protocol):
    """The agent pools (ytcore/agents/pipeline.py). Without one, stages are placeholders."""

    def run_stage(
        self, job_id: str, channel_id: str, stage: str, artifacts: dict[str, str]
    ) -> dict[str, str]: ...

    def run_qa(self, job_id: str, channel_id: str, artifacts: dict[str, str]) -> QAReport: ...

    def build_manifest(
        self, job_id: str, channel_id: str, artifacts: dict[str, str]
    ) -> dict[str, Any]: ...


def _non_retryable(exc: Exception, kind: str) -> ApplicationError:
    return ApplicationError(f"{kind}: {exc}", type=kind, non_retryable=True)


# Production failures that retrying cannot fix: the job fails with this reason.
_PRODUCTION_ERRORS: tuple[type[Exception], ...] = (
    BudgetExceeded,
    StoreError,
    ValueError,
    KeyError,
    LookupError,
    PermissionError,
)


class JobActivities:
    def __init__(
        self,
        backend: Backend,
        publisher: Publisher,
        assets: AssetSink,
        production: Production | None = None,
        replanner: Callable[[str, str], object] | None = None,
        limiter: PoolLimiter | None = None,
        pool_poll_seconds: float = 1.0,
        pool_max_wait_seconds: float = 1500.0,
    ) -> None:
        self._replanner = replanner
        self._limiter = limiter
        self._pool_poll = pool_poll_seconds
        self._pool_max_wait = pool_max_wait_seconds
        self._backend = backend
        self._publisher = publisher
        self._assets = assets
        self._production = production
        self.replan_requests: list[tuple[str, str, str]] = []

    @contextlib.asynccontextmanager
    async def _leased(self, stage: str, job_id: str) -> AsyncIterator[None]:
        """Hold a fair-share lease on the pool(s) a stage uses, in a fixed order (no deadlocks).
        Waits in line (the heartbeat keeps the activity alive); gives up retryably when starved."""
        pools = STAGE_POOLS.get(stage, ())
        if self._limiter is None or not pools:
            yield
            return
        limiter, held = self._limiter, []
        try:
            for pool in pools:
                waited = 0.0
                while not await asyncio.to_thread(limiter.acquire, pool, job_id):
                    if waited >= self._pool_max_wait:
                        raise ApplicationError(f"pool {pool} is busy", type="PoolBusy")
                    await asyncio.sleep(self._pool_poll)
                    waited += self._pool_poll
                held.append(pool)
            yield
        finally:
            for pool in reversed(held):
                await asyncio.to_thread(limiter.release, pool, job_id)

    async def _produce(self, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return await asyncio.to_thread(fn, *args)
        except BudgetExceeded as exc:
            raise _non_retryable(exc, type(exc).__name__) from exc
        except _PRODUCTION_ERRORS as exc:
            raise _non_retryable(exc, "ProductionFailed") from exc
        except Exception as exc:
            if type(exc).__name__ == "AgentError":
                raise _non_retryable(exc, "AgentFailed") from exc
            raise

    @activity.defn
    @observed("transition")
    async def transition(self, job_id: str, to_state: str) -> None:
        try:
            await asyncio.to_thread(
                self._backend.transition, job_id, JobState(to_state), actor=ACTOR
            )
        except (IllegalTransition, ChannelNotPublishable, ApprovalGateError) as exc:
            raise _non_retryable(exc, type(exc).__name__) from exc

    @activity.defn
    @observed("run_stage")
    async def run_stage(
        self, job_id: str, channel_id: str, stage: str, artifacts: dict[str, str] | None = None
    ) -> StageResult:
        async with self._leased(stage, job_id):
            return await self._run_stage(job_id, channel_id, stage, artifacts)

    async def _run_stage(
        self, job_id: str, channel_id: str, stage: str, artifacts: dict[str, str] | None
    ) -> StageResult:
        if self._production is not None:
            produced = await self._produce(
                self._production.run_stage, job_id, channel_id, stage, dict(artifacts or {})
            )
            return StageResult(stage=stage, artifacts=produced)
        # Placeholder when no agent pools are configured; produces a deterministic fake artifact.
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
    @observed("run_qa")
    async def run_qa(
        self, job_id: str, channel_id: str, artifacts: dict[str, str] | None = None
    ) -> QAReport:
        async with self._leased("qa", job_id):
            return await self._run_qa(job_id, channel_id, artifacts)

    async def _run_qa(
        self, job_id: str, channel_id: str, artifacts: dict[str, str] | None
    ) -> QAReport:
        if self._production is not None:
            report: QAReport = await self._produce(
                self._production.run_qa, job_id, channel_id, dict(artifacts or {})
            )
        else:  # placeholder: fake report with a low risk score
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
    @observed("seal_package")
    async def seal_package(
        self,
        job_id: str,
        channel_id: str,
        artifacts: dict[str, str],
        approval_timeout_seconds: int = 48 * 3600,
        approval_deadline_epoch: float | None = None,
    ) -> str:
        """Freeze the publish package and submit it for human approval."""
        deadline = (
            datetime.fromtimestamp(approval_deadline_epoch, tz=UTC)
            if approval_deadline_epoch is not None
            else datetime.now(UTC) + timedelta(seconds=approval_timeout_seconds)
        )
        if self._production is not None:
            built = await self._produce(
                self._production.build_manifest, job_id, channel_id, dict(artifacts)
            )
            return await asyncio.to_thread(
                self._backend.submit_for_approval, job_id, built, actor=ACTOR, deadline=deadline
            )
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
        return await asyncio.to_thread(
            self._backend.submit_for_approval, job_id, manifest, actor=ACTOR, deadline=deadline
        )

    @activity.defn
    @observed("approve")
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
    @observed("publish")
    async def publish(self, job_id: str, token: str) -> PublishOutcome:
        try:
            result = await asyncio.to_thread(self._publisher.publish, job_id, token)
        except UploadInProgress as exc:
            # Retryable: a later attempt sees the finished upload and returns it.
            log.info("publish for job %s waits: %s", job_id, exc)
            raise ApplicationError(str(exc), type="UploadInProgress") from exc
        except (PublishRefused, UploadFailed, QuotaExhausted, StoreError, SecretsError) as exc:
            log.warning("publish for job %s refused: %s: %s", job_id, type(exc).__name__, exc)
            raise _non_retryable(exc, type(exc).__name__) from exc
        except Exception as exc:
            log.warning("publish for job %s failed: %s", job_id, type(exc).__name__)
            raise
        return PublishOutcome(result.video_id, result.state.value, result.duplicate)

    @activity.defn
    @observed("fail")
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
        if self._replanner is not None:
            await asyncio.to_thread(self._replanner, job_id, reason)

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
