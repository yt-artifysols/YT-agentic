"""Temporal time-skipping tests for VideoJobWorkflow with the fake backend and fake YouTube."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.fakes import FakeBackend, FakeYouTubeClient, InMemoryAssetSource, fake_secrets
from tests.harness import Keys, make_service
from ytcore.workflow.activities import JobActivities
from ytcore.workflow.models import ApprovalDecision, VideoJobInput, VideoJobResult
from ytcore.workflow.states import JobState
from ytcore.workflow.video_job import VideoJobWorkflow

TASK_QUEUE = "test-video-jobs"
Handle = WorkflowHandle[VideoJobWorkflow, VideoJobResult]


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


class Harness:
    def __init__(self, **channel: Any) -> None:
        self.keys = Keys()
        self.client = FakeYouTubeClient()
        self.assets = InMemoryAssetSource()
        self.backend = FakeBackend(self.keys.verifier)
        self.backend.add_channel("chan-a", **channel)
        service = make_service(self.backend, self.keys, self.assets, self.client, fake_secrets())
        self.activities = JobActivities(self.backend, service, self.assets)

    def worker(self, env: WorkflowEnvironment) -> Worker:
        return Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[VideoJobWorkflow],
            activities=self.activities.all(),
        )

    async def start(self, env: WorkflowEnvironment, **kw: Any) -> tuple[Handle, VideoJobInput]:
        job_id = self.backend.create_job("chan-a")
        inp = VideoJobInput(
            job_id=job_id, channel_id="chan-a", origin=kw.pop("origin", "owner"), **kw
        )
        handle = await env.client.start_workflow(
            VideoJobWorkflow.run, inp, id=f"job-{job_id}", task_queue=TASK_QUEUE
        )
        return handle, inp

    def token(self, inp: VideoJobInput, chash: str | None = None) -> str:
        return self.keys.token(
            inp.job_id, inp.channel_id, chash or self.backend.jobs[inp.job_id].content_hash or ""
        )


async def _await_state(handle: Handle, state: JobState) -> None:
    for _ in range(200):
        if await handle.query(VideoJobWorkflow.state) == state.value:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"workflow never reached {state}")


async def test_approved_job_goes_live_once(env: WorkflowEnvironment) -> None:
    h = Harness()
    async with h.worker(env):
        handle, inp = await h.start(env)
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        await handle.signal(
            VideoJobWorkflow.submit_approval, ApprovalDecision(True, "owner", h.token(inp))
        )
        result = await handle.result()
    assert result.final_state == JobState.LIVE.value
    assert [u[0] for u in h.client.uploads] == [result.video_id]
    states = [s for j, s, _ in h.backend.events if j == inp.job_id]
    assert states == [
        "planned", "researching", "scripting", "rendering", "qa", "awaiting_approval",
        "approved", "scheduled", "publishing", "published", "live",
    ]  # fmt: skip
    assert h.backend.jobs[inp.job_id].render_cost_cents > 0


async def test_missed_deadline_cancels_and_replans_never_publishes(
    env: WorkflowEnvironment,
) -> None:
    h = Harness()
    async with h.worker(env):
        handle, inp = await h.start(env, origin="trigger", approval_timeout_seconds=3600)
        result = await handle.result()
    assert result.final_state == JobState.CANCELLED.value and result.replanned
    assert h.client.uploads == []
    assert h.activities.replan_requests[0][0] == inp.job_id


async def test_invalid_token_is_ignored_then_deadline_cancels(env: WorkflowEnvironment) -> None:
    h = Harness()
    async with h.worker(env):
        handle, inp = await h.start(
            env, origin="derived", parent_job_id="parent", approval_timeout_seconds=3600
        )
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        forged = h.token(inp, "sha256:" + "0" * 64)
        await handle.signal(VideoJobWorkflow.submit_approval, ApprovalDecision(True, "x", forged))
        result = await handle.result()
    assert result.final_state == JobState.CANCELLED.value and result.replanned
    assert h.client.uploads == []


async def test_valid_token_after_invalid_one_still_publishes(env: WorkflowEnvironment) -> None:
    h = Harness()
    async with h.worker(env):
        handle, inp = await h.start(env, origin="experiment")
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        await handle.signal(
            VideoJobWorkflow.submit_approval, ApprovalDecision(True, "x", "garbage.token")
        )
        await asyncio.sleep(0.3)
        await handle.signal(
            VideoJobWorkflow.submit_approval, ApprovalDecision(True, "owner", h.token(inp))
        )
        result = await handle.result()
    assert result.final_state == JobState.LIVE.value


async def test_rejection_and_missing_token_do_not_publish(env: WorkflowEnvironment) -> None:
    h = Harness()
    async with h.worker(env):
        for decision in (ApprovalDecision(False, "owner"), ApprovalDecision(True, "owner", None)):
            handle, _ = await h.start(env, origin="experiment")
            await _await_state(handle, JobState.AWAITING_APPROVAL)
            await handle.signal(VideoJobWorkflow.submit_approval, decision)
            assert (await handle.result()).final_state == JobState.REJECTED.value
    assert h.client.uploads == []


async def test_shadow_channel_never_publishes(env: WorkflowEnvironment) -> None:
    h = Harness(autonomy_tier="shadow")
    async with h.worker(env):
        handle, inp = await h.start(env)
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        await handle.signal(
            VideoJobWorkflow.submit_approval, ApprovalDecision(True, "owner", h.token(inp))
        )
        result = await handle.result()
    assert result.final_state == JobState.CANCELLED.value
    assert "not publishable" in result.reason
    assert h.client.uploads == []


async def test_budget_exceeded_fails_job(env: WorkflowEnvironment) -> None:
    h = Harness(daily_budget_cents=0)
    async with h.worker(env):
        handle, _ = await h.start(env)
        result = await handle.result()
    assert result.final_state == JobState.FAILED.value
    assert "BudgetExceeded" in result.reason
    assert h.client.uploads == []


async def test_kill_switch_cancels(env: WorkflowEnvironment) -> None:
    h = Harness()
    async with h.worker(env):
        handle, _ = await h.start(env, origin="planner")
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        await handle.signal(VideoJobWorkflow.kill, "operator kill switch")
        result = await handle.result()
    assert result.final_state == JobState.CANCELLED.value
    assert h.client.uploads == []


# ----------------------------------------------------------------- approval hub decisions


async def test_hub_approval_is_accepted_once_recorded(env: WorkflowEnvironment) -> None:
    from ytcore.workflow.models import HubDecision

    h = Harness()
    async with h.worker(env):
        handle, inp = await h.start(env)
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        token = h.token(inp)
        h.backend.verify_approval(inp.job_id, token, actor="hub:approver1")  # hub's DB write
        chash = h.backend.jobs[inp.job_id].content_hash or ""
        await handle.signal(
            VideoJobWorkflow.hub_decision, HubDecision("approve", "approver1", chash, token=token)
        )
        result = await handle.result()
    assert result.final_state == JobState.LIVE.value
    assert len(h.client.uploads) == 1


async def test_hub_approve_without_db_record_is_ignored(env: WorkflowEnvironment) -> None:
    from ytcore.workflow.models import HubDecision

    h = Harness()
    async with h.worker(env):
        handle, inp = await h.start(env, approval_timeout_seconds=3600)
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        forged = h.token(inp, "sha256:" + "1" * 64)
        await handle.signal(
            VideoJobWorkflow.hub_decision, HubDecision("approve", "x", "sha256:" + "1" * 64, forged)
        )
        result = await handle.result()
    assert result.final_state == JobState.CANCELLED.value and result.replanned
    assert h.client.uploads == []


async def test_hub_edit_reruns_qa_and_never_approves(env: WorkflowEnvironment) -> None:
    from ytcore.approvals.hashing import content_hash
    from ytcore.workflow.models import HubDecision

    h = Harness()
    async with h.worker(env):
        handle, inp = await h.start(env, approval_timeout_seconds=3600)
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        job = h.backend.jobs[inp.job_id]
        qa_before = len(h.backend.qa_reports)
        # What the hub does in the DB: new manifest version, new hash, job back to qa.
        manifest = dict(job.manifest or {})
        manifest["metadata"] = {**manifest["metadata"], "title": "Edited title"}
        h.backend.transition(inp.job_id, JobState.QA, actor="hub:approver1")
        job.manifest, job.content_hash = manifest, content_hash(manifest)
        await handle.signal(
            VideoJobWorkflow.hub_decision, HubDecision("edit", "approver1", job.content_hash)
        )
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        for _ in range(100):
            if h.backend.get_state(inp.job_id) == JobState.AWAITING_APPROVAL:
                break
            await asyncio.sleep(0.05)
        assert len(h.backend.qa_reports) == qa_before + 1  # QA re-ran on the edit
        assert h.backend.approvals == {}  # the edit approved nothing
        token = h.token(inp)  # approve the EDITED content
        h.backend.verify_approval(inp.job_id, token, actor="hub:approver1")
        await handle.signal(
            VideoJobWorkflow.hub_decision,
            HubDecision("approve", "approver1", job.content_hash, token=token),
        )
        result = await handle.result()
    assert result.final_state == JobState.LIVE.value
    assert result.content_hash == job.content_hash
    assert h.client.uploads[0][1].title == "Edited title"


async def test_hub_reject_for_rework_reproduces_then_waits_again(
    env: WorkflowEnvironment,
) -> None:
    from ytcore.workflow.models import HubDecision

    h = Harness()
    async with h.worker(env):
        handle, inp = await h.start(env, approval_timeout_seconds=3600)
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        chash = h.backend.jobs[inp.job_id].content_hash or ""
        await handle.signal(
            VideoJobWorkflow.hub_decision,
            HubDecision("reject", "approver1", chash, rework_stage="script", reason="weak hook"),
        )
        result = await handle.result()  # re-produced, then the deadline passes unapproved
    states = [s for j, s, _ in h.backend.events if j == inp.job_id]
    assert states.count("scripting") == 2 and states.count("awaiting_approval") == 2
    assert result.final_state == JobState.CANCELLED.value
    assert h.client.uploads == []


async def test_hub_reject_discard_is_final(env: WorkflowEnvironment) -> None:
    from ytcore.workflow.models import HubDecision

    h = Harness()
    async with h.worker(env):
        handle, inp = await h.start(env)
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        chash = h.backend.jobs[inp.job_id].content_hash or ""
        await handle.signal(
            VideoJobWorkflow.hub_decision,
            HubDecision("reject", "approver1", chash, rework_stage="discard", reason="off brand"),
        )
        result = await handle.result()
    assert result.final_state == JobState.REJECTED.value
    assert h.client.uploads == []


async def test_forged_hub_decision_and_attacker_token_never_publish(
    env: WorkflowEnvironment,
) -> None:
    """Anyone who can signal the workflow still cannot publish: approval is re-verified against
    the database record, not trusted from the signal."""
    from tests.redteam.rig import Rig
    from ytcore.workflow.models import HubDecision

    h = Harness()
    async with h.worker(env):
        handle, inp = await h.start(env, origin="trigger", approval_timeout_seconds=3600)
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        chash = h.backend.jobs[inp.job_id].content_hash or ""
        attacker = Rig.attacker_token(inp.job_id, inp.channel_id, chash)
        await handle.signal(
            VideoJobWorkflow.submit_approval, ApprovalDecision(True, "mallory", attacker)
        )
        await handle.signal(
            VideoJobWorkflow.hub_decision, HubDecision("approve", "mallory", chash, attacker)
        )
        # A validly signed token that the hub never recorded is no approval either.
        unrecorded = h.token(inp)
        await handle.signal(
            VideoJobWorkflow.hub_decision, HubDecision("approve", "hub", chash, unrecorded)
        )
        result = await handle.result()
    assert result.final_state == JobState.CANCELLED.value
    assert h.client.uploads == []


async def test_approval_arriving_before_the_workflow_notices_awaiting_state_is_not_lost(
    env: WorkflowEnvironment,
) -> None:
    """The database reaches awaiting_approval inside seal_package, slightly before the workflow
    knows. A decision sent in that gap used to be dropped and the job waited for nothing."""
    h = Harness()
    loop = asyncio.get_running_loop()
    holder: dict[str, Any] = {}
    original = h.backend.submit_for_approval

    def submit_then_approve_immediately(job_id: str, manifest: Any, **kw: Any) -> str:
        chash = original(job_id, manifest, **kw)
        token = h.keys.token(job_id, "chan-a", chash)
        decision = ApprovalDecision(True, "owner", token)
        asyncio.run_coroutine_threadsafe(
            holder["handle"].signal(VideoJobWorkflow.submit_approval, decision), loop
        ).result(timeout=30)
        return chash

    h.backend.submit_for_approval = submit_then_approve_immediately  # type: ignore[method-assign]
    async with h.worker(env):
        job_id = h.backend.create_job("chan-a")
        from ytcore.workflow.models import VideoJobInput

        handle = await env.client.start_workflow(
            VideoJobWorkflow.run,
            VideoJobInput(job_id=job_id, channel_id="chan-a", origin="owner"),
            id=f"job-{job_id}",
            task_queue=TASK_QUEUE,
        )
        holder["handle"] = handle
        result = await handle.result()
    assert result.final_state == JobState.LIVE.value
    assert len(h.client.uploads) == 1


async def test_a_buffered_decision_for_stale_content_is_ignored(env: WorkflowEnvironment) -> None:
    from ytcore.workflow.models import HubDecision

    h = Harness()
    async with h.worker(env):
        handle, _ = await h.start(env, approval_timeout_seconds=3600)
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        stale = "sha256:" + "2" * 64
        await handle.signal(
            VideoJobWorkflow.hub_decision, HubDecision("reject", "x", stale, None, "discard", "no")
        )
        result = await handle.result()
    # The reject was about other content, so it did not end the job; the deadline did.
    assert result.final_state == JobState.CANCELLED.value and result.replanned


async def test_the_planned_deadline_is_absolute_and_does_not_slide_with_slow_production(
    env: WorkflowEnvironment,
) -> None:
    from datetime import UTC, datetime, timedelta

    h = Harness()
    deadline = datetime.now(UTC) + timedelta(hours=1)
    async with h.worker(env):
        job_id = h.backend.create_job("chan-a")
        from ytcore.workflow.models import VideoJobInput

        handle = await env.client.start_workflow(
            VideoJobWorkflow.run,
            VideoJobInput(
                job_id=job_id,
                channel_id="chan-a",
                origin="planner",
                approval_timeout_seconds=48 * 3600,  # would be 2 days if it slid
                approval_deadline_epoch=deadline.timestamp(),
            ),
            id=f"job-{job_id}",
            task_queue=TASK_QUEUE,
        )
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        result = await handle.result()
        desc = await handle.describe()
    assert result.final_state == JobState.CANCELLED.value and result.replanned
    assert abs((h.backend.jobs[job_id].approval_deadline - deadline).total_seconds()) < 1  # type: ignore[operator]
    assert desc.close_time is not None and desc.start_time is not None
    # cancelled at the planned deadline (1h), not 48h after sealing
    assert desc.close_time - desc.start_time < timedelta(hours=3)


async def test_a_package_that_cannot_be_approved_in_time_is_never_sealed(
    env: WorkflowEnvironment,
) -> None:
    from datetime import UTC, datetime, timedelta

    from ytcore.workflow.models import VideoJobInput

    h = Harness()
    async with h.worker(env):
        job_id = h.backend.create_job("chan-a")
        handle = await env.client.start_workflow(
            VideoJobWorkflow.run,
            VideoJobInput(
                job_id=job_id,
                channel_id="chan-a",
                origin="planner",
                approval_deadline_epoch=(datetime.now(UTC) - timedelta(minutes=1)).timestamp(),
            ),
            id=f"job-{job_id}",
            task_queue=TASK_QUEUE,
        )
        result = await handle.result()
    assert result.final_state == JobState.CANCELLED.value and result.replanned
    assert h.backend.jobs[job_id].manifest is None  # never submitted for approval
    assert h.client.uploads == []
