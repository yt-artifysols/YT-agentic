"""Wire the approval hub from settings. Run with:

    uvicorn ytcore.approvals.service.main:app --port 8001

The database DSN and other secrets come from the SecretsProvider (see ytcore/config).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import psycopg
from fastapi import FastAPI

from ytcore.approvals.keys import KeyProvider, KmsKeyProvider, LocalFileKeyProvider
from ytcore.approvals.service.app import HubDeps, create_app
from ytcore.approvals.service.hub import ApprovalHub
from ytcore.approvals.service.notify import Notifier, build_notifier
from ytcore.approvals.service.repo import HubRepository
from ytcore.approvals.service.security import CeremonyStore, RateLimiter, SessionManager
from ytcore.approvals.service.settings import HubSettings
from ytcore.approvals.service.signals import FakeSignaler, TemporalSignaler, WorkflowSignaler
from ytcore.approvals.token import ApprovalSigner, ApprovalVerifier
from ytcore.channels.rollout import RolloutService
from ytcore.gateway.assets import AssetSource, InMemoryAssetSource
from ytcore.inputs.store import PostgresInputs
from ytcore.knowledge.store import PostgresKnowledge
from ytcore.safety.scrub import install_log_scrubber
from ytcore.store.postgres import PostgresStore
from ytcore.workflow.models import HubDecision


class LazyTemporalSignaler:
    """Connects to Temporal on first use (keeps app startup independent of Temporal)."""

    def __init__(self, address: str, namespace: str) -> None:
        self._address, self._namespace = address, namespace
        self._inner: TemporalSignaler | None = None

    async def send(self, job_id: str, decision: HubDecision) -> None:
        if self._inner is None:
            self._inner = await TemporalSignaler.connect(self._address, self._namespace)
        await self._inner.send(job_id, decision)


def build_key_provider(settings: HubSettings) -> KeyProvider:
    if settings.key_backend == "kms":
        return KmsKeyProvider(key_resource=settings.kms_key_resource)
    return LocalFileKeyProvider(settings.key_file, env=settings.env)


def create_hub_app(
    settings: HubSettings,
    *,
    conninfo: str,
    key_provider: KeyProvider | None = None,
    signaler: WorkflowSignaler | None = None,
    notifier: Notifier | None = None,
    assets: AssetSource | None = None,
    secrets: Any = None,
) -> FastAPI:
    install_log_scrubber()
    keys = key_provider or build_key_provider(settings)
    repo = HubRepository.from_conninfo(conninfo, fast_lane_max_risk=settings.fast_lane_max_risk)
    # The store only ever gets the PUBLIC key; the private key stays inside the signer.
    store = PostgresStore.from_conninfo(conninfo, ApprovalVerifier(keys.public_key()))
    if signaler is None:
        signaler = (
            LazyTemporalSignaler(settings.temporal_address, settings.temporal_namespace)
            if settings.signaler_backend == "temporal"
            else FakeSignaler()
        )
    if notifier is None:
        if secrets is None:
            from ytcore.config import build_secrets_provider, load_settings

            secrets = build_secrets_provider(load_settings())
        notifier = build_notifier(settings, secrets)
    hub = ApprovalHub(
        repo=repo,
        store=store,
        signer=ApprovalSigner(keys),
        signaler=signaler,
        notifier=notifier,
        token_ttl=timedelta(hours=settings.token_ttl_hours),
        batch_max_items=settings.batch_max_items,
        link=settings.origin,
    )
    deps = HubDeps(
        settings=settings,
        repo=repo,
        hub=hub,
        assets=resolve_assets(settings, assets),
        sessions=SessionManager(repo, settings),
        ceremonies=CeremonyStore(),
        limiter=RateLimiter(),
        inputs=PostgresInputs.from_conninfo(conninfo),
        rollout=RolloutService(lambda: psycopg.connect(conninfo, autocommit=True)),
        knowledge=PostgresKnowledge.from_conninfo(conninfo),
    )
    return create_app(deps)


def resolve_assets(settings: HubSettings, assets: AssetSource | None) -> AssetSource:
    """The reviewer must see the real video: production never falls back to an empty in-memory
    store (the preview would be blind and approval would rest on text alone)."""
    if assets is not None:
        return assets
    if settings.env == "production":
        raise RuntimeError("the production hub needs a real asset source (storage bucket)")
    return InMemoryAssetSource()


def _app_from_env() -> FastAPI:
    from ytcore.config import build_secrets_provider, load_settings
    from ytcore.gateway.assets import GcsAssetSource
    from ytcore.secrets.provider import SecretRef

    core = load_settings()
    secrets = build_secrets_provider(core)
    dsn = secrets.get(SecretRef(core.database_url_ref)).reveal()
    assets = GcsAssetSource(core.storage_bucket) if core.storage_bucket else None
    return create_hub_app(HubSettings(), conninfo=dsn, secrets=secrets, assets=assets)


def __getattr__(name: str) -> Any:  # lazy `app` for uvicorn without import-time side effects
    if name == "app":
        return _app_from_env()
    raise AttributeError(name)
