"""Shadow-mode batch: N consecutive jobs through the real workflow to awaiting_approval.

The channel must be on the shadow tier, so even an approval could never publish (the database
refuses `scheduled`/`publishing` for shadow channels). The report lists every job's title,
content hash, QA result, rework loops and cost for human review.

    python -m ytcore.agents.shadow --channel CHAN --count 10 [--out shadow-report.json]

Uses the configured database (YT_DATABASE_URL_REF) and a local Temporal dev server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from typing import Any

import psycopg
from temporalio.client import Client
from temporalio.worker import Worker

from ytcore.workflow.activities import JobActivities
from ytcore.workflow.models import VideoJobInput
from ytcore.workflow.states import JobState
from ytcore.workflow.video_job import VideoJobWorkflow, workflow_id_for

DONE = {JobState.AWAITING_APPROVAL.value, "failed", "cancelled", "rejected"}


@dataclass(frozen=True)
class ShadowJob:
    job_id: str
    state: str
    title: str
    content_hash: str | None
    qa_passed: bool | None
    risk_score: int | None
    rework_loops: int
    cost_cents: int
    owner_inputs: list[str]
    knowledge: dict[str, int]


def _job_report(conninfo: str, job_id: str) -> ShadowJob:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        row = conn.execute(
            """
            SELECT j.state, j.manifest, j.content_hash,
                   (SELECT count(*) FROM job_events e WHERE e.job_id = j.id
                      AND e.from_state = 'qa' AND e.to_state IN ('scripting', 'rendering')),
                   (SELECT coalesce(sum(cost_cents), 0) FROM job_costs c WHERE c.job_id = j.id),
                   (SELECT passed FROM qa_reports q WHERE q.job_id = j.id ORDER BY id DESC LIMIT 1),
                   (SELECT risk_score FROM qa_reports q WHERE q.job_id = j.id
                    ORDER BY id DESC LIMIT 1)
            FROM video_jobs j WHERE j.id = %s
            """,
            (job_id,),
        ).fetchone()
    assert row is not None
    manifest: dict[str, Any] = row[1] or {}
    prov = manifest.get("provenance", {})
    return ShadowJob(
        job_id=job_id,
        state=row[0],
        title=str(manifest.get("metadata", {}).get("title", "")),
        content_hash=row[2],
        qa_passed=row[5],
        risk_score=row[6],
        rework_loops=int(row[3]),
        cost_cents=int(row[4]),
        owner_inputs=list(prov.get("owner_inputs", [])),
        knowledge=dict(prov.get("knowledge", {})),
    )


def require_shadow(conninfo: str, channel_id: str) -> None:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        row = conn.execute(
            "SELECT autonomy_tier FROM channels WHERE id = %s", (channel_id,)
        ).fetchone()
    if row is None or row[0] != "shadow":
        raise SystemExit(f"channel {channel_id} must exist and be on the shadow tier")


async def run_shadow_batch(
    client: Client,
    *,
    store: Any,
    conninfo: str,
    channel_id: str,
    count: int,
    task_queue: str,
    timeout_s: float = 120.0,
) -> list[ShadowJob]:
    """Run `count` jobs one after another (a worker must be polling `task_queue`)."""
    require_shadow(conninfo, channel_id)
    reports = []
    for _ in range(count):
        job_id = store.create_job(channel_id, "planner", actor="planner")
        handle = await client.start_workflow(
            VideoJobWorkflow.run,
            VideoJobInput(job_id=job_id, channel_id=channel_id, origin="planner"),
            id=workflow_id_for(job_id),
            task_queue=task_queue,
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while store.get_state(job_id).value not in DONE:
            if loop.time() > deadline:
                raise TimeoutError(f"job {job_id} did not settle")
            await asyncio.sleep(0.1)
        reports.append(_job_report(conninfo, job_id))
        if reports[-1].state != JobState.AWAITING_APPROVAL.value:
            await handle.result()  # surface the failure reason in the workflow history
    return reports


def summarize(jobs: list[ShadowJob]) -> dict[str, Any]:
    awaiting = [j for j in jobs if j.state == JobState.AWAITING_APPROVAL.value]
    return {
        "jobs": len(jobs),
        "awaiting_approval": len(awaiting),
        "avg_rework_loops": sum(j.rework_loops for j in jobs) / len(jobs) if jobs else 0.0,
        "total_cost_cents": sum(j.cost_cents for j in jobs),
        "published": 0,
    }


async def _main(args: argparse.Namespace) -> int:  # pragma: no cover - needs Temporal + DB
    from temporalio.testing import WorkflowEnvironment

    from ytcore.agents.factory import build_postgres_pipeline, build_providers
    from ytcore.approvals.token import ApprovalVerifier
    from ytcore.config import build_secrets_provider, load_settings
    from ytcore.gateway.assets import InMemoryAssetSource
    from ytcore.gateway.clients import FakeYouTubeClient
    from ytcore.gateway.service import PublishService
    from ytcore.secrets.provider import SecretRef
    from ytcore.store.postgres import PostgresStore

    settings = load_settings()
    secrets = build_secrets_provider(settings)
    conninfo = secrets.get(SecretRef(settings.database_url_ref)).reveal()
    # Shadow runs never publish (the database refuses it for shadow channels); the gateway gets
    # only the hub's PUBLIC key, like everywhere else, and the fake YouTube client.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    pem = secrets.get(SecretRef(settings.approval_public_key_ref)).reveal().encode()
    public_key = load_pem_public_key(pem)
    if not isinstance(public_key, Ed25519PublicKey):
        raise SystemExit("approval public key must be Ed25519")
    verifier = ApprovalVerifier(public_key)
    store = PostgresStore.from_conninfo(conninfo, verifier)
    assets = InMemoryAssetSource()
    pipeline = build_postgres_pipeline(conninfo, assets, build_providers(settings, secrets))
    publisher = PublishService(
        repo=store, verifier=verifier, client=FakeYouTubeClient(), assets=assets, secrets=secrets
    )
    activities = JobActivities(store, publisher, assets, pipeline)
    async with (
        await WorkflowEnvironment.start_local(namespace="default") as env,
        Worker(
            env.client,
            task_queue="shadow",
            workflows=[VideoJobWorkflow],
            activities=activities.all(),
        ),
    ):
        jobs = await run_shadow_batch(
            env.client,
            store=store,
            conninfo=conninfo,
            channel_id=args.channel,
            count=args.count,
            task_queue="shadow",
        )
    report: dict[str, Any] = {"summary": summarize(jobs), "jobs": [asdict(j) for j in jobs]}
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    return 0 if report["summary"]["awaiting_approval"] == len(jobs) else 1


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(prog="python -m ytcore.agents.shadow")
    parser.add_argument("--channel", required=True)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--out")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
