"""YouTube gateway: the ONLY component that talks to the YouTube upload API.

It holds the approval PUBLIC key only and refuses any upload without a valid token bound to the
job, the channel and the exact content hash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ytcore.approvals.tokens import ApprovalClaims, ApprovalError, ApprovalVerifier


class YouTubeClient(Protocol):
    is_fake: bool

    def upload(self, *, channel_id: str, content_hash: str) -> str: ...


@dataclass
class FakeYouTubeClient:
    """In-memory client for dev and tests. Production config refuses it."""

    is_fake: bool = True
    uploads: list[tuple[str, str]] = field(default_factory=list)

    def upload(self, *, channel_id: str, content_hash: str) -> str:
        self.uploads.append((channel_id, content_hash))
        return f"fake-video-{len(self.uploads)}"


class YouTubeGateway:
    def __init__(
        self, verifier: ApprovalVerifier, client: YouTubeClient, *, env: str = "dev"
    ) -> None:
        if env == "production" and client.is_fake:
            raise RuntimeError("production refuses the fake YouTube client")
        self._verifier = verifier
        self._client = client
        self._used_tokens: set[str] = set()

    def publish(self, *, job_id: str, channel_id: str, content_hash: str, token: str) -> str:
        claims: ApprovalClaims = self._verifier.verify(
            token, job_id=job_id, channel_id=channel_id, content_hash=content_hash
        )
        # Single use: a token authorizes exactly one upload.
        if claims.token_id in self._used_tokens:
            raise ApprovalError("token already used")
        self._used_tokens.add(claims.token_id)
        return self._client.upload(channel_id=channel_id, content_hash=content_hash)
