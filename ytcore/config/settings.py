"""Typed settings with strict dev / staging / production separation.

Settings never contain secret material: credentials are SecretRef strings resolved through the
configured SecretsProvider.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ytcore.secrets.provider import (
    CloudSecretsManagerProvider,
    EnvFileSecretsProvider,
    InMemorySecretsProvider,
    SecretRef,
    SecretsError,
    SecretsProvider,
)


class Environment(StrEnum):
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"


SecretsBackend = Literal["env_file", "in_memory", "cloud"]
YouTubeBackend = Literal["fake", "real"]
StorageBackend = Literal["memory", "local", "object_store"]
LLMBackend = Literal["fake", "anthropic"]
QueueBackend = Literal["in_memory", "temporal"]

# Values that are fakes or dev-only conveniences. Production refuses every one of them.
FAKE_VALUES: dict[str, frozenset[str]] = {
    "secrets_backend": frozenset({"env_file", "in_memory"}),
    "youtube_backend": frozenset({"fake"}),
    "storage_backend": frozenset({"memory", "local"}),
    "llm_backend": frozenset({"fake"}),
    "queue_backend": frozenset({"in_memory"}),
}


class ConfigError(ValueError):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="YT_",
        extra="forbid",
        frozen=True,
        case_sensitive=False,
        # Never echo submitted values in validation errors: they could be secrets.
        hide_input_in_errors=True,
    )

    env: Environment = Field(default=Environment.DEV, validation_alias="ENV")

    secrets_backend: SecretsBackend = "env_file"
    secrets_env_file: Path = Path(".env.local")
    secrets_cloud_project: str = ""

    youtube_backend: YouTubeBackend = "fake"
    storage_backend: StorageBackend = "memory"
    llm_backend: LLMBackend = "fake"
    queue_backend: QueueBackend = "in_memory"

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "ytcore-dev"
    database_url_ref: str = "env://DATABASE_URL"
    approval_public_key_ref: str = "env://APPROVAL_PUBLIC_KEY"

    max_channels: int = Field(default=20, ge=1, le=20)

    @field_validator("database_url_ref", "approval_public_key_ref")
    @classmethod
    def _must_be_ref(cls, value: str) -> str:
        try:
            SecretRef(value)
        except SecretsError:
            raise ValueError("must be a secret reference (vault://... or env://...)") from None
        return value

    @model_validator(mode="after")
    def _enforce_environment(self) -> Settings:
        if self.env is Environment.PRODUCTION:
            bad = [
                f"{name}={getattr(self, name)}"
                for name, fakes in FAKE_VALUES.items()
                if getattr(self, name) in fakes
            ]
            if bad:
                raise ConfigError(f"production refuses fake/dev-only providers: {', '.join(bad)}")
            for name in ("database_url_ref", "approval_public_key_ref"):
                if not getattr(self, name).startswith("vault://"):
                    raise ConfigError(f"production requires vault:// references for {name}")
            if not self.temporal_namespace.endswith("prod"):
                raise ConfigError("production temporal_namespace must end with 'prod'")
        elif self.env is Environment.STAGING:
            if self.secrets_backend in FAKE_VALUES["secrets_backend"]:
                raise ConfigError("staging must use the cloud secrets manager")
            if self.temporal_namespace.endswith("prod"):
                raise ConfigError("staging must not use a production temporal namespace")
        else:
            # Dev can never reach the real YouTube API or production infrastructure.
            if self.youtube_backend == "real":
                raise ConfigError("dev must use the fake YouTube client")
            if self.temporal_namespace.endswith("prod"):
                raise ConfigError("dev must not use a production temporal namespace")
        return self


def load_settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def build_secrets_provider(settings: Settings) -> SecretsProvider:
    if settings.secrets_backend == "cloud":
        return CloudSecretsManagerProvider(project=settings.secrets_cloud_project)
    if settings.env is Environment.PRODUCTION:  # pragma: no cover - blocked by validation
        raise ConfigError("dev-only secrets provider in production")
    if settings.secrets_backend == "in_memory":
        return InMemorySecretsProvider()
    return EnvFileSecretsProvider(settings.secrets_env_file, env=settings.env.value)
