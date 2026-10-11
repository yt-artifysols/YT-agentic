"""Agents against real Postgres: pgvector memory isolation, budgets, licenses, and the Phase 2
exit gate - 10 consecutive shadow jobs reach awaiting_approval with knowledge and owner-input
provenance recorded, and the database blocks publishing."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.agentkit import BANNED, BRAND_VOICE, HOOKS, POLICY
from tests.harness import Keys, make_service
from ytcore.agents.factory import build_postgres_pipeline
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
from ytcore.agents.shadow import run_shadow_batch, summarize
from ytcore.approvals.service.repo import HubRepository
from ytcore.gateway.assets import InMemoryAssetSource
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.inputs.models import InputKind
from ytcore.inputs.store import PostgresInputs
from ytcore.knowledge.models import KnowledgeKind
from ytcore.knowledge.store import PostgresKnowledge
from ytcore.store.errors import (
    ChannelNotPublishable,
    JobBudgetExceeded,
    LicenseInvalid,
    StoreError,
)
from ytcore.store.postgres import PostgresStore
from ytcore.store.production import PostgresProductionStore
from ytcore.workflow.activities import JobActivities
from ytcore.workflow.states import JobState
from ytcore.workflow.video_job import VideoJobWorkflow

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]


def _connect(database: str) -> Callable[[], psycopg.Connection[Any]]:
    return lambda: psycopg.connect(database, autocommit=True)


def _seed_knowledge(database: str, channel: str, approver: str) -> None:
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


def test_memory_namespaces_are_isolated_in_postgres(
    database: str, channel: Callable[..., str], sql: SQL
) -> None:
    a, b = channel(), channel()
    mem = PostgresMemory(_connect(database))
    mem.for_channel(a).add("fact", "channel A secret about index funds")
    mem.for_channel(b).add("fact", "channel B note about bonds")
    assert [m.content for m in mem.for_channel(b).search("secret index funds", k=10)] == [
        "channel B note about bonds"
    ]
    assert mem.for_channel(a).search("secret index funds")[0].content.startswith("channel A")
    assert mem.for_channel(a).namespace == f"mem:{a}"
    # A memory object pointed at another channel's namespace is refused by the database.
    from ytcore.agents.memory import HashEmbedder, PostgresChannelMemory

    forged = PostgresChannelMemory(_connect(database), a, f"mem:{b}", HashEmbedder())
    with pytest.raises(StoreError):
        forged.add("fact", "leak")
    assert sql("SELECT count(*) FROM channel_memory WHERE channel_id = %s", a)[0][0] == 1


def test_postgres_budgets_licenses_and_alerts(
    database: str, channel: Callable[..., str], sql: SQL, keys: Keys
) -> None:
    chan = channel()
    sql("UPDATE channels SET job_budget_cents = 10 WHERE id = %s", chan)
    store = PostgresStore.from_conninfo(database, keys.verifier)
    job = store.create_job(chan, "owner", actor="planner")
    prod = PostgresProductionStore(_connect(database))
    prod.record_cost(job, "llm", 8, provider="fake-llm", detail={"what": "t"}, actor="agent:x")
    with pytest.raises(JobBudgetExceeded):
        prod.record_cost(job, "llm", 5, provider="fake-llm", detail={}, actor="agent:x")
    spend = prod.spend(job, chan)
    assert (spend.job_cents, spend.job_budget_cents) == (8, 10)
    prod.alert(chan, job, "budget.job", "cap hit")
    assert sql("SELECT kind FROM alerts WHERE job_id = %s", job) == [("budget.job",)]
    with pytest.raises(LicenseInvalid):
        prod.record_asset(
            job,
            chan,
            name="music",
            kind="music",
            sha256="sha256:" + "1" * 64,
            media_type="audio/mpeg",
            provider="x",
            license_ref=None,
        )
    sql(
        "INSERT INTO rights_register (license_ref, asset_kind, provider) "
        "VALUES ('lic_it-music', 'music', 'x')"
    )
    for _ in range(2):
        version = prod.record_asset(
            job,
            chan,
            name="music",
            kind="music",
            sha256="sha256:" + "1" * 64,
            media_type="audio/mpeg",
            provider="x",
            license_ref="lic_it-music",
        )
    assert version == 2 and prod.license_valid("lic_it-music", chan)


async def test_exit_gate_ten_shadow_jobs_reach_awaiting_approval(
    database: str, channel: Callable[..., str], sql: SQL, keys: Keys, secrets: Any
) -> None:
    chan = channel(autonomy_tier="shadow")
    sql("UPDATE channels SET daily_budget_cents = 100000 WHERE id = %s", chan)
    for ref, kind in ((FAKE_MUSIC_LICENSE, "music"), (FAKE_STOCK_LICENSE, "stock")):
        sql(
            "INSERT INTO rights_register (license_ref, asset_kind, provider) VALUES (%s, %s, "
            "'fake-stock') ON CONFLICT DO NOTHING",
            ref,
            kind,
        )
    _seed_knowledge(database, chan, "gate-approver")
    inputs = PostgresInputs.from_conninfo(database)
    ideas = [
        inputs.add(
            chan,
            InputKind.IDEA,
            submitted_by="owner",
            body=f"Personal finance myth #{i}\nSources: https://example.test/myth-{i}",
        ).id
        for i in range(10)
    ]
    inputs.add(
        chan, InputKind.GOAL, body="audience: new investors; cadence: 3/week", submitted_by="owner"
    )

    store = PostgresStore.from_conninfo(database, keys.verifier)
    assets = InMemoryAssetSource()
    providers = Providers(
        FakeLLM(), FakeTTS(), FakeImage(), FakeVideo(), FakeStockLibrary(), FakeWebFetcher()
    )
    pipeline = build_postgres_pipeline(database, assets, providers)
    client = FakeYouTubeClient()
    publisher = make_service(store, keys, assets, client, secrets)
    activities = JobActivities(store, publisher, assets, pipeline)

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="shadow-it",
            workflows=[VideoJobWorkflow],
            activities=activities.all(),
        ),
    ):
        jobs = await run_shadow_batch(
            env.client,
            store=store,
            conninfo=database,
            channel_id=chan,
            count=10,
            task_queue="shadow-it",
        )

    summary = summarize(jobs)
    assert summary["awaiting_approval"] == 10, jobs
    assert summary["avg_rework_loops"] < 2
    assert summary["published"] == 0 and client.uploads == []
    # Provenance: each job consumed the next new idea, in order; knowledge versions recorded.
    assert [j.owner_inputs for j in jobs] == [[i] for i in ideas]
    assert all(
        j.knowledge == {"banned_claims": 1, "brand_voice": 1, "hook_library": 1, "policy": 1}
        for j in jobs
    )
    assert all(j.qa_passed and j.cost_cents > 0 for j in jobs)
    assert (
        sql("SELECT count(DISTINCT job_id) FROM job_knowledge WHERE channel_id = %s", chan)[0][0]
        == 10
    )
    assert (
        sql("SELECT count(*) FROM owner_inputs WHERE channel_id = %s AND status = 'used'", chan)[0][
            0
        ]
        == 10
    )
    assert len({j.content_hash for j in jobs}) == 10
    # The database blocks publishing on the shadow tier, even with a valid approval.
    job = jobs[0]
    store.verify_approval(
        job.job_id, keys.token(job.job_id, chan, job.content_hash or ""), actor="hub:gate-approver"
    )
    with pytest.raises(ChannelNotPublishable):
        store.transition(job.job_id, JobState.SCHEDULED, actor="orchestrator")
    assert sql("SELECT audit_chain_first_break()")[0][0] is None
