"""Operator reconciliation for uploads left `pending` by a crash.

If a worker dies after YouTube accepted a video but before the gateway recorded it, the upload
stays `pending` and every retry is refused (UploadInProgress) rather than risk a second video.
An operator checks YouTube Studio for the video and then either ADOPTS the upload (records the
video id) or RELEASES it (no video exists: the job fails and is re-planned). Both are audited
under the operator's name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from ytcore.store.postgres import Connect, PostgresStore, map_db_error


@dataclass(frozen=True)
class PendingUpload:
    upload_id: str
    job_id: str
    channel_id: str
    created_at: datetime
    idempotency_key: str


def pending_uploads(connect: Connect) -> list[PendingUpload]:
    try:
        with connect() as conn:
            rows: list[Any] = conn.execute(
                "SELECT id::text, job_id::text, channel_id, created_at, idempotency_key "
                "FROM uploads WHERE status = 'pending' ORDER BY created_at"
            ).fetchall()
    except psycopg.Error as exc:
        raise map_db_error(exc) from exc
    return [PendingUpload(*r) for r in rows]


def adopt_upload(store: PostgresStore, upload_id: str, video_id: str, *, operator: str) -> None:
    """The video exists on YouTube: record it (job -> published). The gateway's next publish
    returns this upload as a duplicate instead of uploading again."""
    if not video_id.strip():
        raise ValueError("a video id is required")
    store.complete_upload(upload_id, video_id.strip(), actor=f"operator:{operator}")


def release_upload(store: PostgresStore, upload_id: str, reason: str, *, operator: str) -> None:
    """No video exists on YouTube: mark the upload failed (job -> failed) so the job can be
    re-planned. Never use this without checking YouTube first."""
    if len(reason.strip()) < 10:
        raise ValueError("a reason of at least 10 characters is required")
    store.fail_upload(upload_id, f"released by operator: {reason}", actor=f"operator:{operator}")
