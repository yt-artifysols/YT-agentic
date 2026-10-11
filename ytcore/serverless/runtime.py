"""Wiring for the Vercel staging deployment. Two services, two database logins:

  hub     (yt_hub)    approval API: signs and records approvals, passkeys, console
  jobs    (yt_worker) job driver (cron): production stages and publishing of approved jobs
          (yt_ops)    planner (cron), a separate login in a separate variable

Everything here refuses ENV=production: production keeps Temporal, KMS and the separate
gateway service. Real YouTube publishing is never enabled here (fake client only)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import FastAPI

from ytcore.approvals.service.main import create_hub_app
from ytcore.approvals.service.notify import FakeNotifier
from ytcore.approvals.service.settings import HubSettings
from ytcore.config import Environment, build_secrets_provider, load_settings
from ytcore.planner.daily import DailyPlanner, FakeWorkflowStarter, PlanRepository
from ytcore.secrets.provider import SecretRef
from ytcore.serverless.assets import PostgresAssetSource
from ytcore.serverless.driver import JobDriver
from ytcore.serverless.signaler import DbSignaler
from ytcore.workflow.worker import build_stack


def _refuse_production() -> None:
    if os.environ.get("ENV", "").strip().lower() == "production" or (
        os.environ.get("YT_ENV", "").strip().lower() == "production"
    ):
        raise RuntimeError("the serverless runtime is for staging only")


def require_service_role(dsn: str, role: str) -> None:
    """A serverless service must connect as its own limited login. Refuse a superuser, the table
    owner (the migration login) or any login that is not a member of the service role: a leaked
    or mis-wired owner URL would otherwise silently defeat the role separation (ADR 0013)."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT pg_has_role(current_user, %s, 'USAGE'), "
            "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
            "COALESCE(pg_has_role(current_user, "
            "  (SELECT tableowner FROM pg_tables WHERE tablename = 'video_jobs'), 'MEMBER') "
            "  AND current_user <> %s, false) OR "
            "COALESCE(current_user = (SELECT tableowner FROM pg_tables "
            "  WHERE tablename = 'video_jobs'), false)",
            (role, role),
        ).fetchone()
    assert row is not None
    member, superuser, owner = row
    if not member or superuser or owner:
        raise RuntimeError(
            f"refusing to start: the database login must be a limited member of {role}, "
            "not a superuser or the owner (use the service login from the bootstrap)"
        )


def build_hub_app() -> FastAPI:
    """The approval hub on Vercel: key from the platform secret, decisions and ceremonies in
    Postgres, assets in Postgres."""
    _refuse_production()
    settings = HubSettings(
        key_backend="env",
        signaler_backend="db",
        ceremony_backend="db",
        asset_backend="db",
    )
    dsn = os.environ["YT_HUB_DATABASE_URL"]
    require_service_role(dsn, "yt_hub")
    signaler = DbSignaler(
        dsn,
        wake_url=os.environ.get("JOBS_WAKE_URL", ""),
        wake_secret=os.environ.get("CRON_SECRET", ""),
    )
    return create_hub_app(settings, conninfo=dsn, signaler=signaler, notifier=FakeNotifier())


@dataclass
class JobsRuntime:
    driver: JobDriver
    planner: DailyPlanner | None


def build_jobs_runtime() -> JobsRuntime:
    _refuse_production()
    settings = load_settings()
    if settings.env is Environment.PRODUCTION or settings.youtube_backend == "real":
        raise RuntimeError("staging runtime: real YouTube publishing needs the gateway service")
    secrets = build_secrets_provider(settings)
    dsn = secrets.get(SecretRef(settings.database_url_ref)).reveal()
    require_service_role(dsn, "yt_worker")
    stack = build_stack(
        settings,
        secrets,
        conninfo=dsn,
        assets=PostgresAssetSource(dsn),
        hub_records_approvals=True,
    )
    planner = None
    ops_dsn = os.environ.get("YT_OPS_DATABASE_URL", "")
    if ops_dsn:
        require_service_role(ops_dsn, "yt_ops")
        # Jobs are rows the driver picks up; nothing needs "starting".
        planner = DailyPlanner(PlanRepository.from_conninfo(ops_dsn), FakeWorkflowStarter())
    return JobsRuntime(JobDriver(stack.activities, dsn), planner)
