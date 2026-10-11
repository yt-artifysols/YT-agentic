"""Cluster supervisor: fan-out with a concurrency cap, deadline priority, budget hold, health."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.fakes import FakeBackend, FakeYouTubeClient, InMemoryAssetSource, fake_secrets
from tests.harness import Keys, make_service
from ytcore.workflow.activities import JobActivities
from ytcore.workflow.models import ApprovalDecision
from ytcore.workflow.states import JobState
from ytcore.workflow.supervisor import (
    ClusterActivities,
    ClusterHealth,
    ClusterLimits,
    ClusterSupervisorWorkflow,
    JobSpec,
    SupervisorInput,
    SupervisorResult,
)
from ytcore.workflow.video_job import VideoJobWorkflow

QUEUE = "supervisor-test"


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


class Rig:
    def __init__(self, max_concurrent: int = 3) -> None:
        self.keys = Keys()
        self.backend = FakeBackend(self.keys.verifier, daily_quota_units=1_000_000)
        self.backend.add_channel("chan-a")
        self.client = FakeYouTubeClient()
        assets = InMemoryAssetSource()
        service = make_service(self.backend, self.keys, assets, self.client, fake_secrets())
        self.jobs = JobActivities(self.backend, service, assets)
        self.limits = ClusterLimits(max_concurrent, None)
        self.reports: list[ClusterHealth] = []
        self.cluster = ClusterActivities(lambda _c: self.limits, self.reports.append)

    def worker(self, env: WorkflowEnvironment) -> Worker:
        return Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[ClusterSupervisorWorkflow, VideoJobWorkflow],
            activities=[*self.jobs.all(), *self.cluster.all()],
        )

    def spec(self, hours_to_deadline: float) -> JobSpec:
        job = self.backend.create_job("chan-a")
        deadline = datetime.now(UTC) + timedelta(hours=hours_to_deadline)
        return JobSpec(job, "chan-a", deadline.timestamp(), int(hours_to_deadline * 3600))

    async def approve(self, env: WorkflowEnvironment, job: str) -> None:
        chash = self.backend.jobs[job].content_hash or ""
        handle = env.client.get_workflow_handle(f"job-{job}")
        await handle.signal(
            VideoJobWorkflow.submit_approval,
            ApprovalDecision(True, "owner", self.keys.token(job, "chan-a", chash)),
        )

    def awaiting(self) -> set[str]:
        return {j for j, r in self.backend.jobs.items() if r.state == JobState.AWAITING_APPROVAL}


async def _until(cond, what: str, timeout: float = 20) -> None:  # type: ignore[no-untyped-def]
    for _ in range(int(timeout / 0.1)):
        if cond():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


async def test_the_cluster_cap_holds_and_the_most_urgent_jobs_go_first(
    env: WorkflowEnvironment,
) -> None:
    rig = Rig(max_concurrent=3)
    async with rig.worker(env):
        sup = await env.client.start_workflow(
            ClusterSupervisorWorkflow.run,
            SupervisorInput("cluster-a", idle_seconds=120),
            id="cluster-cluster-a",
            task_queue=QUEUE,
        )
        specs = [rig.spec(h) for h in (30, 5, 20, 2, 40, 10, 8, 3)]
        for spec in specs:
            await sup.signal(ClusterSupervisorWorkflow.enqueue, spec)
        await sup.signal(ClusterSupervisorWorkflow.enqueue, specs[0])  # duplicate: ignored
        by_urgency = [s.job_id for s in sorted(specs, key=lambda s: s.approval_deadline_epoch or 0)]

        await _until(lambda: len(rig.awaiting()) == 3, "three jobs to start")
        await asyncio.sleep(0.5)
        assert rig.awaiting() == set(by_urgency[:3])  # deadlines 2h, 3h, 5h
        health = await sup.query(ClusterSupervisorWorkflow.health)
        assert (health.running, health.pending) == (3, 5)
        assert all(r.state == JobState.PLANNED for j, r in rig.backend.jobs.items()
                   if j in by_urgency[3:])  # fmt: skip

        # Approving one frees a slot: the next most urgent job starts, never more than 3 at once.
        await rig.approve(env, by_urgency[0])
        await _until(lambda: by_urgency[3] in rig.awaiting(), "the next job to start")
        assert len(rig.awaiting()) <= 3

        for job in by_urgency[1:]:
            await _until(lambda job=job: job in rig.awaiting(), f"{job} to wait for approval")
            await rig.approve(env, job)
        result: SupervisorResult = await asyncio.wait_for(sup.result(), 60)
    assert result.started == 8 and result.outcomes == {"live": 8}
    assert len(rig.client.uploads) == 8
    assert rig.reports and rig.reports[-1].finished == 8 and rig.reports[-1].pending == 0


async def test_a_spent_cluster_budget_holds_new_jobs_until_it_frees_up(
    env: WorkflowEnvironment,
) -> None:
    rig = Rig()
    rig.limits = ClusterLimits(3, 0)  # budget used up
    async with rig.worker(env):
        sup = await env.client.start_workflow(
            ClusterSupervisorWorkflow.run,
            SupervisorInput("cluster-a", idle_seconds=120, recheck_seconds=1),
            id="cluster-held",
            task_queue=QUEUE,
        )
        spec = rig.spec(10)
        await sup.signal(ClusterSupervisorWorkflow.enqueue, spec)
        await asyncio.sleep(1.5)
        assert rig.backend.jobs[spec.job_id].state == JobState.PLANNED
        health = await sup.query(ClusterSupervisorWorkflow.health)
        assert health.budget_held and health.pending == 1

        rig.limits = ClusterLimits(3, 500)  # next day's budget
        await _until(lambda: spec.job_id in rig.awaiting(), "the held job to start", timeout=30)
        await rig.approve(env, spec.job_id)
        result = await asyncio.wait_for(sup.result(), 60)
    assert result.outcomes == {"live": 1}


async def test_a_paused_cluster_starts_nothing(env: WorkflowEnvironment) -> None:
    rig = Rig()
    rig.limits = ClusterLimits(3, None, paused=True)
    async with rig.worker(env):
        sup = await env.client.start_workflow(
            ClusterSupervisorWorkflow.run,
            SupervisorInput("cluster-a", idle_seconds=120, recheck_seconds=1),
            id="cluster-paused",
            task_queue=QUEUE,
        )
        spec = rig.spec(10)
        await sup.signal(ClusterSupervisorWorkflow.enqueue, spec)
        await asyncio.sleep(1.5)
        assert rig.backend.jobs[spec.job_id].state == JobState.PLANNED
        assert (await sup.query(ClusterSupervisorWorkflow.health)).paused
        await sup.cancel()
