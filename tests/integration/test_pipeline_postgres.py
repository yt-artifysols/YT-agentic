"""Exit-gate tests: the full pipeline and every publish-blocking rule against real Postgres."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.harness import Keys, drive_to_awaiting, make_service
from ytcore.gateway.assets import InMemoryAssetSource
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.gateway.service import PublishRefused, PublishService
from ytcore.store.errors import (
    ApprovalInvalid,
    ApprovalReplayError,
    ChannelNotPublishable,
    QuotaExhausted,
)
from ytcore.store.postgres import PostgresStore
from ytcore.workflow.activities import JobActivities
from ytcore.workflow.models import ApprovalDecision, VideoJobInput
from ytcore.workflow.states import JobState
from ytcore.workflow.video_job import VideoJobWorkflow

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]


class Rig:
    def __init__(self, store: PostgresStore, keys: Keys, secrets: Any) -> None:
        self.store, self.keys = store, keys
        self.assets = InMemoryAssetSource()
        self.client = FakeYouTubeClient()
        self.service: PublishService = make_service(store, keys, self.assets, self.client, secrets)

    def approved(self, channel_id: str) -> tuple[str, str, str]:
        job = self.store.create_job(channel_id, "owner", actor="planner")
        chash = drive_to_awaiting(self.store, self.assets, job, channel_id)
        token = self.keys.token(job, channel_id, chash)
        self.store.verify_approval(job, token, actor="approval-hub")
        return job, token, chash

    def scheduled(self, channel_id: str) -> tuple[str, str, str]:
        job, token, chash = self.approved(channel_id)
        self.store.transition(job, JobState.SCHEDULED, actor="scheduler")
        return job, token, chash


@pytest.fixture
def rig(store: PostgresStore, keys: Keys, secrets: Any) -> Rig:
    return Rig(store, keys, secrets)


# ------------------------------------------------------------------------- full pipeline


async def test_full_pipeline_goes_live_with_complete_trail(
    rig: Rig, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    job = rig.store.create_job(ch, "owner", actor="planner")
    activities = JobActivities(rig.store, rig.service, rig.assets)
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="it", workflows=[VideoJobWorkflow],
               activities=activities.all()),
    ):  # fmt: skip
        handle = await env.client.start_workflow(
            VideoJobWorkflow.run,
            VideoJobInput(job_id=job, channel_id=ch, origin="owner"),
            id=f"it-{job}",
            task_queue="it",
        )
        for _ in range(200):
            if await handle.query(VideoJobWorkflow.state) == JobState.AWAITING_APPROVAL.value:
                break
            await asyncio.sleep(0.05)
        chash = sql("SELECT content_hash FROM video_jobs WHERE id = %s", job)[0][0]
        await handle.signal(
            VideoJobWorkflow.submit_approval,
            ApprovalDecision(True, "owner", rig.keys.token(job, ch, chash)),
        )
        result = await handle.result()

    assert result.final_state == JobState.LIVE.value
    assert rig.store.get_state(job) == JobState.LIVE
    events = sql("SELECT from_state, to_state, actor FROM job_events WHERE job_id = %s "
                 "ORDER BY id", job)  # fmt: skip
    assert [e[1] for e in events] == [
        "planned", "researching", "scripting", "rendering", "qa", "awaiting_approval",
        "approved", "scheduled", "publishing", "published", "live",
    ]  # fmt: skip
    actors = {e[1]: e[2] for e in events}
    assert actors["planned"] == "planner"
    assert actors["approved"] == "approver:owner@example.test"
    assert actors["publishing"] == actors["live"] == "gateway"

    audit = {r[0] for r in sql("SELECT action FROM audit_events WHERE subject = %s",
                               f"job:{job}")}  # fmt: skip
    assert {"job.created", "job.transition", "approval.consumed", "cost.recorded",
            "upload.pending", "upload.uploaded", "upload.live"} <= audit  # fmt: skip
    assert sql("SELECT audit_chain_first_break()")[0][0] is None

    upload = sql("SELECT status, youtube_video_id, synthetic_disclosure FROM uploads "
                 "WHERE job_id = %s", job)  # fmt: skip
    assert upload == [("live", result.video_id, True)]
    ops = sql("SELECT operation, units FROM quota_ledger WHERE job_id = %s ORDER BY id", job)
    assert ops == [("videos.insert", 1600), ("videos.list", 1)]
    assert sql("SELECT render_cost_cents FROM video_jobs WHERE id = %s", job)[0][0] > 0


# ------------------------------------------------------------------------- negatives


def test_replayed_token_is_blocked(rig: Rig, channel: Callable[..., str]) -> None:
    job, token, _ = rig.approved(channel())
    with pytest.raises(ApprovalReplayError):
        rig.store.verify_approval(job, token, actor="approval-hub")


def test_token_replayed_on_another_job_is_blocked(rig: Rig, channel: Callable[..., str]) -> None:
    ch = channel()
    _, token, _ = rig.approved(ch)
    other = rig.store.create_job(ch, "owner", actor="planner")
    drive_to_awaiting(rig.store, rig.assets, other, ch)
    with pytest.raises(ApprovalInvalid):
        rig.store.verify_approval(other, token, actor="approval-hub")
    assert rig.store.get_state(other) == JobState.AWAITING_APPROVAL


def test_token_for_different_content_hash_is_blocked(rig: Rig, channel: Callable[..., str]) -> None:
    ch = channel()
    job = rig.store.create_job(ch, "owner", actor="planner")
    drive_to_awaiting(rig.store, rig.assets, job, ch)
    stale = rig.keys.token(job, ch, "sha256:" + "f" * 64)
    with pytest.raises(ApprovalInvalid):
        rig.store.verify_approval(job, stale, actor="approval-hub")
    scheduled_job, _, _ = rig.scheduled(ch)
    with pytest.raises(PublishRefused):
        rig.service.publish(scheduled_job, rig.keys.token(scheduled_job, ch, "sha256:" + "f" * 64))
    assert rig.client.uploads == []


@pytest.mark.parametrize("target", ["channel", "cluster"])
def test_paused_channel_or_cluster_is_blocked(
    rig: Rig, channel: Callable[..., str], sql: SQL, target: str
) -> None:
    ch = channel()
    job, token, _ = rig.scheduled(ch)
    other, _, _ = rig.approved(ch)  # approvals are refused once a kill switch is engaged
    if target == "channel":
        sql("UPDATE channels SET status = 'paused' WHERE id = %s", ch)
    else:
        sql("UPDATE clusters SET status = 'paused' WHERE id = "
            "(SELECT cluster_id FROM channels WHERE id = %s)", ch)  # fmt: skip
    with pytest.raises(PublishRefused):
        rig.service.publish(job, token)
    # The DB blocks it even if the gateway check were bypassed.
    with pytest.raises(ChannelNotPublishable):
        rig.store.transition(other, JobState.SCHEDULED, actor="scheduler")
    assert rig.client.uploads == []
    assert rig.store.get_state(job) == JobState.SCHEDULED


def test_shadow_mode_channel_is_blocked(rig: Rig, channel: Callable[..., str]) -> None:
    ch = channel(autonomy_tier="shadow")
    job, _, _ = rig.approved(ch)
    with pytest.raises(ChannelNotPublishable):
        rig.store.transition(job, JobState.SCHEDULED, actor="scheduler")
    assert rig.store.get_state(job) == JobState.APPROVED
    assert rig.client.uploads == []


def test_quota_exhausted_is_blocked(rig: Rig, channel: Callable[..., str], sql: SQL) -> None:
    ch = channel(quota=1000)
    job, token, _ = rig.scheduled(ch)
    with pytest.raises(QuotaExhausted):
        rig.service.publish(job, token)
    assert rig.client.uploads == []
    assert rig.store.get_state(job) == JobState.SCHEDULED  # whole transaction rolled back
    assert sql("SELECT count(*) FROM uploads WHERE job_id = %s", job)[0][0] == 0


def test_gateway_without_approval_is_blocked(
    rig: Rig, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    job = rig.store.create_job(ch, "owner", actor="planner")
    chash = drive_to_awaiting(rig.store, rig.assets, job, ch)
    unconsumed = rig.keys.token(job, ch, chash)  # validly signed but never approved/consumed
    with pytest.raises(PublishRefused):
        rig.service.publish(job, unconsumed)
    with pytest.raises(PublishRefused):
        rig.service.publish(job, "no-token")
    assert rig.client.uploads == []
    assert sql("SELECT count(*) FROM uploads WHERE job_id = %s", job)[0][0] == 0


def test_duplicate_upload_returns_same_video_id(
    rig: Rig, channel: Callable[..., str], sql: SQL
) -> None:
    job, token, _ = rig.scheduled(channel())
    first = rig.service.publish(job, token)
    second = rig.service.publish(job, token)
    assert first.state == JobState.LIVE
    assert second.duplicate and second.video_id == first.video_id
    assert len(rig.client.uploads) == 1
    assert sql("SELECT count(*) FROM uploads WHERE job_id = %s", job)[0][0] == 1


def test_concurrent_attempt_never_uploads_twice(
    rig: Rig, channel: Callable[..., str], sql: SQL
) -> None:
    from ytcore.gateway.service import UploadInProgress, idempotency_key

    job, token, chash = rig.scheduled(channel())
    ctx = rig.store.publish_context(job)
    jti = rig.keys.verifier.verify(
        token, job_id=job, channel_id=ctx.channel_id, content_hash=chash
    ).jti
    kwargs = {
        "idempotency_key": idempotency_key(job, chash),
        "approval_jti": jti,
        "synthetic_disclosure": True,
        "quota_units": 1600,
        "actor": "attempt-1",
    }
    first = rig.store.begin_upload(ctx, **kwargs)  # type: ignore[arg-type]
    assert first.status == "pending"
    raced = rig.store.begin_upload(rig.store.publish_context(job), **kwargs)  # type: ignore[arg-type]
    assert raced.status == "in_progress"
    with pytest.raises(UploadInProgress):
        rig.service.publish(job, token)
    assert rig.client.uploads == []
    # The losing attempt's quota charge was rolled back with its transaction.
    assert sql("SELECT count(*) FROM quota_ledger WHERE job_id = %s", job)[0][0] == 1


def test_replay_unique_constraint_maps_to_typed_error(
    rig: Rig, channel: Callable[..., str], sql: SQL
) -> None:
    import psycopg

    from ytcore.store.postgres import map_db_error

    ch = channel()
    job, _, _ = rig.approved(ch)
    jti = sql("SELECT token_jti FROM approvals WHERE job_id = %s", job)[0][0]
    other = rig.store.create_job(ch, "owner", actor="planner")
    drive_to_awaiting(rig.store, rig.assets, other, ch)
    with pytest.raises(psycopg.errors.UniqueViolation) as exc:
        sql("INSERT INTO approvals (job_id, channel_id, content_hash, token_jti, approver, "
            "expires_at) SELECT id, channel_id, content_hash, %s, 'x', now() + interval '1h' "
            "FROM video_jobs WHERE id = %s", jti, other)  # fmt: skip
    assert isinstance(map_db_error(exc.value), ApprovalReplayError)


def test_budget_is_enforced_per_channel(rig: Rig, channel: Callable[..., str]) -> None:
    from ytcore.store.errors import BudgetExceeded

    ch = channel(budget=100)
    job = rig.store.create_job(ch, "owner", actor="planner")
    rig.store.record_cost(job, "render", 80, actor="renderer")
    with pytest.raises(BudgetExceeded):
        rig.store.record_cost(job, "render", 30, actor="renderer")
