from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.fakes import FakeYouTubeClient, InMemoryJobStore
from ytcore.approvals.token import ApprovalSigner, ApprovalVerifier
from ytcore.gateway.youtube import YouTubeGateway
from ytcore.workflow.activities import JobActivities
from ytcore.workflow.models import ApprovalDecision, VideoJobInput, VideoJobResult
from ytcore.workflow.states import JobState
from ytcore.workflow.video_job import VideoJobWorkflow

TASK_QUEUE = "test-video-jobs"


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


class Harness:
    def __init__(self) -> None:
        self.signer = ApprovalSigner(Ed25519PrivateKey.generate())
        self.client = FakeYouTubeClient()
        self.store = InMemoryJobStore()
        self.activities = JobActivities(
            self.store, YouTubeGateway(ApprovalVerifier(self.signer.public_key()), self.client)
        )

    def token(self, job_id: str, channel_id: str, chash: str) -> str:
        token, _ = self.signer.issue(
            job_id=job_id,
            channel_id=channel_id,
            content_hash=chash,
            approver="owner",
            ttl=timedelta(hours=72),
        )
        return token


async def _start(
    env: WorkflowEnvironment, inp: VideoJobInput
) -> WorkflowHandle[VideoJobWorkflow, VideoJobResult]:
    return await env.client.start_workflow(
        VideoJobWorkflow.run, inp, id=f"job-{inp.job_id}", task_queue=TASK_QUEUE
    )


async def _await_state(
    handle: WorkflowHandle[VideoJobWorkflow, VideoJobResult], state: str
) -> None:
    for _ in range(200):
        if await handle.query(VideoJobWorkflow.state) == state:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"workflow never reached {state}")


async def test_missed_deadline_cancels_and_replans_never_publishes(
    env: WorkflowEnvironment,
) -> None:
    h = Harness()
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[VideoJobWorkflow],
        activities=h.activities.all(),
    ):
        inp = VideoJobInput(
            job_id=str(uuid.uuid4()),
            channel_id="chan-a",
            origin="trigger",
            approval_timeout_seconds=3600,
        )
        result = await (await _start(env, inp)).result()
    assert result.final_state == JobState.CANCELLED.value
    assert result.replanned
    assert h.client.uploads == []
    assert h.activities.replan_requests and h.activities.replan_requests[0][0] == inp.job_id


async def test_approved_job_publishes_once(env: WorkflowEnvironment) -> None:
    h = Harness()
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[VideoJobWorkflow],
        activities=h.activities.all(),
    ):
        inp = VideoJobInput(job_id=str(uuid.uuid4()), channel_id="chan-a", origin="owner")
        handle = await _start(env, inp)
        await _await_state(handle, JobState.AWAITING_APPROVAL.value)
        chash = h.store.hashes[inp.job_id]
        assert chash is not None
        await handle.signal(
            VideoJobWorkflow.submit_approval,
            ApprovalDecision(True, "owner", h.token(inp.job_id, inp.channel_id, chash)),
        )
        result = await handle.result()
    assert result.final_state == JobState.PUBLISHED.value
    assert h.client.uploads == [("chan-a", chash)]


async def test_token_for_other_content_fails_without_upload(env: WorkflowEnvironment) -> None:
    h = Harness()
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[VideoJobWorkflow],
        activities=h.activities.all(),
    ):
        inp = VideoJobInput(
            job_id=str(uuid.uuid4()), channel_id="chan-a", origin="derived", parent_job_id="parent"
        )
        handle = await _start(env, inp)
        await _await_state(handle, JobState.AWAITING_APPROVAL.value)
        forged = h.token(inp.job_id, inp.channel_id, "sha256:" + "0" * 64)
        await handle.signal(VideoJobWorkflow.submit_approval, ApprovalDecision(True, "x", forged))
        result = await handle.result()
    assert result.final_state == JobState.FAILED.value
    assert h.client.uploads == []


async def test_rejection_and_missing_token_do_not_publish(env: WorkflowEnvironment) -> None:
    h = Harness()
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[VideoJobWorkflow],
        activities=h.activities.all(),
    ):
        for decision in (ApprovalDecision(False, "owner"), ApprovalDecision(True, "owner", None)):
            inp = VideoJobInput(job_id=str(uuid.uuid4()), channel_id="chan-b", origin="experiment")
            handle = await _start(env, inp)
            await _await_state(handle, JobState.AWAITING_APPROVAL.value)
            await handle.signal(VideoJobWorkflow.submit_approval, decision)
            assert (await handle.result()).final_state == JobState.REJECTED.value
    assert h.client.uploads == []


async def test_kill_switch_cancels(env: WorkflowEnvironment) -> None:
    h = Harness()
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[VideoJobWorkflow],
        activities=h.activities.all(),
    ):
        inp = VideoJobInput(job_id=str(uuid.uuid4()), channel_id="chan-a", origin="planner")
        handle = await _start(env, inp)
        await _await_state(handle, JobState.AWAITING_APPROVAL.value)
        await handle.signal(VideoJobWorkflow.kill, "operator kill switch")
        result = await handle.result()
    assert result.final_state == JobState.CANCELLED.value
    assert h.client.uploads == []
