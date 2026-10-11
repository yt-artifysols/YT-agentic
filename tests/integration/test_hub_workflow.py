"""Exit gate: approve, edit and reject through the hub API drive the real workflow (Temporal
time-skipping) against Postgres, with the right token, signal, approvals row and audit."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.harness import Keys, make_service
from tests.integration.hubkit import Hub, build_hub
from ytcore.approvals.service.signals import TemporalSignaler
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.workflow.activities import JobActivities
from ytcore.workflow.models import VideoJobInput, VideoJobResult
from ytcore.workflow.states import JobState
from ytcore.workflow.video_job import VideoJobWorkflow, workflow_id_for

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]
QUEUE = "hub-it"


class Rig:
    def __init__(self, env: WorkflowEnvironment, database: str, secrets: Any) -> None:
        self.env = env
        keys = Keys()
        self.hub: Hub = build_hub(database, signaler=TemporalSignaler(env.client), keys=keys)
        self.client = FakeYouTubeClient()
        service = make_service(self.hub.store, keys, self.hub.assets, self.client, secrets)
        self.activities = JobActivities(self.hub.store, service, self.hub.assets)

    def worker(self) -> Worker:
        return Worker(
            self.env.client,
            task_queue=QUEUE,
            workflows=[VideoJobWorkflow],
            activities=self.activities.all(),
        )

    async def start(self, channel_id: str) -> tuple[str, WorkflowHandle[Any, VideoJobResult]]:
        job = self.hub.store.create_job(channel_id, "owner", actor="planner")
        handle = await self.env.client.start_workflow(
            VideoJobWorkflow.run,
            VideoJobInput(job_id=job, channel_id=channel_id, origin="owner"),
            id=workflow_id_for(job),
            task_queue=QUEUE,
        )
        return job, handle

    async def awaiting(self, handle: WorkflowHandle[Any, Any], job: str) -> str:
        for _ in range(300):
            if (
                await handle.query(VideoJobWorkflow.state) == JobState.AWAITING_APPROVAL.value
                and self.hub.store.get_state(job) == JobState.AWAITING_APPROVAL
            ):
                head = self.hub.repo.head(job)
                assert head is not None and head.content_hash
                return head.content_hash
            await asyncio.sleep(0.05)
        raise AssertionError("never reached awaiting_approval")


@pytest.fixture
async def rig(database: str, secrets: Any) -> AsyncIterator[Rig]:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield Rig(env, database, secrets)


async def test_approve_in_hub_publishes(rig: Rig, channel: Callable[..., str], sql: SQL) -> None:
    ch = channel()
    async with rig.worker(), rig.hub.client() as c:
        csrf = await rig.hub.register(c, "approver1", "approver")
        job, handle = await rig.start(ch)
        chash = await rig.awaiting(handle, job)
        res = await c.post(f"/api/jobs/{job}/approve", json={"content_hash": chash},
                           headers={"x-csrf-token": csrf})  # fmt: skip
        assert res.status_code == 200, res.text
        result = await handle.result()
    assert result.final_state == JobState.LIVE.value
    jti = res.json()["jti"]
    assert sql("SELECT approver FROM approvals WHERE token_jti = %s", jti) == [("approver1",)]
    assert sql("SELECT approval_jti FROM uploads WHERE job_id = %s", job) == [(jti,)]
    actions = {
        r[0] for r in sql("SELECT action FROM audit_events WHERE subject = %s", f"job:{job}")
    }
    assert {"hub.approve", "approval.consumed", "upload.live"} <= actions


async def test_edit_in_hub_reruns_qa_then_new_content_is_approved(
    rig: Rig, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    async with rig.worker(), rig.hub.client() as c:
        csrf = await rig.hub.register(c, "approver2", "approver")
        hdr = {"x-csrf-token": csrf}
        job, handle = await rig.start(ch)
        first = await rig.awaiting(handle, job)
        qa_before = sql("SELECT count(*) FROM qa_reports WHERE job_id = %s", job)[0][0]
        change = {"content_hash": first, "title": "Sharper title"}
        edit = await c.post(f"/api/jobs/{job}/edit", json=change, headers=hdr)
        assert edit.status_code == 200, edit.text
        second = await rig.awaiting(handle, job)
        assert second == edit.json()["content_hash"] != first
        assert sql("SELECT count(*) FROM qa_reports WHERE job_id = %s", job)[0][0] == qa_before + 1
        assert sql("SELECT count(*) FROM approvals WHERE job_id = %s", job)[0][0] == 0
        stale = await c.post(f"/api/jobs/{job}/approve", json={"content_hash": first}, headers=hdr)
        assert stale.status_code == 409
        ok = await c.post(f"/api/jobs/{job}/approve", json={"content_hash": second}, headers=hdr)
        assert ok.status_code == 200
        result = await handle.result()
    assert result.final_state == JobState.LIVE.value
    assert rig.client.uploads[0][1].title == "Sharper title"
    assert sql("SELECT content_hash FROM uploads WHERE job_id = %s", job) == [(second,)]


async def test_reject_in_hub_reworks_from_stage(
    rig: Rig, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    async with rig.worker(), rig.hub.client() as c:
        csrf = await rig.hub.register(c, "approver3", "approver")
        hdr = {"x-csrf-token": csrf}
        job, handle = await rig.start(ch)
        first = await rig.awaiting(handle, job)
        res = await c.post(f"/api/jobs/{job}/reject",
                           json={"content_hash": first, "reason": "hook is too weak for the topic",
                                 "rework_stage": "render"}, headers=hdr)  # fmt: skip
        assert res.status_code == 200
        await asyncio.sleep(0.2)
        again = await rig.awaiting(handle, job)
        ok = await c.post(f"/api/jobs/{job}/approve", json={"content_hash": again}, headers=hdr)
        assert ok.status_code == 200
        result = await handle.result()
    assert result.final_state == JobState.LIVE.value
    states = [
        r[0] for r in sql("SELECT to_state FROM job_events WHERE job_id = %s ORDER BY id", job)
    ]
    assert states.count("rendering") == 2 and states.count("awaiting_approval") == 2
    assert sql("SELECT rework_stage FROM job_rejections WHERE job_id = %s", job) == [("render",)]
