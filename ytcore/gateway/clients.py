"""YouTube client interface and the default fake implementation.

Only the gateway service (ytcore/gateway/service.py) may hold or call a YouTubeClient.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from ytcore.secrets.provider import SecretValue


@dataclass(frozen=True)
class ChannelCredentials:
    """Resolved per channel through the SecretsProvider; never logged (SecretValue masks)."""

    refresh_token: SecretValue
    oauth_client: SecretValue  # JSON {"client_id": ..., "client_secret": ...}


@dataclass(frozen=True)
class UploadMetadata:
    title: str
    description: str
    tags: tuple[str, ...]
    category_id: str
    privacy_status: str  # private | unlisted | public
    made_for_kids: bool
    contains_synthetic_media: bool  # YouTube altered/synthetic content disclosure


@dataclass(frozen=True)
class VideoStatus:
    video_id: str
    upload_status: str  # uploaded | processed | failed | rejected | deleted
    privacy_status: str

    @property
    def is_live(self) -> bool:
        return self.upload_status == "processed"

    @property
    def is_rejected(self) -> bool:
        return self.upload_status in {"failed", "rejected", "deleted"}


class YouTubeClient(Protocol):
    is_fake: bool

    def upload_resumable(
        self,
        *,
        credentials: ChannelCredentials,
        media: bytes,
        metadata: UploadMetadata,
        idempotency_key: str,
    ) -> str:
        """Upload with a resumable session and return the YouTube video id."""
        ...

    def get_status(self, *, credentials: ChannelCredentials, video_id: str) -> VideoStatus: ...


@dataclass
class FakeYouTubeClient:
    """Default client. Records uploads in memory, chunked like a resumable upload.

    Production config and the gateway refuse it.
    """

    chunk_size: int = 256 * 1024
    fail_uploads: bool = False
    status_after_upload: str = "processed"
    is_fake: bool = True
    uploads: list[tuple[str, UploadMetadata, str]] = field(default_factory=list)
    chunks_sent: int = 0
    _by_key: dict[str, str] = field(default_factory=dict)

    def upload_resumable(
        self,
        *,
        credentials: ChannelCredentials,
        media: bytes,
        metadata: UploadMetadata,
        idempotency_key: str,
    ) -> str:
        if self.fail_uploads:
            raise RuntimeError("fake upload failure")
        if not credentials.refresh_token.reveal():
            raise RuntimeError("missing channel credentials")
        for offset in range(0, max(len(media), 1), self.chunk_size):
            _ = media[offset : offset + self.chunk_size]
            self.chunks_sent += 1
        video_id = f"fake-{uuid.uuid4().hex[:11]}"
        self.uploads.append((video_id, metadata, idempotency_key))
        self._by_key[idempotency_key] = video_id
        return video_id

    def get_status(self, *, credentials: ChannelCredentials, video_id: str) -> VideoStatus:
        privacy = next((m.privacy_status for v, m, _ in self.uploads if v == video_id), "private")
        return VideoStatus(video_id, self.status_after_upload, privacy)
