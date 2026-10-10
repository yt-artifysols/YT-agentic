"""Worker process: wires the stores, agent pipeline and gateway into the job activities and runs
them against Temporal. Also the daily planner entry point.

    python -m ytcore.workflow.worker run         # long-running worker
    python -m ytcore.workflow.worker plan-once   # one planner pass (idempotent; run daily)

Everything external is injectable so staging-config tests can run the same wiring with fakes.
The worker holds the approval PUBLIC key only; signing lives in the approval hub.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from temporalio.client import Client
from temporalio.worker import Worker

from ytcore.agents.factory import build_postgres_pipeline, build_providers
from ytcore.agents.pipeline import Providers
from ytcore.approvals.token import ApprovalVerifier
from ytcore.config import Settings, build_secrets_provider, load_settings
from ytcore.gateway import build_youtube_client
from ytcore.gateway.assets import (
    GcsAssetSource,
    InMemoryAssetSource,
    ListableAssets,
    LocalDirAssetSource,
)
from ytcore.gateway.clients import YouTubeClient
from ytcore.gateway.service import PublishService
from ytcore.planner.daily import DailyPlanner, PlanRepository
from ytcore.planner.temporal import TemporalStarter
from ytcore.safety.scrub import install_log_scrubber
from ytcore.secrets.provider import SecretRef, SecretsProvider
from ytcore.store.postgres import PostgresStore
from ytcore.workflow.activities import JobActivities
from ytcore.workflow.video_job import VideoJobWorkflow

log = logging.getLogger(__name__)


@dataclass
class Stack:
    store: PostgresStore
    activities: JobActivities
    plans: PlanRepository
    assets: ListableAssets
    conninfo: str


def build_assets(settings: Settings) -> ListableAssets:
    if settings.storage_backend == "object_store":
        if not settings.storage_bucket:
            raise SystemExit("storage_backend=object_store requires YT_STORAGE_BUCKET")
        return GcsAssetSource(settings.storage_bucket)
    if settings.storage_backend == "local":
        return LocalDirAssetSource(Path(settings.storage_local_dir))
    return InMemoryAssetSource()


def load_verifier(settings: Settings, secrets: SecretsProvider) -> ApprovalVerifier:
    pem = secrets.get(SecretRef(settings.approval_public_key_ref)).reveal().encode()
    key = load_pem_public_key(pem)
    if not isinstance(key, Ed25519PublicKey):
        raise SystemExit("approval public key must be Ed25519")
    return ApprovalVerifier(key)


def build_stack(
    settings: Settings,
    secrets: SecretsProvider,
    *,
    conninfo: str | None = None,
    verifier: ApprovalVerifier | None = None,
    assets: ListableAssets | None = None,
    youtube: YouTubeClient | None = None,
    providers: Providers | None = None,
) -> Stack:
    dsn = conninfo or secrets.get(SecretRef(settings.database_url_ref)).reveal()
    verifier = verifier or load_verifier(settings, secrets)
    assets = assets or build_assets(settings)
    store = PostgresStore.from_conninfo(dsn, verifier)
    plans = PlanRepository.from_conninfo(dsn)
    pipeline = build_postgres_pipeline(dsn, assets, providers or build_providers(settings, secrets))
    publisher = PublishService(
        repo=store,
        verifier=verifier,
        client=youtube or build_youtube_client(settings.youtube_backend, settings.env.value),
        assets=assets,
        secrets=secrets,
        env=settings.env.value,
        upload_units=settings.youtube_upload_quota_units,
        status_units=settings.youtube_status_quota_units,
    )
    activities = JobActivities(
        store,
        publisher,
        assets,
        pipeline,
        replanner=lambda job, reason: plans.request_replan(job, reason),
    )
    return Stack(store, activities, plans, assets, dsn)


async def connect(settings: Settings) -> Client:
    return await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)


async def run_worker(settings: Settings, stack: Stack, client: Client | None = None) -> None:
    client = client or await connect(settings)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[VideoJobWorkflow],
        activities=stack.activities.all(),
    )
    await worker.run()


async def plan_once(settings: Settings, stack: Stack, client: Client | None = None) -> Any:
    client = client or await connect(settings)
    loop = asyncio.get_running_loop()
    starter = TemporalStarter(client, loop, settings.temporal_task_queue)
    planner = DailyPlanner(stack.plans, starter)
    return await asyncio.to_thread(planner.run)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI
    parser = argparse.ArgumentParser(prog="python -m ytcore.workflow.worker")
    parser.add_argument("cmd", choices=["run", "plan-once"])
    args = parser.parse_args(argv)
    install_log_scrubber()
    settings = load_settings()
    secrets = build_secrets_provider(settings)
    stack = build_stack(settings, secrets)
    if args.cmd == "run":
        asyncio.run(run_worker(settings, stack))
        return 0
    run = asyncio.run(plan_once(settings, stack))
    print(f"planned {len(run.planned)}, replanned {len(run.replanned)}, skipped {len(run.skipped)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
