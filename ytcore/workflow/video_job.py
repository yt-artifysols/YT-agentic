"""VideoJobWorkflow: research -> script -> render -> QA -> human approval -> publish.

Nothing is auto-published. Without an explicit approval before the deadline the job is
cancelled and a re-plan is requested.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from ytcore.workflow.activities import JobActivities
    from ytcore.workflow.models import (
        ApprovalDecision,
        PublishRequest,
        VideoJobInput,
        VideoJobResult,
    )
    from ytcore.workflow.states import JobState

_TIMEOUT = timedelta(minutes=10)
_RETRY = RetryPolicy(maximum_attempts=3)


@workflow.defn
class VideoJobWorkflow:
    def __init__(self) -> None:
        self._decision: ApprovalDecision | None = None
        self._kill_reason: str | None = None
        self._state = JobState.PLANNED

    @workflow.signal
    def submit_approval(self, decision: ApprovalDecision) -> None:
        if self._state == JobState.AWAITING_APPROVAL and self._decision is None:
            self._decision = decision

    @workflow.signal
    def kill(self, reason: str) -> None:
        self._kill_reason = reason

    @workflow.query
    def state(self) -> str:
        return self._state.value

    async def _to(self, inp: VideoJobInput, state: JobState, chash: str | None = None) -> None:
        await workflow.execute_activity_method(
            JobActivities.transition,
            args=[inp.job_id, state.value, chash],
            start_to_close_timeout=_TIMEOUT,
            retry_policy=_RETRY,
        )
        self._state = state

    async def _stop_if_killed(self, inp: VideoJobInput) -> VideoJobResult | None:
        if self._kill_reason is None:
            return None
        await self._to(inp, JobState.CANCELLED)
        return VideoJobResult(job_id=inp.job_id, final_state=JobState.CANCELLED.value)

    @workflow.run
    async def run(self, inp: VideoJobInput) -> VideoJobResult:
        artifacts: dict[str, str] = {}
        for state, stage in ((JobState.RESEARCHING, "research"), (JobState.SCRIPTING, "script")):
            if (stopped := await self._stop_if_killed(inp)) is not None:
                return stopped
            await self._to(inp, state)
            res = await workflow.execute_activity_method(
                JobActivities.run_stage,
                args=[inp.job_id, inp.channel_id, stage],
                start_to_close_timeout=_TIMEOUT,
                retry_policy=_RETRY,
            )
            artifacts.update(res.artifacts)

        rework = 0
        while True:
            if (stopped := await self._stop_if_killed(inp)) is not None:
                return stopped
            await self._to(inp, JobState.RENDERING)
            res = await workflow.execute_activity_method(
                JobActivities.run_stage,
                args=[inp.job_id, inp.channel_id, "render"],
                start_to_close_timeout=_TIMEOUT,
                retry_policy=_RETRY,
            )
            artifacts.update(res.artifacts)
            await self._to(inp, JobState.QA)
            report = await workflow.execute_activity_method(
                JobActivities.run_qa,
                args=[inp.job_id, inp.channel_id],
                start_to_close_timeout=_TIMEOUT,
                retry_policy=_RETRY,
            )
            if report.passed:
                break
            rework += 1
            if rework > inp.max_qa_rework:
                await self._to(inp, JobState.FAILED)
                return VideoJobResult(job_id=inp.job_id, final_state=JobState.FAILED.value)
            if report.rework_stage == JobState.SCRIPTING.value:
                await self._to(inp, JobState.SCRIPTING)
                res = await workflow.execute_activity_method(
                    JobActivities.run_stage,
                    args=[inp.job_id, inp.channel_id, "script"],
                    start_to_close_timeout=_TIMEOUT,
                    retry_policy=_RETRY,
                )
                artifacts.update(res.artifacts)

        chash = await workflow.execute_activity_method(
            JobActivities.compute_content_hash,
            args=[inp.job_id, inp.channel_id, artifacts],
            start_to_close_timeout=_TIMEOUT,
            retry_policy=_RETRY,
        )
        if (stopped := await self._stop_if_killed(inp)) is not None:
            return stopped
        await self._to(inp, JobState.AWAITING_APPROVAL, chash)

        try:
            await workflow.wait_condition(
                lambda: self._decision is not None or self._kill_reason is not None,
                timeout=timedelta(seconds=inp.approval_timeout_seconds),
            )
        except TimeoutError:
            # Missed deadline: never auto-publish. Cancel and re-plan.
            await self._to(inp, JobState.CANCELLED)
            await workflow.execute_activity_method(
                JobActivities.request_replan,
                args=[inp.job_id, inp.channel_id, "approval deadline missed"],
                start_to_close_timeout=_TIMEOUT,
                retry_policy=_RETRY,
            )
            return VideoJobResult(
                job_id=inp.job_id,
                final_state=JobState.CANCELLED.value,
                content_hash=chash,
                replanned=True,
            )

        if (stopped := await self._stop_if_killed(inp)) is not None:
            return stopped
        decision = self._decision
        assert decision is not None
        if not decision.approved or not decision.token:
            await self._to(inp, JobState.REJECTED)
            return VideoJobResult(
                job_id=inp.job_id, final_state=JobState.REJECTED.value, content_hash=chash
            )

        await self._to(inp, JobState.APPROVED)
        await self._to(inp, JobState.PUBLISHING)
        try:
            video_id = await workflow.execute_activity_method(
                JobActivities.publish,
                PublishRequest(inp.job_id, inp.channel_id, chash, decision.token),
                start_to_close_timeout=timedelta(hours=2),
                retry_policy=RetryPolicy(
                    maximum_attempts=3, non_retryable_error_types=["ApprovalError"]
                ),
            )
        except Exception:
            await self._to(inp, JobState.FAILED)
            return VideoJobResult(
                job_id=inp.job_id, final_state=JobState.FAILED.value, content_hash=chash
            )
        await self._to(inp, JobState.PUBLISHED)
        return VideoJobResult(
            job_id=inp.job_id,
            final_state=JobState.PUBLISHED.value,
            content_hash=chash,
            video_id=video_id,
        )
