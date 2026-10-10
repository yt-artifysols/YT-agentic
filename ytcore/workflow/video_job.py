"""VideoJobWorkflow: research -> script -> render -> QA -> human approval -> schedule ->
publish through the YouTube gateway -> live.

Nothing is auto-published. An approval is a signed token that the store verifies with the public
key and consumes once; an invalid token is ignored and the job keeps waiting. Without a valid
approval before the deadline the job is cancelled and a re-plan is requested.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from ytcore.workflow.activities import JobActivities
    from ytcore.workflow.models import ApprovalDecision, VideoJobInput, VideoJobResult
    from ytcore.workflow.states import JobState

_TIMEOUT = timedelta(minutes=10)
_RETRY = RetryPolicy(maximum_attempts=3)
# Publish retries only on UploadInProgress (everything else is non-retryable); spaced out so a
# concurrent upload can finish and the retry returns its result instead of uploading again.
_PUBLISH_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30), backoff_coefficient=2.0, maximum_attempts=8
)


@workflow.defn
class VideoJobWorkflow:
    def __init__(self) -> None:
        self._decision: ApprovalDecision | None = None
        self._kill_reason: str | None = None
        self._state = JobState.PLANNED
        self.rejected_tokens = 0

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

    async def _to(self, inp: VideoJobInput, state: JobState) -> None:
        await workflow.execute_activity_method(
            JobActivities.transition,
            args=[inp.job_id, state.value],
            start_to_close_timeout=_TIMEOUT,
            retry_policy=_RETRY,
        )
        self._state = state

    async def _stage(self, inp: VideoJobInput, stage: str) -> dict[str, str]:
        res = await workflow.execute_activity_method(
            JobActivities.run_stage,
            args=[inp.job_id, inp.channel_id, stage],
            start_to_close_timeout=_TIMEOUT,
            retry_policy=_RETRY,
        )
        return res.artifacts

    async def _end(
        self, inp: VideoJobInput, state: JobState, reason: str, **kw: object
    ) -> VideoJobResult:
        if self._state != state:
            await self._to(inp, state)
        return VideoJobResult(job_id=inp.job_id, final_state=state.value, reason=reason, **kw)  # type: ignore[arg-type]

    async def _fail(self, inp: VideoJobInput, reason: str, **kw: object) -> VideoJobResult:
        final = await workflow.execute_activity_method(
            JobActivities.fail,
            args=[inp.job_id],
            start_to_close_timeout=_TIMEOUT,
            retry_policy=_RETRY,
        )
        self._state = JobState(final)
        return VideoJobResult(job_id=inp.job_id, final_state=final, reason=reason, **kw)  # type: ignore[arg-type]

    async def _killed(self, inp: VideoJobInput) -> VideoJobResult | None:
        if self._kill_reason is None:
            return None
        return await self._end(inp, JobState.CANCELLED, f"killed: {self._kill_reason}")

    async def _produce(self, inp: VideoJobInput) -> dict[str, str] | VideoJobResult:
        artifacts: dict[str, str] = {}
        for state, stage in ((JobState.RESEARCHING, "research"), (JobState.SCRIPTING, "script")):
            if (stopped := await self._killed(inp)) is not None:
                return stopped
            await self._to(inp, state)
            artifacts.update(await self._stage(inp, stage))

        rework = 0
        while True:
            if (stopped := await self._killed(inp)) is not None:
                return stopped
            await self._to(inp, JobState.RENDERING)
            artifacts.update(await self._stage(inp, "render"))
            await self._to(inp, JobState.QA)
            report = await workflow.execute_activity_method(
                JobActivities.run_qa,
                args=[inp.job_id, inp.channel_id],
                start_to_close_timeout=_TIMEOUT,
                retry_policy=_RETRY,
            )
            if report.passed:
                return artifacts
            rework += 1
            if rework > inp.max_qa_rework:
                return await self._end(inp, JobState.FAILED, "QA rework limit reached")
            if report.rework_stage == JobState.SCRIPTING.value:
                await self._to(inp, JobState.SCRIPTING)
                artifacts.update(await self._stage(inp, "script"))

    async def _await_approval(self, inp: VideoJobInput, chash: str) -> str | VideoJobResult:
        """Return a consumed approval token, or the job's final result."""
        deadline = workflow.now() + timedelta(seconds=inp.approval_timeout_seconds)
        while True:
            remaining = deadline - workflow.now()
            try:
                if remaining <= timedelta(0):
                    raise TimeoutError
                await workflow.wait_condition(
                    lambda: self._decision is not None or self._kill_reason is not None,
                    timeout=remaining,
                )
            except TimeoutError:
                # Missed deadline: never auto-publish. Cancel and re-plan.
                result = await self._end(
                    inp,
                    JobState.CANCELLED,
                    "approval deadline missed",
                    content_hash=chash,
                    replanned=True,
                )
                await workflow.execute_activity_method(
                    JobActivities.request_replan,
                    args=[inp.job_id, inp.channel_id, "approval deadline missed"],
                    start_to_close_timeout=_TIMEOUT,
                    retry_policy=_RETRY,
                )
                return result
            if (stopped := await self._killed(inp)) is not None:
                return stopped
            decision, self._decision = self._decision, None
            assert decision is not None
            if not decision.approved or not decision.token:
                return await self._end(
                    inp, JobState.REJECTED, decision.reason or "rejected", content_hash=chash
                )
            try:
                await workflow.execute_activity_method(
                    JobActivities.approve,
                    args=[inp.job_id, decision.token],
                    start_to_close_timeout=_TIMEOUT,
                    retry_policy=_RETRY,
                )
            except ActivityError:
                # Invalid, replayed or mismatched token: ignore it and keep waiting.
                self.rejected_tokens += 1
                continue
            self._state = JobState.APPROVED
            return decision.token

    @workflow.run
    async def run(self, inp: VideoJobInput) -> VideoJobResult:
        try:
            produced = await self._produce(inp)
        except ActivityError as exc:
            return await self._fail(inp, f"production failed: {exc.cause or exc}")
        if isinstance(produced, VideoJobResult):
            return produced

        if (stopped := await self._killed(inp)) is not None:
            return stopped
        chash = await workflow.execute_activity_method(
            JobActivities.seal_package,
            args=[inp.job_id, inp.channel_id, produced],
            start_to_close_timeout=_TIMEOUT,
            retry_policy=_RETRY,
        )
        self._state = JobState.AWAITING_APPROVAL

        approval = await self._await_approval(inp, chash)
        if isinstance(approval, VideoJobResult):
            return approval
        token = approval

        try:
            await self._to(inp, JobState.SCHEDULED)
        except ActivityError as exc:
            # Paused channel/cluster or shadow tier: never publish; cancel and re-plan.
            return await self._end(
                inp,
                JobState.CANCELLED,
                f"not publishable: {exc.cause or exc}",
                content_hash=chash,
                replanned=True,
            )
        if (stopped := await self._killed(inp)) is not None:
            return stopped

        try:
            outcome = await workflow.execute_activity_method(
                JobActivities.publish,
                args=[inp.job_id, token],
                start_to_close_timeout=timedelta(hours=2),
                retry_policy=_PUBLISH_RETRY,
            )
        except ActivityError as exc:
            return await self._fail(inp, f"publish failed: {exc.cause or exc}", content_hash=chash)
        self._state = JobState(outcome.state)
        return VideoJobResult(
            job_id=inp.job_id,
            final_state=outcome.state,
            content_hash=chash,
            video_id=outcome.video_id,
        )
