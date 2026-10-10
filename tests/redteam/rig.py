"""Shared rig for the red-team suite: a fake backend wired to the real gateway, plus helpers an
attacker would use. Everything here uses only public interfaces; the point of the suite is that
no sequence of calls gets a video onto YouTube without a valid human approval."""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.fakes import FakeBackend, FakeYouTubeClient, InMemoryAssetSource, fake_secrets
from tests.harness import Keys, drive_to_awaiting, make_service
from ytcore.approvals.token import ApprovalSigner
from ytcore.gateway.service import PublishService
from ytcore.workflow.states import JobState


@dataclass
class Rig:
    keys: Keys = field(default_factory=Keys)
    assets: InMemoryAssetSource = field(default_factory=InMemoryAssetSource)
    client: FakeYouTubeClient = field(default_factory=FakeYouTubeClient)

    def __post_init__(self) -> None:
        self.backend = FakeBackend(self.keys.verifier)
        self.service: PublishService = make_service(
            self.backend, self.keys, self.assets, self.client, fake_secrets()
        )

    def channel(self, channel_id: str = "chan-a", **kw: object) -> str:
        if channel_id not in self.backend.channels:
            self.backend.add_channel(channel_id, **kw)
        return channel_id

    def awaiting(self, channel_id: str = "chan-a") -> tuple[str, str]:
        self.channel(channel_id)
        job = self.backend.create_job(channel_id)
        return job, drive_to_awaiting(self.backend, self.assets, job, channel_id)

    def approved(self, channel_id: str = "chan-a") -> tuple[str, str, str]:
        job, chash = self.awaiting(channel_id)
        token = self.keys.token(job, channel_id, chash)
        self.backend.verify_approval(job, token, actor="hub:approver")
        return job, token, chash

    def scheduled(self, channel_id: str = "chan-a") -> tuple[str, str, str]:
        job, token, chash = self.approved(channel_id)
        self.backend.transition(job, JobState.SCHEDULED, actor="scheduler")
        return job, token, chash

    @staticmethod
    def attacker_token(job: str, channel: str, chash: str) -> str:
        """A well-formed token signed with a key the platform has never seen."""
        token, _ = ApprovalSigner(Ed25519PrivateKey.generate()).issue(
            job_id=job,
            channel_id=channel,
            content_hash=chash,
            approver="mallory",
            ttl=__import__("datetime").timedelta(hours=1),
        )
        return token
