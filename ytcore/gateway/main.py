"""The gateway service entry point: `uvicorn ytcore.gateway.main:app` (YT_COMPONENT=gateway).

It builds the only PublishService in production: it reads the channel OAuth tokens, holds the
YouTube client and the PUBLIC verification key, and connects to Postgres as the `yt_gateway`
role. Callers are authenticated; the only route that does anything is POST /v1/publish.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from ytcore.config import Environment, Settings, build_secrets_provider, load_settings
from ytcore.gateway import build_youtube_client
from ytcore.gateway.auth import Authenticator, GoogleIdTokenAuthenticator, StaticBearerAuthenticator
from ytcore.gateway.server import create_gateway_app
from ytcore.gateway.service import PublishService
from ytcore.safety.scrub import install_log_scrubber
from ytcore.secrets.provider import SecretRef
from ytcore.store.postgres import PostgresStore


def build_authenticator(settings: Settings, secrets: Any) -> Authenticator:
    if settings.env is Environment.PRODUCTION or settings.gateway_audience:
        return GoogleIdTokenAuthenticator(
            settings.gateway_audience, frozenset(settings.gateway_allowed_callers)
        )
    token = secrets.get(SecretRef(settings.gateway_static_token_ref)).reveal()
    return StaticBearerAuthenticator(token)


def create_app_from_settings(settings: Settings | None = None) -> FastAPI:
    from ytcore.workflow.worker import build_assets, load_verifier  # local: keeps imports light

    install_log_scrubber()
    settings = settings or load_settings()
    secrets = build_secrets_provider(settings)
    verifier = load_verifier(settings, secrets)
    dsn = secrets.get(SecretRef(settings.database_url_ref)).reveal()
    service = PublishService(
        repo=PostgresStore.from_conninfo(dsn, verifier),
        verifier=verifier,
        client=build_youtube_client(settings.youtube_backend, settings.env.value),
        assets=build_assets(settings),
        secrets=secrets,
        env=settings.env.value,
        upload_units=settings.youtube_upload_quota_units,
        status_units=settings.youtube_status_quota_units,
    )
    return create_gateway_app(service, build_authenticator(settings, secrets))


def __getattr__(name: str) -> Any:  # lazy `app` for uvicorn without import-time side effects
    if name == "app":
        return create_app_from_settings()
    raise AttributeError(name)
