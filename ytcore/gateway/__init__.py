from ytcore.gateway.clients import FakeYouTubeClient, YouTubeClient
from ytcore.gateway.service import (
    PublishRefused,
    PublishResult,
    PublishService,
    UploadFailed,
    UploadInProgress,
)

__all__ = [
    "FakeYouTubeClient",
    "PublishRefused",
    "PublishResult",
    "PublishService",
    "UploadFailed",
    "UploadInProgress",
    "YouTubeClient",
    "build_youtube_client",
]


def build_youtube_client(backend: str, env: str) -> YouTubeClient:
    """Select the client from config. Production requires real; dev refuses real."""
    if backend == "real":
        if env == "dev":
            raise RuntimeError("dev must use the fake YouTube client")
        from ytcore.gateway.youtube_api import RealYouTubeClient

        return RealYouTubeClient()
    if env == "production":
        raise RuntimeError("production requires the real YouTube client")
    return FakeYouTubeClient()
