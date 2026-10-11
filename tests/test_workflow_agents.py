"""VideoJobWorkflow with the agent pools (fake providers, in-memory stores, time-skipping)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.agentkit import Kit
from tests.fakes import FakeBackend, FakeYouTubeClient, fake_secrets
from tests.harness import Keys, make_service
from ytcore.approvals.hashing import content_hash
from ytcore.workflow.activities import JobActivities
from ytcore.workflow.models import ApprovalDecision, HubDecision, VideoJobInput, VideoJobResult
from ytcore.workflow.states import JobState
from ytcore.workflow.video_job import VideoJobWorkflow

QUEUE = "agents-test"
Handle = WorkflowHandle[VideoJobWorkflow, VideoJobResult]


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


class AgentHarness:
    def __init__(self, *, tier: str = "shadow", **budgets: int) -> None:
        self.keys = Keys()
        self.kit = Kit()
        self.chan = self.kit.channel("chan-a", autonomy_tier=tier, **budgets)
        self.backend = FakeBackend(self.keys.verifier)
        self.backend.add_channel("chan-a", autonomy_tier=tier)
        self.client = FakeYouTubeClient()
        service = make_service(
            self.backend, self.keys, self.kit.assets, self.client, fake_secrets()
        )
        self.activities = JobActivities(self.backend, service, self.kit.assets, self.kit.p)

    def worker(self, env: WorkflowEnvironment) -> Worker:
        return Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[VideoJobWorkflow],
            activities=self.activities.all(),
        )

    async def start(self, env: WorkflowEnvironment, **kw: Any) -> tuple[Handle, VideoJobInput]:
        job_id = self.backend.create_job(self.chan)
        self.kit.store.add_job(job_id, self.chan)
        inp = VideoJobInput(job_id=job_id, channel_id=self.chan, origin="owner", **kw)
        handle = await env.client.start_workflow(
            VideoJobWorkflow.run, inp, id=f"job-{job_id}", task_queue=QUEUE
        )
        return handle, inp


async def _await_state(handle: Handle, state: JobState) -> None:
    for _ in range(400):
        if await handle.query(VideoJobWorkflow.state) == state.value:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"workflow never reached {state}")


async def test_shadow_job_reaches_awaiting_approval_and_never_publishes(
    env: WorkflowEnvironment,
) -> None:
    h = AgentHarness()
    idea = h.kit.idea(h.chan, "Why index funds beat stock picking")
    async with h.worker(env):
        handle, inp = await h.start(env)
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        job = h.backend.jobs[inp.job_id]
        assert job.manifest is not None and job.content_hash == content_hash(job.manifest)
        assert job.manifest["provenance"]["owner_inputs"] == [idea]
        assert job.manifest["metadata"]["title"].startswith("Why index funds")
        # Even a valid approval cannot publish on a shadow channel.
        token = h.keys.token(inp.job_id, inp.channel_id, job.content_hash or "")
        await handle.signal(
            VideoJobWorkflow.submit_approval, ApprovalDecision(True, "owner", token)
        )
        result = await handle.result()
    assert result.final_state == JobState.CANCELLED.value and "not publishable" in result.reason
    assert h.client.uploads == []
    assert h.backend.qa_reports[0][1] is True


async def test_gated_job_publishes_the_agent_package_after_approval(
    env: WorkflowEnvironment,
) -> None:
    h = AgentHarness(tier="gated")
    h.kit.idea(h.chan, "Index funds in five minutes")
    async with h.worker(env):
        handle, inp = await h.start(env)
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        chash = h.backend.jobs[inp.job_id].content_hash or ""
        await handle.signal(
            VideoJobWorkflow.submit_approval,
            ApprovalDecision(True, "owner", h.keys.token(inp.job_id, h.chan, chash)),
        )
        result = await handle.result()
    assert result.final_state == JobState.LIVE.value and len(h.client.uploads) == 1


async def test_hub_rework_reruns_the_script_with_the_reviewer_notes(
    env: WorkflowEnvironment,
) -> None:
    h = AgentHarness()
    h.kit.idea(h.chan, "Index funds explained")
    async with h.worker(env):
        handle, inp = await h.start(env, approval_timeout_seconds=3600)
        await _await_state(handle, JobState.AWAITING_APPROVAL)
        first = h.backend.jobs[inp.job_id].content_hash or ""
        h.kit.store.rejections[inp.job_id] = "Tone is too dry, add an example"
        await handle.signal(
            VideoJobWorkflow.hub_decision,
            HubDecision(
                "reject", "approver", first, rework_stage="script", reason="Tone is too dry"
            ),
        )
        for _ in range(400):
            if h.backend.jobs[inp.job_id].content_hash not in (None, first) and (
                h.backend.jobs[inp.job_id].state == JobState.AWAITING_APPROVAL
            ):
                break
            await asyncio.sleep(0.05)
        await handle.signal(VideoJobWorkflow.kill, "test done")
        await handle.result()
    scripts = [r for r in h.kit.llm.requests if r.task == "script_seo.write"]
    assert "reviewer: Tone is too dry, add an example" in scripts[-1].user


async def test_budget_cap_fails_the_job_with_a_clear_reason(env: WorkflowEnvironment) -> None:
    h = AgentHarness(job_budget_cents=3)
    h.kit.idea(h.chan, "Index funds")
    async with h.worker(env):
        handle, _ = await h.start(env)
        result = await handle.result()
    assert result.final_state == JobState.FAILED.value
    assert "JobBudgetExceeded" in result.reason and "job budget exceeded" in result.reason
    assert h.kit.store.alerts[0][2] == "budget.job"
    assert h.client.uploads == []
