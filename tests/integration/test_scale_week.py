"""Phase 5 exit gate (simulation half): 20 channels in 4 clusters for 7 simulated days.

Everything runs for real except the outside world: the YAML fleet is applied to Postgres, the
daily planner hands jobs to cluster supervisors, the agent pipeline runs with fake providers that
fail (rate limits, upstream errors), shared pools are fair-shared with leases, humans approve in a
daily session (and miss/reject a few), and the gateway publishes through the fake YouTube client.

It measures queue depth, pool contention, retries and cost per video and holds them to the
targets in the fleet's platform.yaml. Set SCALE_REPORT=<path> to also write the numbers as JSON.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.agentkit import BANNED  # noqa: F401  (documented knowledge fixtures live there)
from tests.harness import Keys
from tests.integration.conftest import fresh_database
from tests.integration.test_roles import as_role
from tests.scalekit import Faults, Flaky, UniqueImage, topic, write_fleet
from ytcore.agents.fake import (
    FAKE_MUSIC_LICENSE,
    FAKE_STOCK_LICENSE,
    FakeLLM,
    FakeStockLibrary,
    FakeTTS,
    FakeVideo,
    FakeWebFetcher,
)
from ytcore.agents.pipeline import Providers
from ytcore.approvals.service.repo import HubRepository
from ytcore.channels.apply import apply_fleet
from ytcore.channels.config import load_fleet
from ytcore.gateway.assets import InMemoryAssetSource
from ytcore.gateway.auth import StaticBearerAuthenticator
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.gateway.remote import RemoteGateway
from ytcore.gateway.server import create_gateway_app
from ytcore.gateway.service import PublishService
from ytcore.inputs.models import InputKind
from ytcore.inputs.store import PostgresInputs
from ytcore.planner.daily import DailyPlanner, PlanRepository
from ytcore.planner.temporal import TemporalStarter
from ytcore.secrets.provider import InMemorySecretsProvider
from ytcore.store.postgres import PostgresStore
from ytcore.workflow.models import ApprovalDecision, VideoJobResult
from ytcore.workflow.states import TERMINAL_STATES, JobState
from ytcore.workflow.supervisor import ClusterSupervisorWorkflow
from ytcore.workflow.video_job import VideoJobWorkflow, workflow_id_for
from ytcore.workflow.worker import build_stack

pytestmark = [pytest.mark.integration, pytest.mark.chaos]
QUEUE = "scale-week"
GATEWAY_TOKEN = "scale-week-gateway-token-0123456789"
DAYS = 7
APPROVER = "scale-approver"
# (day, channel index): the humans do not approve in time / reject the video
MISSES = {(2, 3), (2, 11), (5, 17)}
REJECTS = {(4, 6)}


@pytest.fixture
def database() -> Iterator[str]:
    with fresh_database() as conninfo:
        yield conninfo


def _sql(db: str, query: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(db, autocommit=True) as conn:
        cur = conn.execute(query, params or None)
        return cur.fetchall() if cur.description else []


async def test_twenty_channels_for_a_week_meet_the_targets(database: str, tmp_path: Path) -> None:
    try:
        await _run(database, tmp_path)
    except BaseException:
        # Diagnostics for a failed run (the database is dropped afterwards).
        print("states:", _sql(database, "SELECT state, count(*) FROM video_jobs GROUP BY 1"))
        print("uploads:", _sql(database, "SELECT status, failure_reason, count(*) FROM uploads "
                                        "GROUP BY 1, 2"))  # fmt: skip
        print(
            "qaflags:",
            _sql(
                database,
                "SELECT left(flags::text, 160), count(*) FROM qa_reports "
                "WHERE NOT passed GROUP BY 1 ORDER BY 2 DESC LIMIT 5",
            ),
        )
        print("alerts:", _sql(database, "SELECT kind, message FROM alerts LIMIT 8"))
        raise


async def _run(database: str, tmp_path: Path) -> None:
    db = database
    root = tmp_path / "config"
    slugs = write_fleet(root, clusters=4, per_cluster=5)
    fleet = load_fleet(root)
    assert len(fleet.channels) == 20  # no code change: 20 YAML files and one loop

    connect = lambda: psycopg.connect(db, autocommit=True)  # noqa: E731
    report = apply_fleet(connect, fleet)
    assert set(report.channels.values()) == {"created"}
    assert not apply_fleet(connect, fleet).changed

    # Humans approve every proposed brand/policy document (agents and config only draft them).
    hub = HubRepository.from_conninfo(as_role(db, "yt_hub"))
    hub.create_user(APPROVER, "Approver", "approver", actor="hub:test")
    for doc_id, sha in _sql(db, "SELECT id::text, content_sha256 FROM knowledge_docs"):
        hub.decide_knowledge(doc_id, sha, username=APPROVER, approve=True)
    for ref, kind in ((FAKE_MUSIC_LICENSE, "music"), (FAKE_STOCK_LICENSE, "stock")):
        _sql(db, "INSERT INTO rights_register (license_ref, asset_kind, provider) "
                 "VALUES (%s, %s, 'fake-stock')", ref, kind)  # fmt: skip
    inputs = PostgresInputs.from_conninfo(db)
    for i, slug in enumerate(slugs):
        for d in range(DAYS + 3):  # a few spare ideas for replans
            inputs.add(
                slug,
                InputKind.IDEA,
                submitted_by="owner",
                body=f"{topic(i, d)}\nSources: https://c{i}-d{d}.example.test/{i}-{d}",
            )

    faults = Faults(seed=11, rate_limit_p=0.03, error_p=0.01)
    providers = Providers(
        Flaky(FakeLLM(), faults, "llm", ("complete",)),  # type: ignore[arg-type]
        Flaky(FakeTTS(), faults, "tts", ("synthesize",)),  # type: ignore[arg-type]
        UniqueImage(),  # type: ignore[arg-type]
        Flaky(FakeVideo(), faults, "video", ("render",)),  # type: ignore[arg-type]
        FakeStockLibrary(),
        FakeWebFetcher(),
    )
    keys, assets, youtube = Keys(), InMemoryAssetSource(), FakeYouTubeClient()
    values = {"vault://youtube-oauth-client": '{"client_id": "x", "client_secret": "y"}'}
    for ch in fleet.channels.values():  # dummy per-channel tokens: the references are real
        values[ch.youtube_credentials_ref] = "dummy-refresh"
    # Every service connects as its own database role and the worker publishes only through the
    # gateway service: the week runs with exactly the privileges each service has in production.
    secrets = InMemorySecretsProvider(values)
    gateway_store = PostgresStore.from_conninfo(as_role(db, "yt_gateway"), keys.verifier)
    gateway_app = create_gateway_app(
        PublishService(
            repo=gateway_store, verifier=keys.verifier, client=youtube, assets=assets,
            secrets=secrets,
        ),
        StaticBearerAuthenticator(GATEWAY_TOKEN),
    )  # fmt: skip
    remote = RemoteGateway(
        "http://gateway.test", lambda: GATEWAY_TOKEN, client=TestClient(gateway_app)
    )
    hub_store = PostgresStore.from_conninfo(as_role(db, "yt_hub"), keys.verifier)
    stack = build_stack(
        _staging(),
        secrets,
        conninfo=as_role(db, "yt_worker"),
        verifier=keys.verifier,
        assets=assets,
        providers=providers,
        pool_poll_seconds=0.05,
        gateway=remote,
    )
    plans = PlanRepository.from_conninfo(as_role(db, "yt_ops"))

    def approve(job: str, channel: str, content_hash: str) -> str:
        """The human decision: only the hub role can record it. Returns the token."""
        token = keys.token(job, channel, content_hash)
        hub_store.verify_approval(job, token, actor=f"hub:{APPROVER}")
        return token

    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    handles: dict[str, WorkflowHandle[Any, Any]] = {}
    peak: dict[str, int] = {"queue": 0, "cluster_running": 0}
    peak_pool: dict[str, int] = {}
    day_seconds: list[float] = []
    queue_depths: list[int] = []

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[VideoJobWorkflow, ClusterSupervisorWorkflow],
            activities=[*stack.activities.all(), *stack.cluster.all()],
            max_concurrent_activities=64,
        ),
    ):
        loop = asyncio.get_running_loop()
        planner = DailyPlanner(plans, TemporalStarter(env.client, loop, QUEUE, supervised=True))
        stop = asyncio.Event()

        async def sampler() -> None:
            while not stop.is_set():
                rows = await asyncio.to_thread(
                    _sql, db,
                    "SELECT pool, count(*) FROM pool_leases WHERE released_at IS NULL "
                    "GROUP BY pool",
                )  # fmt: skip
                for pool, n in rows:
                    peak_pool[pool] = max(peak_pool.get(pool, 0), n)
                running = await asyncio.to_thread(
                    _sql, db, "SELECT coalesce(max(running), 0) FROM cluster_health"
                )
                peak["cluster_running"] = max(peak["cluster_running"], running[0][0])
                await asyncio.sleep(0.05)

        sampling = asyncio.create_task(sampler())

        async def awaiting_or_done(job: str) -> JobState:
            for _ in range(2400):
                state = stack.store.get_state(job)
                if state is JobState.AWAITING_APPROVAL or state in TERMINAL_STATES:
                    return state
                await asyncio.sleep(0.1)
            raise AssertionError(f"job {job} stuck in {stack.store.get_state(job).value}")

        deadlines: dict[str, datetime] = {}
        for day in range(DAYS):
            started = time.monotonic()
            run = await asyncio.to_thread(planner.run, base + timedelta(days=day))
            todays = [p for p in run.planned + run.replanned if p.job_id in run.started]
            deadlines.update({p.job_id: p.slot.approval_deadline for p in todays})
            for p in todays:
                handles[p.job_id] = env.client.get_workflow_handle(
                    workflow_id_for(p.job_id), result_type=VideoJobResult
                )
            states = await asyncio.gather(*(awaiting_or_done(p.job_id) for p in todays))
            day_seconds.append(time.monotonic() - started)
            waiting = [
                p for p, s in zip(todays, states, strict=True) if s is JobState.AWAITING_APPROVAL
            ]
            queue_depths.append(len(waiting))
            for p, st in zip(todays, states, strict=True):
                if st is JobState.FAILED:
                    r = await asyncio.wait_for(handles[p.job_id].result(), 60)
                    print("FAILED", p.channel_id, r.reason[:200])

            finish, miss = [], []
            for p in waiting:
                idx = slugs.index(p.channel_id)
                if p.attempt == 1 and (day, idx) in MISSES:
                    miss.append(p.job_id)
                    continue
                ctx = stack.store.publish_context(p.job_id)
                ok = not (p.attempt == 1 and (day, idx) in REJECTS)
                token = approve(p.job_id, p.channel_id, ctx.content_hash or "") if ok else None
                await handles[p.job_id].signal(
                    VideoJobWorkflow.submit_approval,
                    ApprovalDecision(ok, APPROVER, token, "" if ok else "off-brand"),
                )
                finish.append(p.job_id)
            for job in finish:
                res = await asyncio.wait_for(_result(handles[job], env), 180)
                assert res.final_state in {"live", "rejected"}, res
            if miss:  # nobody approves: Temporal's clock moves past the latest deadline
                target = max(deadlines[j] for j in miss) + timedelta(minutes=1)
                await env.sleep(max(target - await env.get_current_time(), timedelta(seconds=1)))
                for job in miss:
                    res = await asyncio.wait_for(handles[job].result(), 180)
                    assert res.final_state == "cancelled" and res.replanned

        # the last day's missed deadlines are replanned and published
        run = await asyncio.to_thread(planner.run, base + timedelta(days=DAYS))
        for job in run.started:
            handles[job] = env.client.get_workflow_handle(
                workflow_id_for(job), result_type=VideoJobResult
            )
            await awaiting_or_done(job)
            if stack.store.get_state(job) is not JobState.AWAITING_APPROVAL:
                continue
            ctx = stack.store.publish_context(job)
            token = approve(job, ctx.channel_id, ctx.content_hash or "")
            await handles[job].signal(
                VideoJobWorkflow.submit_approval, ApprovalDecision(True, APPROVER, token)
            )
            assert (await asyncio.wait_for(_result(handles[job], env), 180)).final_state == "live"
        stop.set()
        await sampling

    # ---- results ---------------------------------------------------------------------------
    by_state = dict(_sql(db, "SELECT state, count(*) FROM video_jobs GROUP BY state"))
    total = sum(by_state.values())
    live = by_state.get("live", 0)
    produced = live + by_state.get("rejected", 0) + by_state.get("failed", 0)
    cost = _sql(db, "SELECT coalesce(sum(cost_cents), 0) FROM job_costs")[0][0]
    cost_per_video = cost / max(produced, 1)
    expected_ok = total - by_state.get("cancelled", 0) - by_state.get("rejected", 0)
    success_rate = live / max(expected_ok, 1)
    metrics = {
        "channels": 20, "days": DAYS, "jobs": total, "states": by_state,
        "success_rate": round(success_rate, 4), "cost_cents_total": int(cost),
        "cost_per_video_cents": round(cost_per_video, 2),
        "approval_queue_depth_by_day": queue_depths,
        "max_cluster_running": peak["cluster_running"], "peak_pool_leases": peak_pool,
        "day_seconds": [round(s, 1) for s in day_seconds],
        "injected_faults": dict(faults.injected),
        "uploads": len(youtube.uploads),
    }  # fmt: skip
    if path := os.environ.get("SCALE_REPORT"):
        Path(path).write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    print(json.dumps(metrics, indent=2, sort_keys=True))

    t = fleet.platform.targets
    # nothing is stuck, nothing uploaded twice, every missed deadline was replanned
    assert all(JobState(s) in TERMINAL_STATES for s in by_state), by_state
    assert len(youtube.uploads) == live
    assert by_state.get("cancelled") == len(MISSES) and by_state.get("rejected") == len(REJECTS)
    assert sum(faults.injected.values()) > 0  # the failures really happened
    assert success_rate >= t.min_success_rate, metrics
    assert cost_per_video <= t.max_cost_per_video_cents, metrics
    assert max(queue_depths) <= t.max_approval_queue_depth, metrics
    assert max(day_seconds) <= t.max_sim_day_seconds, metrics
    # fair share held: no pool ever exceeded its global limit, no cluster its concurrency cap
    for pool, limit in fleet.platform.pools.items():
        assert peak_pool.get(pool, 0) <= limit, (pool, peak_pool)
    assert peak["cluster_running"] <= 3  # the cap applies to jobs in production
    assert _sql(db, "SELECT audit_chain_first_break()")[0][0] is None


async def _result(handle: WorkflowHandle[Any, Any], env: WorkflowEnvironment) -> VideoJobResult:
    """result(), moving simulated time on if the workflow waits on a retry timer."""
    for _ in range(20):
        try:
            return await asyncio.wait_for(handle.result(), 30)  # type: ignore[no-any-return]
        except (RuntimeError, TimeoutError):
            await env.sleep(timedelta(minutes=5))
    raise AssertionError("workflow never completed")


def _staging() -> Any:
    from ytcore.config import Settings

    return Settings(
        env="staging",  # type: ignore[arg-type]
        secrets_backend="cloud",
        secrets_cloud_project="ytcore-staging",
        temporal_namespace="ytcore-staging",
        temporal_task_queue=QUEUE,
    )
