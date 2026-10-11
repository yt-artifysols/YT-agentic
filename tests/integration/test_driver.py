"""Serverless staging: the database-driven job driver (cron) replaces the Temporal workflow.
The same rules must hold: only a hub-recorded approval publishes, edits re-run QA and never
approve, stale decisions are ignored, a missed deadline cancels and re-plans."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from tests.harness import Keys, make_service
from tests.integration.hubkit import Hub, build_hub
from tests.integration.test_roles import as_role
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.serverless.driver import JobDriver
from ytcore.serverless.signaler import DbSignaler
from ytcore.store.postgres import PostgresStore
from ytcore.workflow.activities import JobActivities

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]


class Rig:
    def __init__(self, database: str, secrets: Any) -> None:
        keys = Keys()
        self.database = database
        self.mine: list[str] = []
        self.hub: Hub = build_hub(
            database, signaler=DbSignaler(as_role(database, "yt_hub")), keys=keys
        )
        self.client = FakeYouTubeClient()
        gateway_store = PostgresStore.from_conninfo(as_role(database, "yt_gateway"), keys.verifier)
        service = make_service(gateway_store, keys, self.hub.assets, self.client, secrets)
        self.activities = JobActivities(
            self.hub.worker_store, service, self.hub.assets, hub_records_approvals=True
        )
        self.driver = JobDriver(self.activities, as_role(database, "yt_worker"))

    def job(self, channel_id: str) -> str:
        job = self.hub.worker_store.create_job(channel_id, "owner", actor="planner")
        self.mine.append(job)
        return job

    async def tick(self) -> Any:
        # the test database is shared: only drive this test's own jobs
        return await self.driver.tick(only=self.mine)

    async def to_awaiting(self, job: str) -> str:
        await self.tick()
        head = self.hub.repo.head(job)
        assert head is not None and head.state == "awaiting_approval" and head.content_hash
        return head.content_hash


@pytest.fixture
def rig(database: str, secrets: Any) -> Rig:
    return Rig(database, secrets)


async def test_driver_produces_then_waits_for_the_human(
    rig: Rig, channel: Callable[..., str], sql: SQL
) -> None:
    job = rig.job(channel())
    await rig.to_awaiting(job)
    await rig.tick()  # idle ticks change nothing and never publish
    assert rig.client.uploads == []
    assert sql("SELECT count(*) FROM approvals WHERE job_id = %s", job)[0][0] == 0
    assert sql("SELECT state FROM video_jobs WHERE id = %s", job) == [("awaiting_approval",)]


async def test_hub_approval_publishes_through_the_driver(
    rig: Rig, channel: Callable[..., str], sql: SQL
) -> None:
    job = rig.job(channel())
    chash = await rig.to_awaiting(job)
    async with rig.hub.client() as c:
        csrf = await rig.hub.register(c, "drv-approver", "approver")
        res = await c.post(f"/api/jobs/{job}/approve", json={"content_hash": chash},
                           headers={"x-csrf-token": csrf})  # fmt: skip
        assert res.status_code == 200, res.text
    pending = sql("SELECT token IS NOT NULL FROM workflow_decisions WHERE job_id = %s", job)
    assert pending == [(True,)]
    await rig.tick()
    assert sql("SELECT state FROM video_jobs WHERE id = %s", job) == [("live",)]
    assert len(rig.client.uploads) == 1
    jti = res.json()["jti"]
    assert sql("SELECT approval_jti FROM uploads WHERE job_id = %s", job) == [(jti,)]
    # the decision is consumed and the token erased
    assert sql(
        "SELECT consumed_at IS NOT NULL, token IS NULL FROM workflow_decisions WHERE job_id = %s",
        job,
    ) == [(True, True)]
    await rig.tick()
    assert len(rig.client.uploads) == 1


async def test_overlapping_ticks_publish_once(
    rig: Rig, channel: Callable[..., str], sql: SQL
) -> None:
    job = rig.job(channel())
    chash = await rig.to_awaiting(job)
    async with rig.hub.client() as c:
        csrf = await rig.hub.register(c, "drv-approver2", "approver")
        res = await c.post(f"/api/jobs/{job}/approve", json={"content_hash": chash},
                           headers={"x-csrf-token": csrf})  # fmt: skip
        assert res.status_code == 200
    await asyncio.gather(rig.tick(), rig.tick(), rig.tick())
    assert len(rig.client.uploads) == 1
    assert sql("SELECT state FROM video_jobs WHERE id = %s", job) == [("live",)]


async def test_edit_reruns_qa_and_only_the_new_content_can_be_approved(
    rig: Rig, channel: Callable[..., str], sql: SQL
) -> None:
    job = rig.job(channel())
    first = await rig.to_awaiting(job)
    qa_before = sql("SELECT count(*) FROM qa_reports WHERE job_id = %s", job)[0][0]
    async with rig.hub.client() as c:
        csrf = await rig.hub.register(c, "drv-approver3", "approver")
        hdr = {"x-csrf-token": csrf}
        change = {"content_hash": first, "title": "Sharper title"}
        edit = await c.post(f"/api/jobs/{job}/edit", json=change, headers=hdr)
        assert edit.status_code == 200, edit.text
        await rig.tick()
        second = edit.json()["content_hash"]
        assert second != first
        assert sql("SELECT count(*) FROM qa_reports WHERE job_id = %s", job)[0][0] == qa_before + 1
        assert sql("SELECT state FROM video_jobs WHERE id = %s", job) == [("awaiting_approval",)]
        assert sql("SELECT count(*) FROM approvals WHERE job_id = %s", job)[0][0] == 0
        stale = await c.post(f"/api/jobs/{job}/approve", json={"content_hash": first}, headers=hdr)
        assert stale.status_code == 409
        ok = await c.post(f"/api/jobs/{job}/approve", json={"content_hash": second}, headers=hdr)
        assert ok.status_code == 200
    await rig.tick()
    assert rig.client.uploads[0][1].title == "Sharper title"


async def test_reject_discards_or_reworks(rig: Rig, channel: Callable[..., str], sql: SQL) -> None:
    ch = channel()
    discard, rework = rig.job(ch), rig.job(ch)
    await rig.tick()
    async with rig.hub.client() as c:
        csrf = await rig.hub.register(c, "drv-approver4", "approver")
        hdr = {"x-csrf-token": csrf}
        for job, stage in ((discard, "discard"), (rework, "render")):
            head = rig.hub.repo.head(job)
            assert head is not None
            res = await c.post(f"/api/jobs/{job}/reject",
                               json={"content_hash": head.content_hash,
                                     "reason": "hook is too weak for the topic",
                                     "rework_stage": stage}, headers=hdr)  # fmt: skip
            assert res.status_code == 200, res.text
    await rig.tick()
    assert sql("SELECT state FROM video_jobs WHERE id = %s", discard) == [("rejected",)]
    assert sql("SELECT state FROM video_jobs WHERE id = %s", rework) == [("awaiting_approval",)]
    states = [
        r[0] for r in sql("SELECT to_state FROM job_events WHERE job_id = %s ORDER BY id", rework)
    ]
    assert states.count("rendering") == 2
    assert rig.client.uploads == []


async def test_stale_decision_is_ignored_and_a_forged_token_never_publishes(
    rig: Rig, channel: Callable[..., str], sql: SQL
) -> None:
    job = rig.job(channel())
    await rig.to_awaiting(job)
    other = "sha256:" + "0" * 64
    with psycopg.connect(as_role(rig.database, "yt_hub"), autocommit=True) as conn:
        for chash, token in ((other, "stale-token"), (None, "forged-token")):
            current = chash or sql("SELECT content_hash FROM video_jobs WHERE id = %s", job)[0][0]
            conn.execute(
                "INSERT INTO workflow_decisions (job_id, action, actor, content_hash, token) "
                "VALUES (%s, 'approve', 'hub:mallory', %s, %s)",
                (job, current, token),
            )
    await rig.tick()
    assert sql("SELECT state FROM video_jobs WHERE id = %s", job) == [("awaiting_approval",)]
    assert rig.client.uploads == []
    assert sql("SELECT count(*) FROM workflow_decisions WHERE consumed_at IS NULL")[0][0] == 0


async def test_missed_deadline_cancels_and_replans(
    rig: Rig, channel: Callable[..., str], sql: SQL
) -> None:
    job = rig.job(channel())
    await rig.to_awaiting(job)
    with psycopg.connect(rig.database, autocommit=True) as conn:
        conn.execute("ALTER TABLE video_jobs DISABLE TRIGGER USER")
        conn.execute(
            "UPDATE video_jobs SET approval_deadline = now() - interval '1 minute' WHERE id = %s",
            (job,),
        )
        conn.execute("ALTER TABLE video_jobs ENABLE TRIGGER USER")
    await rig.tick()
    assert sql("SELECT state FROM video_jobs WHERE id = %s", job) == [("cancelled",)]
    assert [r[0] for r in rig.activities.replan_requests] == [job]
    assert rig.client.uploads == []


def test_only_the_hub_can_leave_decisions_and_tokens_are_erased(
    rig: Rig, channel: Callable[..., str], sql: SQL
) -> None:
    job = rig.job(channel())
    with (
        psycopg.connect(as_role(rig.database, "yt_worker"), autocommit=True) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(
            "INSERT INTO workflow_decisions (job_id, action, actor, content_hash, token) "
            "VALUES (%s, 'approve', 'worker', 'x', 't')",
            (job,),
        )
    with psycopg.connect(as_role(rig.database, "yt_hub"), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO workflow_decisions (job_id, action, actor, content_hash, token) "
            "VALUES (%s, 'approve', 'hub:x', 'h', 'secret-token')",
            (job,),
        )
    with psycopg.connect(as_role(rig.database, "yt_worker"), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE workflow_decisions SET actor = 'worker' WHERE job_id = %s", (job,))
        conn.execute("UPDATE workflow_decisions SET consumed_at = now() WHERE job_id = %s", (job,))
    assert sql("SELECT token FROM workflow_decisions WHERE job_id = %s", job) == [(None,)]
    with (
        psycopg.connect(rig.database, autocommit=True) as conn,
        pytest.raises(psycopg.errors.RaiseException),
    ):
        conn.execute("UPDATE workflow_decisions SET reason = 'x' WHERE job_id = %s", (job,))
