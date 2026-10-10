"""Real YouTube Data API v3 client (google-api-python-client).

Enabled only by `youtube_backend = "real"`, which production requires and dev refuses.
Install with the `youtube` extra. Never imported unless selected.
"""

from __future__ import annotations

import io
import json
from typing import Any

from ytcore.gateway.clients import ChannelCredentials, UploadMetadata, VideoStatus

SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint, not a secret
CHUNK_SIZE = 8 * 1024 * 1024


class RealYouTubeClient:
    is_fake = False

    def __init__(self, *, max_chunk_retries: int = 5) -> None:
        self._max_chunk_retries = max_chunk_retries

    @staticmethod
    def _service(credentials: ChannelCredentials) -> Any:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        client = json.loads(credentials.oauth_client.reveal())
        creds = Credentials(  # type: ignore[no-untyped-call]
            token=None,
            refresh_token=credentials.refresh_token.reveal(),
            client_id=client["client_id"],
            client_secret=client["client_secret"],
            token_uri=TOKEN_URI,
            scopes=list(SCOPES),
        )
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    def upload_resumable(
        self,
        *,
        credentials: ChannelCredentials,
        media: bytes,
        metadata: UploadMetadata,
        idempotency_key: str,
    ) -> str:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseUpload

        body = {
            "snippet": {
                "title": metadata.title,
                "description": metadata.description,
                "tags": list(metadata.tags),
                "categoryId": metadata.category_id,
            },
            "status": {
                "privacyStatus": metadata.privacy_status,
                "selfDeclaredMadeForKids": metadata.made_for_kids,
                "containsSyntheticMedia": metadata.contains_synthetic_media,
            },
        }
        upload = MediaIoBaseUpload(
            io.BytesIO(media), mimetype="video/*", chunksize=CHUNK_SIZE, resumable=True
        )
        request = (
            self._service(credentials)
            .videos()
            .insert(part="snippet,status", body=body, media_body=upload, notifySubscribers=False)
        )
        response: dict[str, Any] | None = None
        retries = 0
        while response is None:
            try:
                _, response = request.next_chunk(num_retries=3)
                retries = 0
            except HttpError as exc:
                if (
                    exc.resp.status not in {500, 502, 503, 504}
                    or retries >= self._max_chunk_retries
                ):
                    raise
                retries += 1
        return str(response["id"])

    def get_status(self, *, credentials: ChannelCredentials, video_id: str) -> VideoStatus:
        result = (
            self._service(credentials)
            .videos()
            .list(part="status", id=video_id)
            .execute(num_retries=3)
        )
        items = result.get("items", [])
        if not items:
            return VideoStatus(video_id, "deleted", "unknown")
        status = items[0]["status"]
        return VideoStatus(
            video_id, status.get("uploadStatus", "uploaded"), status["privacyStatus"]
        )
