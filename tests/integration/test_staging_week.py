"""Phase 4 exit gate: staging wiring (same code as the worker entry point, with fakes at the
edges) runs the daily planner for 3 channels over a simulated week. Humans approve most videos,
miss two deadlines and reject one. At the end: no stuck jobs, no duplicate uploads, every missed
deadline re-planned, scorecards computed, and a restore drill of the resulting database passes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pydantic import ValidationError
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.agentkit import BANNED, BRAND_VOICE, HOOKS, POLICY
from tests.fakes import fake_secrets
from tests.harness import Keys
from ytcore.agents.fake import (
    FAKE_MUSIC_LICENSE,
    FAKE_STOCK_LICENSE,
    FakeImage,
    FakeLLM,
    FakeStockLibrary,
    FakeTTS,
    FakeVideo,
    FakeWebFetcher,
)
from ytcore.agents.memory import PostgresMemory
from ytcore.agents.pipeline import Providers
from ytcore.analytics.client import FakeAnalyticsClient
from ytcore.analytics.ingest import AnalyticsIngestor
from ytcore.approvals.service.repo import HubRepository
from ytcore.config import Settings
from ytcore.gateway.assets import InMemoryAssetSource, LocalDirAssetSource
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.inputs.models import InputKind
from ytcore.inputs.store import PostgresInputs
from ytcore.knowledge.models import KnowledgeKind
from ytcore.knowledge.store import PostgresKnowledge
from ytcore.observability.monitor import MonitorThresholds, OpsMonitor
from ytcore.ops import backup
from ytcore.planner.daily import DailyPlanner
from ytcore.planner.temporal import TemporalStarter
from ytcore.workflow.models import ApprovalDecision, VideoJobResult
from ytcore.workflow.states import TERMINAL_STATES, JobState
from ytcore.workflow.video_job import VideoJobWorkflow, workflow_id_for
from ytcore.workflow.worker import build_stack

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]
QUEUE = "staging-week"
DAYS = 7
ZONES = ["America/New_York", "Europe/Berlin", "Asia/Tokyo"]
# (day index, channel index) -> what the humans do with that day's FIRST-attempt video
MISSES = {(2, 1), (5, 2)}
REJECTS = {(4, 0)}


def staging_settings() -> Settings:
    return Settings(
        env="staging",  # type: ignore[arg-type]
        secrets_backend="cloud",
        secrets_cloud_project="ytcore-staging",
        temporal_namespace="ytcore-staging",
        temporal_task_queue=QUEUE,
    )


def test_staging_config_is_valid_and_dev_conveniences_stay_out_of_production() -> None:
    s = staging_settings()
    assert s.env.value == "staging" and s.secrets_backend == "cloud"
    with pytest.raises(ValidationError, match="cloud secrets manager"):
        Settings(env="staging", secrets_backend="env_file", temporal_namespace="ytcore-staging")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="production temporal"):
        Settings(env="staging", secrets_backend="cloud", temporal_namespace="ytcore-prod")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="refuses fake"):
        Settings(env="production", secrets_backend="cloud", temporal_namespace="ytcore-prod")  # type: ignore[arg-type]


def _seed_channel(
    database: str, sql: SQL, channel: str, approver: str, zone: str, hour: int
) -> None:
    sql(
        "UPDATE channels SET audience_timezone = %s, publish_hour_local = %s, "
        "daily_budget_cents = 100000 WHERE id = %s",
        zone, hour, channel,
    )  # fmt: skip
    hub = HubRepository.from_conninfo(database)
    if hub.user_by_name(approver) is None:
        hub.create_user(approver, "Approver", "approver", actor="hub:test")
    kb = PostgresKnowledge.from_conninfo(database).channel(channel)
    for kind, content in (
        (KnowledgeKind.BRAND_VOICE, BRAND_VOICE),
        (KnowledgeKind.HOOK_LIBRARY, HOOKS),
        (KnowledgeKind.BANNED_CLAIMS, BANNED),
        (KnowledgeKind.POLICY, json.dumps(POLICY)),
    ):
        draft = kb.propose(kind, content, proposed_by="owner", base_version=None)
        hub.decide_knowledge(draft.id, draft.content_sha256, username=approver, approve=True)


TOPICS = [
    "How compound interest quietly builds wealth",
    "Why emergency funds beat hot stock tips",
    "Reading a bank statement without panic",
    "The real cost of carrying a credit card balance",
    "What an index fund actually owns",
    "Budgeting methods compared for irregular income",
    "Understanding inflation in everyday prices",
    "Renting versus buying a first home",
    "How retirement accounts differ across countries",
    "Insurance basics that save money later",
    "Student loans and the order of repayment",
    "Taxes on small side income explained",
    "Bond ladders for patient savers",
    "Credit scores and what moves them",
    "Currency exchange fees travellers overlook",
    "Dollar cost averaging versus lump sums",
    "Why subscriptions drain budgets slowly",
    "Negotiating a salary with data",
    "Frugal habits that survive real life",
    "Diversification across sectors and regions",
    "Reading company annual reports quickly",
]


async def test_a_simulated_week_of_daily_planning_leaves_no_stuck_jobs_and_a_restorable_database(
    database: str, channel: Callable[..., str], sql: SQL, keys: Keys, tmp_path: Path
) -> None:
    sql("UPDATE global_controls SET publishing_paused = false")
    cluster_ch = [channel() for _ in range(3)]
    # The test database is shared: park every other channel so only this week's three are planned.
    sql("UPDATE channels SET status = 'paused' WHERE id <> ALL(%s)", cluster_ch)
    cluster = sql("SELECT cluster_id FROM channels WHERE id = %s", cluster_ch[0])[0][0]
    for ch in cluster_ch[1:]:
        sql("UPDATE channels SET cluster_id = %s WHERE id = %s", cluster, ch)
    for ref, kind in ((FAKE_MUSIC_LICENSE, "music"), (FAKE_STOCK_LICENSE, "stock")):
        sql("INSERT INTO rights_register (license_ref, asset_kind, provider) VALUES (%s, %s, "
            "'fake-stock') ON CONFLICT DO NOTHING", ref, kind)  # fmt: skip
    inputs = PostgresInputs.from_conninfo(database)
    n = 0
    for i, ch in enumerate(cluster_ch):
        _seed_channel(database, sql, ch, "week-approver", ZONES[i], 17 + i)
        for d in range(DAYS + 4):  # a few spare ideas for replans
            topic = TOPICS[n % len(TOPICS)] + f" (part {n // len(TOPICS) + 1})"
            slug = topic.lower().replace(" ", "-")
            inputs.add(
                ch,
                InputKind.IDEA,
                submitted_by="owner",
                body=f"{topic}\nSources: https://c{i}-d{d}.example.test/{slug}",
            )
            n += 1

    assets = InMemoryAssetSource()
    youtube = FakeYouTubeClient()
    providers = Providers(
        FakeLLM(), FakeTTS(), UniqueImage(), FakeVideo(), FakeStockLibrary(), FakeWebFetcher()
    )
    stack = build_stack(
        staging_settings(),
        fake_secrets(),
        conninfo=database,
        verifier=keys.verifier,
        assets=assets,
        youtube=youtube,
        providers=providers,
    )
    monitor = OpsMonitor(
        lambda: psycopg.connect(database, autocommit=True), MonitorThresholds(plan_due_hour_utc=24)
    )
    handles: dict[str, WorkflowHandle[Any, Any]] = {}
    approved_at_attempt1: dict[tuple[int, int], str] = {}
    base = datetime(2026, 5, 4, 6, 0, tzinfo=UTC)

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[VideoJobWorkflow],
            activities=stack.activities.all(),
        ),
    ):
        loop = asyncio.get_running_loop()
        planner = DailyPlanner(stack.plans, TemporalStarter(env.client, loop, QUEUE))

        slept: list[str] = []

        async def finish(job: str) -> VideoJobResult:
            """A workflow's result. If it is waiting on a retry timer, move simulated time on."""
            for _ in range(20):
                try:
                    return await asyncio.wait_for(handles[job].result(), 30)  # type: ignore[no-any-return]
                except (RuntimeError, TimeoutError):
                    slept.append(f"{job} {stack.store.get_state(job).value}")
                    await env.sleep(timedelta(minutes=5))
            raise AssertionError(f"job {job} never completed: {slept[-3:]}")

        async def awaiting(job: str) -> None:
            for _ in range(600):
                state = stack.store.get_state(job)
                if state is JobState.AWAITING_APPROVAL:
                    return
                if state in TERMINAL_STATES:
                    reason = await handles[job].result()
                    flags = sql("SELECT flags FROM qa_reports WHERE job_id = %s ORDER BY id", job)
                    raise AssertionError(f"job {job} ended as {state.value}: {reason} {flags}")
                await asyncio.sleep(0.1)
            raise AssertionError(f"job {job} never reached approval")

        for day in range(DAYS):
            sim_now = base + timedelta(days=day)
            run = await asyncio.to_thread(planner.run, sim_now)
            assert run.started or day > 0, "the first run must start workflows"
            for job in run.started:
                handles[job] = env.client.get_workflow_handle(
                    workflow_id_for(job), result_type=VideoJobResult
                )
            planned = run.planned + run.replanned
            today = [
                PlannedKey(p.job_id, p.channel_id, p.attempt)
                for p in planned
                if p.job_id in run.started
            ]
            to_finish, to_miss = [], []
            for p in today:
                await awaiting(p.job)
                ci = cluster_ch.index(p.channel)
                key = (day, ci)
                if p.attempt == 1 and key in MISSES:
                    to_miss.append(p.job)
                    approved_at_attempt1[key] = p.job
                    continue
                ctx = stack.store.publish_context(p.job)
                token = keys.token(p.job, p.channel, ctx.content_hash or "")
                approve = not (p.attempt == 1 and key in REJECTS)
                await handles[p.job].signal(
                    VideoJobWorkflow.submit_approval,
                    ApprovalDecision(approve, "week-approver", token if approve else None,
                                     "" if approve else "off-brand"),
                )  # fmt: skip
                to_finish.append(p.job)
            for job in to_finish:
                res = await finish(job)
                assert res.final_state in {"live", "rejected"}, res
            if to_miss:  # nobody approves: skip past the approval deadline
                await env.sleep(timedelta(days=2))
            for job in to_miss:
                res = await asyncio.wait_for(handles[job].result(), 120)
                assert res.final_state == "cancelled" and res.replanned, res

        # One more pass so the last day's missed deadline is replanned and run to completion.
        sim_now = base + timedelta(days=DAYS)
        run = await asyncio.to_thread(planner.run, sim_now)
        for job in run.started:
            handles[job] = env.client.get_workflow_handle(
                workflow_id_for(job), result_type=VideoJobResult
            )
            await awaiting(job)
            ch = sql("SELECT channel_id FROM video_jobs WHERE id = %s", job)[0][0]
            ctx = stack.store.publish_context(job)
            await handles[job].signal(
                VideoJobWorkflow.submit_approval,
                ApprovalDecision(
                    True, "week-approver", keys.token(job, ch, ctx.content_hash or "")
                ),
            )
            assert (await asyncio.wait_for(handles[job].result(), 120)).final_state == "live"
        await asyncio.to_thread(planner.run, sim_now)  # idempotent: nothing new to start

    assert slept == [], slept
    # ---- exit gate -------------------------------------------------------------------------
    states = sql(
        "SELECT state, count(*) FROM video_jobs WHERE channel_id = ANY(%s) GROUP BY state",
        cluster_ch,
    )
    by_state = dict(states)
    non_terminal = {s: c for s, c in by_state.items() if JobState(s) not in TERMINAL_STATES}
    assert non_terminal == {}, f"stuck jobs: {non_terminal}"
    assert by_state.get("cancelled") == len(MISSES) and by_state.get("rejected") == len(REJECTS)
    live = by_state["live"]
    assert live >= DAYS * 3 - len(MISSES) - len(REJECTS)
    assert len(youtube.uploads) == live  # one upload per live video, no duplicates
    assert sql("SELECT count(*) FROM uploads WHERE channel_id = ANY(%s)", cluster_ch)[0][0] == live
    assert not [f for f in monitor.findings(base + timedelta(days=DAYS + 1))
                if f.kind == "job.stuck" and f.channel_id in cluster_ch]  # fmt: skip
    # every missed deadline was replanned (a new attempt), nothing else was
    attempts = sql("SELECT channel_id, plan_date, count(*) FROM daily_plans WHERE channel_id = "
                   "ANY(%s) AND attempt > 1 GROUP BY 1, 2", cluster_ch)  # fmt: skip
    assert len(attempts) == len(MISSES)
    open_plans = sql(
        "SELECT status, count(*) FROM daily_plans WHERE channel_id = ANY(%s) "
        "AND status IN ('replan_requested', 'exhausted') GROUP BY 1",
        cluster_ch,
    )
    assert open_plans == []
    assert sql("SELECT count(*) FROM alerts WHERE kind = 'plan.exhausted'")[0][0] == 0
    # the slot rules: one live slot per channel per local day, staggered inside the cluster
    doubled = sql(
        "SELECT count(*) FROM (SELECT channel_id, slot_date FROM daily_plans "
        "WHERE status <> 'replanned' AND channel_id = ANY(%s) "
        "GROUP BY 1, 2 HAVING count(*) > 1) x",
        cluster_ch,
    )
    rows = sql(
        "SELECT channel_id, slot_date, plan_date, attempt, status FROM daily_plans "
        "WHERE channel_id = ANY(%s) ORDER BY 1, 2, 4",
        cluster_ch,
    )
    assert doubled[0][0] == 0, rows
    assert sql("SELECT audit_chain_first_break()")[0][0] is None

    # scorecards for every channel and day
    ing = AnalyticsIngestor(
        lambda: psycopg.connect(database, autocommit=True),
        FakeAnalyticsClient(),
        fake_secrets(),
        PostgresMemory(lambda: psycopg.connect(database, autocommit=True)),
    )
    today = datetime.now(UTC).date()
    assert ing.scorecards(today) >= 3
    card = sql("SELECT sum(videos_published), sum(cost_cents) FROM daily_scorecards "
               "WHERE channel_id = ANY(%s) AND score_date = %s", cluster_ch, today)[0]  # fmt: skip
    assert card[0] == live and card[1] > 0

    # restore drill on the resulting database (Postgres + the assets it references)
    out = tmp_path / "backup"
    backup.backup_postgres(database, out)
    disk = LocalDirAssetSource(tmp_path / "disk")
    for key in assets.list_keys():
        disk.write_raw(key, assets.read(key))
    backup_store = LocalDirAssetSource(out / "storage")
    backup.backup_storage(disk, backup_store, out)
    result = backup.restore_drill(
        database,
        out,
        backup_assets=backup_store,
        scratch_assets=LocalDirAssetSource(tmp_path / "r"),
    )
    assert result.ok, result.problems
    assert result.restored_objects >= live


class UniqueImage(FakeImage):
    """A real image model never returns byte-identical art for two videos; the deterministic fake
    does when two prompts match, which the duplicate-media check (rightly) blocks."""

    _n = 0

    def generate(self, prompt: str, *, size: str = "1280x720") -> Any:
        UniqueImage._n += 1
        return super().generate(f"{prompt} #{UniqueImage._n}", size=size)


class PlannedKey:
    def __init__(self, job: str, channel: str, attempt: int) -> None:
        self.job, self.channel, self.attempt = job, channel, attempt
