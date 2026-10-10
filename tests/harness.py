"""Shared wiring for gateway and pipeline tests (fake or Postgres backends)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ytcore.approvals.token import ApprovalSigner, ApprovalVerifier
from ytcore.gateway.assets import InMemoryAssetSource
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.gateway.service import PublishService
from ytcore.secrets.provider import SecretsProvider
from ytcore.workflow.states import JobState

PRODUCTION_STEPS = (
    JobState.RESEARCHING,
    JobState.SCRIPTING,
    JobState.RENDERING,
    JobState.QA,
)


class Keys:
    """The approval hub's signer (tests only) and the public verifier everyone else gets."""

    def __init__(self) -> None:
        self.signer = ApprovalSigner(Ed25519PrivateKey.generate())
        self.verifier = ApprovalVerifier(self.signer.public_key())

    def token(
        self, job_id: str, channel_id: str, chash: str, *, ttl: timedelta = timedelta(hours=24)
    ) -> str:
        token, _ = self.signer.issue(
            job_id=job_id,
            channel_id=channel_id,
            content_hash=chash,
            approver="owner@example.test",
            ttl=ttl,
        )
        return token


def manifest_for(job_id: str, channel_id: str, video_digest: str) -> dict[str, Any]:
    return {
        "metadata": {
            "job_id": job_id,
            "channel_id": channel_id,
            "title": "Test video",
            "description": "",
            "tags": ["test"],
            "category_id": "22",
            "privacy_status": "private",
            "made_for_kids": False,
            "contains_synthetic_media": True,
        },
        "assets": {"video": video_digest},
    }


def make_service(
    backend: Any,
    keys: Keys,
    assets: InMemoryAssetSource,
    client: FakeYouTubeClient,
    secrets: SecretsProvider,
) -> PublishService:
    return PublishService(
        repo=backend, verifier=keys.verifier, client=client, assets=assets, secrets=secrets
    )


def drive_to_awaiting(
    backend: Any, assets: InMemoryAssetSource, job_id: str, channel_id: str, actor: str = "test"
) -> str:
    """Walk an existing job through production and submit it; return the content hash."""
    for state in PRODUCTION_STEPS:
        backend.transition(job_id, state, actor=actor)
    digest = assets.put(backend.storage_prefix(channel_id), f"video-{job_id}".encode())
    return str(
        backend.submit_for_approval(job_id, manifest_for(job_id, channel_id, digest), actor=actor)
    )
