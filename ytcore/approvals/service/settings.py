"""Approval hub settings (env prefix YT_HUB_). No secrets here: keys and DSNs are references."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class HubSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="YT_HUB_", extra="forbid", frozen=True, hide_input_in_errors=True
    )

    env: Literal["dev", "staging", "production"] = Field(default="dev", validation_alias="ENV")

    # WebAuthn relying party. rp_id must be the registrable domain the UI is served from.
    rp_id: str = "localhost"
    rp_name: str = "yt-agentic approval hub"
    origin: str = "http://localhost:3000"

    session_idle_minutes: int = Field(default=15, ge=1, le=120)
    session_absolute_hours: int = Field(default=8, ge=1, le=24)
    cookie_secure: bool = False
    # Behind the UI's reverse proxy every request comes from the proxy; trust its
    # X-Forwarded-For for anonymous rate limiting only when the API is not exposed directly.
    trust_forwarded_for: bool = False
    invite_ttl_hours: int = Field(default=24, ge=1, le=168)

    # Approval policy.
    token_ttl_hours: int = Field(default=24, ge=1, le=72)
    # A fresh passkey assertion for each approval, batch approval, kill-switch release and tier
    # promotion (a stolen session cookie alone cannot approve).
    require_stepup: bool = True
    fast_lane_max_risk: int = Field(default=20, ge=0, le=100)
    batch_max_items: int = Field(default=25, ge=1, le=100)

    # Rate limits (requests per minute per client and bucket).
    rate_auth_per_minute: int = Field(default=10, ge=1)
    rate_write_per_minute: int = Field(default=60, ge=1)
    rate_read_per_minute: int = Field(default=300, ge=1)

    key_backend: Literal["local_file", "kms"] = "local_file"
    key_file: Path = Path(".local/approval_ed25519.pem")
    kms_key_resource: str = ""

    signaler_backend: Literal["fake", "temporal"] = "fake"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "ytcore-dev"

    notifier_backend: Literal["fake", "slack", "email"] = "fake"
    slack_webhook_ref: str = "env://SLACK_WEBHOOK_URL"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user_ref: str = "env://SMTP_USER"
    smtp_password_ref: str = "env://SMTP_PASSWORD"  # noqa: S105 - a reference, not a secret
    email_from: str = "hub@localhost"
    email_to: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _production_rules(self) -> HubSettings:
        if self.env == "production":
            problems = []
            if not self.origin.startswith("https://"):
                problems.append("origin must be https")
            host = self.origin.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
            if host != self.rp_id and not host.endswith("." + self.rp_id):
                problems.append("rp_id must be the origin host or a parent domain of it")
            if not self.cookie_secure:
                problems.append("cookie_secure must be true")
            if not self.require_stepup:
                problems.append("require_stepup must be true")
            if self.key_backend != "kms":
                problems.append("key_backend must be kms")
            if self.signaler_backend == "fake":
                problems.append("signaler_backend must be temporal")
            if self.notifier_backend == "fake":
                problems.append("notifier_backend must not be fake")
            if problems:
                raise ValueError("production hub settings: " + "; ".join(problems))
        return self
