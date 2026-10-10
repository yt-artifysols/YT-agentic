from __future__ import annotations

import pytest
from pydantic import ValidationError

from ytcore.config import Environment, Settings, build_secrets_provider
from ytcore.secrets import CloudSecretsManagerProvider, EnvFileSecretsProvider

PROD_OK = {
    "ENV": "production",
    "secrets_backend": "cloud",
    "secrets_cloud_project": "yt-prod",
    "youtube_backend": "real",
    "storage_backend": "object_store",
    "llm_backend": "anthropic",
    "queue_backend": "temporal",
    "temporal_namespace": "ytcore-prod",
    "database_url_ref": "vault://platform/db#url",
    "approval_public_key_ref": "vault://approvals/public#key",
    "llm_api_key_ref": "vault://providers/anthropic#api_key",
    "web_backend": "http",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("ENV", "YT_SECRETS_BACKEND", "YT_YOUTUBE_BACKEND"):
        monkeypatch.delenv(key, raising=False)


def test_defaults_are_dev_with_fakes() -> None:
    s = Settings()
    assert s.env is Environment.DEV
    assert s.youtube_backend == "fake"
    assert isinstance(build_secrets_provider(s), EnvFileSecretsProvider)


def test_valid_production_settings() -> None:
    # Production stays blocked until real media providers exist (only fakes so far).
    with pytest.raises(ValidationError, match="fake media providers"):
        Settings(**PROD_OK)
    s = Settings.model_construct(**{**PROD_OK, "env": Environment.PRODUCTION})
    assert isinstance(build_secrets_provider(s), CloudSecretsManagerProvider)


@pytest.mark.parametrize(
    ("field", "fake"),
    [
        ("secrets_backend", "env_file"),
        ("secrets_backend", "in_memory"),
        ("youtube_backend", "fake"),
        ("storage_backend", "memory"),
        ("storage_backend", "local"),
        ("llm_backend", "fake"),
        ("web_backend", "fake"),
        ("queue_backend", "in_memory"),
    ],
)
def test_production_rejects_fakes(field: str, fake: str) -> None:
    with pytest.raises(ValidationError, match="production refuses"):
        Settings(**{**PROD_OK, field: fake})


def test_production_rejects_fakes_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "production")
    with pytest.raises(ValidationError, match="production refuses"):
        Settings()


def test_production_requires_vault_refs() -> None:
    with pytest.raises(ValidationError, match=r"vault://"):
        Settings(**{**PROD_OK, "database_url_ref": "env://DATABASE_URL"})


def test_production_requires_prod_namespace() -> None:
    with pytest.raises(ValidationError, match="namespace"):
        Settings(**{**PROD_OK, "temporal_namespace": "ytcore-dev"})


def test_staging_rejects_env_file_secrets() -> None:
    with pytest.raises(ValidationError, match="staging"):
        Settings(ENV="staging", secrets_backend="env_file", temporal_namespace="ytcore-staging")


def test_dev_cannot_use_real_youtube_or_prod_namespace() -> None:
    with pytest.raises(ValidationError, match="fake YouTube"):
        Settings(youtube_backend="real")
    with pytest.raises(ValidationError, match="production temporal"):
        Settings(temporal_namespace="ytcore-prod")


def test_secret_fields_must_be_references_and_errors_do_not_echo_them() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(database_url_ref="postgres://user:dummy-pw@host/db")
    assert "dummy-pw" not in str(exc.value)


def test_unknown_settings_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(youtube_api_key="literal")  # type: ignore[call-arg]
