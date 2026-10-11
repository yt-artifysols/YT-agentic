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

import psycopg
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from temporalio.client import Client
from temporalio.worker import Worker

from ytcore.agents.factory import build_postgres_pipeline, build_providers
from ytcore.agents.pipeline import Providers
from ytcore.approvals.token import ApprovalVerifier
from ytcore.config import Environment, Settings, build_secrets_provider, load_settings
from ytcore.gateway import build_youtube_client
from ytcore.gateway.assets import (
    GcsAssetSource,
    InMemoryAssetSource,
    ListableAssets,
    LocalDirAssetSource,
)
from ytcore.gateway.clients import YouTubeClient
from ytcore.gateway.remote import RemoteGateway, google_id_token_provider
from ytcore.gateway.service import PublishService
from ytcore.planner.daily import DailyPlanner, PlanRepository
from ytcore.planner.temporal import TemporalStarter
from ytcore.safety.scrub import install_log_scrubber
from ytcore.scheduling.cluster import PostgresClusterState
from ytcore.scheduling.pools import PoolLimiter
from ytcore.secrets.provider import SecretRef, SecretsProvider
from ytcore.store.postgres import PostgresStore
from ytcore.workflow.activities import JobActivities, Publisher
from ytcore.workflow.supervisor import ClusterActivities, ClusterSupervisorWorkflow
from ytcore.workflow.video_job import VideoJobWorkflow

log = logging.getLogger(__name__)


@dataclass
class Stack:
    store: PostgresStore
    activities: JobActivities
    cluster: ClusterActivities
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


def build_gateway(settings: Settings, secrets: SecretsProvider) -> Publisher | None:
    """The remote gateway when one is configured (always, in production); else None."""
    if not settings.gateway_url:
        return None
    if settings.gateway_static_token_ref:  # dev/test only; production validation refuses it
        token = secrets.get(SecretRef(settings.gateway_static_token_ref)).reveal()
        return RemoteGateway(settings.gateway_url, lambda: token, allow_insecure=True)
    audience = settings.gateway_audience or settings.gateway_url
    return RemoteGateway(settings.gateway_url, google_id_token_provider(audience))


def build_stack(
    settings: Settings,
    secrets: SecretsProvider,
    *,
    conninfo: str | None = None,
    verifier: ApprovalVerifier | None = None,
    assets: ListableAssets | None = None,
    youtube: YouTubeClient | None = None,
    providers: Providers | None = None,
    pool_poll_seconds: float = 1.0,
    gateway: Publisher | None = None,
    hub_records_approvals: bool = False,
) -> Stack:
    dsn = conninfo or secrets.get(SecretRef(settings.database_url_ref)).reveal()
    verifier = verifier or load_verifier(settings, secrets)
    assets = assets or build_assets(settings)
    store = PostgresStore.from_conninfo(dsn, verifier)
    plans = PlanRepository.from_conninfo(dsn)
    pipeline = build_postgres_pipeline(dsn, assets, providers or build_providers(settings, secrets))
    publisher = gateway or build_gateway(settings, secrets)
    separated = publisher is not None
    if publisher is None:
        if settings.env is Environment.PRODUCTION:  # pragma: no cover - blocked by validation
            raise SystemExit("production workers publish only through the gateway service")
        # Dev and tests: the same PublishService, in this process.
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

    def connect() -> psycopg.Connection[Any]:
        return psycopg.connect(dsn, autocommit=True)

    activities = JobActivities(
        store,
        publisher,
        assets,
        pipeline,
        replanner=lambda job, reason: plans.request_replan(job, reason),
        limiter=PoolLimiter(connect),
        pool_poll_seconds=pool_poll_seconds,
        hub_records_approvals=separated or hub_records_approvals,
    )
    state = PostgresClusterState(connect)
    cluster = ClusterActivities(state.limits, state.report)
    return Stack(store, activities, cluster, plans, assets, dsn)


async def connect(settings: Settings) -> Client:
    return await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)


async def run_worker(settings: Settings, stack: Stack, client: Client | None = None) -> None:
    client = client or await connect(settings)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[VideoJobWorkflow, ClusterSupervisorWorkflow],
        activities=[*stack.activities.all(), *stack.cluster.all()],
    )
    await worker.run()


async def plan_once(
    settings: Settings, stack: Stack | PlanRepository, client: Client | None = None
) -> Any:
    plans = stack.plans if isinstance(stack, Stack) else stack
    client = client or await connect(settings)
    loop = asyncio.get_running_loop()
    starter = TemporalStarter(
        client, loop, settings.temporal_task_queue, supervised=settings.cluster_supervisors
    )
    planner = DailyPlanner(plans, starter)
    return await asyncio.to_thread(planner.run)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI
    parser = argparse.ArgumentParser(prog="python -m ytcore.workflow.worker")
    parser.add_argument("cmd", choices=["run", "plan-once"])
    args = parser.parse_args(argv)
    install_log_scrubber()
    settings = load_settings()
    secrets = build_secrets_provider(settings)
    if args.cmd == "run":
        asyncio.run(run_worker(settings, build_stack(settings, secrets)))
        return 0
    # The planner is an ops job: it needs only the plan tables and Temporal, never the
    # production pipeline, assets or the gateway.
    dsn = secrets.get(SecretRef(settings.database_url_ref)).reveal()
    run = asyncio.run(plan_once(settings, PlanRepository.from_conninfo(dsn)))
    print(f"planned {len(run.planned)}, replanned {len(run.replanned)}, skipped {len(run.skipped)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
