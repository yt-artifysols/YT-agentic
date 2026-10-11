"""Serverless staging building blocks refuse production and keep the hub's private key where it
belongs."""

from __future__ import annotations

from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ytcore.approvals.keys import EnvKeyProvider
from ytcore.approvals.service.settings import HubSettings
from ytcore.approvals.token import ApprovalSigner, ApprovalVerifier
from ytcore.config import Settings
from ytcore.secrets.provider import (
    EnvVarSecretsProvider,
    ProductionForbiddenError,
    SecretRef,
    SecretsError,
)
from ytcore.serverless.runtime import build_hub_app, build_jobs_runtime

HASH = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ENV", "YT_ENV", "YT_HUB_ENV"):
        monkeypatch.delenv(name, raising=False)


def _pem() -> str:
    return (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )


def test_env_key_provider_signs_and_accepts_escaped_newlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YT_HUB_SIGNING_KEY_PEM", _pem().replace("\n", "\\n"))
    keys = EnvKeyProvider(env="staging")
    token, _ = ApprovalSigner(keys).issue(
        job_id="j", channel_id="c", content_hash=HASH, approver="a", ttl=timedelta(hours=1)
    )
    ApprovalVerifier(keys.public_key()).verify(token, job_id="j", channel_id="c", content_hash=HASH)


def test_env_key_provider_refuses_production_and_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YT_HUB_SIGNING_KEY_PEM", _pem())
    with pytest.raises(ProductionForbiddenError):
        EnvKeyProvider(env="production")
    monkeypatch.setenv("ENV", "production")
    with pytest.raises(ProductionForbiddenError):
        EnvKeyProvider()
    monkeypatch.delenv("ENV")
    monkeypatch.delenv("YT_HUB_SIGNING_KEY_PEM")
    with pytest.raises(ValueError, match="not set"):
        EnvKeyProvider(env="staging")


def test_env_var_secrets_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    provider = EnvVarSecretsProvider(env="staging")
    assert provider.get(SecretRef("env://DATABASE_URL")).reveal() == "postgresql://x"
    assert provider.get(SecretRef("vault://db#DATABASE_URL")).reveal() == "postgresql://x"
    with pytest.raises(SecretsError):
        provider.get(SecretRef("env://MISSING"))
    with pytest.raises(ProductionForbiddenError):
        EnvVarSecretsProvider(env="production")


def test_production_settings_refuse_the_serverless_backends() -> None:
    for field, value in (
        ("key_backend", "env"),
        ("signaler_backend", "db"),
        ("asset_backend", "db"),
    ):
        base = {
            "ENV": "production",
            "origin": "https://hub.example.com",
            "rp_id": "example.com",
            "cookie_secure": True,
            "key_backend": "kms",
            "signaler_backend": "temporal",
            "notifier_backend": "slack",
            field: value,
        }
        with pytest.raises(ValueError, match="production hub settings"):
            HubSettings(**base)  # type: ignore[arg-type]


def test_platform_secret_store_is_never_a_production_backend() -> None:
    with pytest.raises(ValueError, match=r"secrets_backend=platform_env"):
        Settings(
            env="production",  # type: ignore[arg-type]
            secrets_backend="platform_env",
        )


def test_runtime_entrypoints_refuse_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "production")
    with pytest.raises(RuntimeError, match="staging only"):
        build_hub_app()
    with pytest.raises(RuntimeError, match="staging only"):
        build_jobs_runtime()
