"""The gateway as its own service: the worker publishes only through authenticated HTTP, holds
no YouTube credentials, and connects to Postgres as a role that cannot approve or upload."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.harness import Keys, drive_to_awaiting, make_service
from tests.integration.test_roles import as_role
from ytcore.gateway.assets import InMemoryAssetSource
from ytcore.gateway.auth import StaticBearerAuthenticator
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.gateway.remote import RemoteGateway
from ytcore.gateway.server import create_gateway_app
from ytcore.gateway.service import PublishRefused
from ytcore.store.errors import QuotaExhausted
from ytcore.store.postgres import PostgresStore
from ytcore.workflow.activities import JobActivities
from ytcore.workflow.models import ApprovalDecision, VideoJobInput
from ytcore.workflow.states import JobState
from ytcore.workflow.video_job import VideoJobWorkflow

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]
TOKEN = "gateway-test-token-0123456789"


class Rig:
    def __init__(self, database: str, keys: Keys, secrets: Any) -> None:
        self.keys = keys
        self.assets, self.client = InMemoryAssetSource(), FakeYouTubeClient()
        self.hub = PostgresStore.from_conninfo(as_role(database, "yt_hub"), keys.verifier)
        self.worker = PostgresStore.from_conninfo(as_role(database, "yt_worker"), keys.verifier)
        self.ops = PostgresStore.from_conninfo(as_role(database, "yt_ops"), keys.verifier)
        gateway_store = PostgresStore.from_conninfo(as_role(database, "yt_gateway"), keys.verifier)
        service = make_service(gateway_store, keys, self.assets, self.client, secrets)
        self.app = create_gateway_app(service, StaticBearerAuthenticator(TOKEN))
        self.http = TestClient(self.app)
        self.remote = RemoteGateway("http://gateway.test", lambda: TOKEN, client=self.http)

    def scheduled(self, ch: str) -> tuple[str, str]:
        job = self.ops.create_job(ch, "owner", actor="planner")
        chash = drive_to_awaiting(self.worker, self.assets, job, ch)
        token = self.keys.token(job, ch, chash)
        self.hub.verify_approval(job, token, actor="hub:alice")
        self.worker.transition(job, JobState.SCHEDULED, actor="workflow")
        return job, token


@pytest.fixture
def rig(database: str, keys: Keys, secrets: Any) -> Rig:
    return Rig(database, keys, secrets)


def test_publish_through_the_gateway_service(rig: Rig, channel: Callable[..., str]) -> None:
    job, token = rig.scheduled(channel())
    result = rig.remote.publish(job, token)
    assert result.state in (JobState.PUBLISHED, JobState.LIVE) and not result.duplicate
    again = rig.remote.publish(job, token)  # retried by the workflow: same video, no second upload
    assert again.duplicate and again.video_id == result.video_id
    assert len(rig.client.uploads) == 1


def test_calls_without_the_service_identity_are_refused(
    rig: Rig, channel: Callable[..., str]
) -> None:
    job, token = rig.scheduled(channel())
    body = {"job_id": job, "token": token}
    assert rig.http.post("/v1/publish", json=body).status_code == 401
    bad = {"Authorization": "Bearer " + "z" * 24}
    assert rig.http.post("/v1/publish", json=body, headers=bad).status_code == 401
    wrong = RemoteGateway("http://gateway.test", lambda: "x" * 20, client=rig.http)
    with pytest.raises(PublishRefused):
        wrong.publish(job, token)
    assert rig.client.uploads == []


def test_refusals_keep_their_type_across_the_wire_and_nothing_uploads(
    rig: Rig, channel: Callable[..., str]
) -> None:
    ch = channel()
    job = rig.ops.create_job(ch, "owner", actor="planner")
    chash = drive_to_awaiting(rig.worker, rig.assets, job, ch)
    unrecorded = rig.keys.token(job, ch, chash)  # signed, but no human approved it
    with pytest.raises(PublishRefused):
        rig.remote.publish(job, unrecorded)
    with pytest.raises(PublishRefused):
        rig.remote.publish(job, "garbage")
    assert rig.http.post("/v1/publish", json={"job_id": "nope", "token": "t"}).status_code == 422
    quota_ch = channel(quota=1000)
    job2, token2 = rig.scheduled(quota_ch)
    with pytest.raises(QuotaExhausted):
        rig.remote.publish(job2, token2)
    assert rig.client.uploads == []


def test_the_gateway_exposes_nothing_else(rig: Rig) -> None:
    paths = {r.path for r in rig.app.routes}  # type: ignore[attr-defined]
    assert paths == {"/healthz", "/v1/publish"}


def test_an_insecure_gateway_url_is_refused() -> None:
    with pytest.raises(ValueError, match="https"):
        RemoteGateway("http://gateway.example", lambda: "t")


async def test_workflow_publishes_via_the_gateway_with_a_worker_that_cannot_record_approvals(
    rig: Rig, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    job = rig.ops.create_job(ch, "owner", actor="planner")
    activities = JobActivities(rig.worker, rig.remote, rig.assets, hub_records_approvals=True)
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="gw", workflows=[VideoJobWorkflow],
               activities=activities.all()),
    ):  # fmt: skip
        handle = await env.client.start_workflow(
            VideoJobWorkflow.run,
            VideoJobInput(job_id=job, channel_id=ch, origin="owner"),
            id=f"gw-{job}",
            task_queue="gw",
        )
        for _ in range(200):
            if await handle.query(VideoJobWorkflow.state) == JobState.AWAITING_APPROVAL.value:
                break
            await asyncio.sleep(0.05)
        chash = sql("SELECT content_hash FROM video_jobs WHERE id = %s", job)[0][0]
        token = rig.keys.token(job, ch, chash)
        decision = ApprovalDecision(True, "alice", token)
        await handle.signal(VideoJobWorkflow.submit_approval, decision)
        await asyncio.sleep(1)
        # The worker never records approvals: without the hub's record nothing moves on.
        assert rig.worker.get_state(job) == JobState.AWAITING_APPROVAL
        rig.hub.verify_approval(job, token, actor="hub:alice")  # the human decision (hub)
        await handle.signal(VideoJobWorkflow.submit_approval, decision)
        result = await handle.result()
    assert result.final_state == JobState.LIVE.value
    assert len(rig.client.uploads) == 1
