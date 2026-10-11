"""YouTube gateway: the ONLY publish path.

It holds the approval PUBLIC key only (ApprovalVerifier) and refuses to upload unless:
- the job is `scheduled` or `publishing` in the database,
- the token verifies for the job, its channel and the job's CURRENT content hash, and its jti
  was consumed by the approval step for that content,
- the channel and its cluster are active and the channel's autonomy tier allows publishing,
- the manifest still hashes to the approved content hash and asset bytes match their digests.

Uploads are idempotent (one key per job and content hash), quota is charged per Google project
before each API call, and the video is verified live after upload.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from opentelemetry import trace

from ytcore.approvals.hashing import content_hash
from ytcore.approvals.token import ApprovalClaims, ApprovalError, ApprovalVerifier
from ytcore.gateway.assets import AssetSource, read_verified
from ytcore.gateway.clients import ChannelCredentials, UploadMetadata, YouTubeClient
from ytcore.observability.metrics import record_publish
from ytcore.observability.tracing import job_span
from ytcore.safety.assets import AssetRejected, check_asset
from ytcore.safety.checks import ensure_disclosed
from ytcore.secrets.provider import SecretRef, SecretsProvider
from ytcore.store.errors import QuotaExhausted
from ytcore.store.models import PublishContext, UploadRecord
from ytcore.workflow.states import JobState

log = logging.getLogger(__name__)

# YouTube Data API v3 quota costs (units). Configurable to track Google's published costs.
DEFAULT_UPLOAD_UNITS = 1600
DEFAULT_STATUS_UNITS = 1


class PublishRefused(Exception):
    """The gateway refused to publish. Never retried automatically."""


class UploadFailed(Exception):
    pass


class UploadInProgress(Exception):
    """Another attempt holds this upload (pending). Retry later; never upload twice."""


@dataclass(frozen=True)
class PublishResult:
    job_id: str
    video_id: str
    state: JobState  # published (uploaded, processing) or live (verified)
    upload_id: str
    duplicate: bool = False


class GatewayRepository(Protocol):
    def publish_context(self, job_id: str) -> PublishContext: ...

    def approval_consumed(self, job_id: str, jti: str, content_hash: str) -> bool: ...

    def find_upload(self, idempotency_key: str) -> UploadRecord | None: ...

    def begin_upload(
        self,
        ctx: PublishContext,
        *,
        idempotency_key: str,
        approval_jti: str,
        synthetic_disclosure: bool,
        quota_units: int,
        actor: str,
    ) -> UploadRecord:
        """Atomically: scheduled -> publishing (if needed), charge quota, insert pending upload."""
        ...

    def complete_upload(self, upload_id: str, video_id: str, *, actor: str) -> None:
        """Atomically: upload -> uploaded, job publishing -> published."""
        ...

    def mark_live(self, upload_id: str, *, actor: str) -> None:
        """Atomically: upload -> live, job published -> live."""
        ...

    def fail_upload(self, upload_id: str, reason: str, *, actor: str) -> None:
        """Atomically: upload -> failed, job -> failed."""
        ...

    def charge_quota(
        self, google_project_id: str, units: int, operation: str, *, job_id: str, actor: str
    ) -> None: ...


def idempotency_key(job_id: str, chash: str) -> str:
    return "upl_" + hashlib.sha256(f"{job_id}:{chash}".encode()).hexdigest()


def _metadata(manifest: dict[str, Any]) -> UploadMetadata:
    meta = manifest.get("metadata", {})
    synthetic = meta.get("contains_synthetic_media")
    if not isinstance(synthetic, bool):
        raise PublishRefused("manifest must declare contains_synthetic_media explicitly")
    # Generated voice, images or video are realistic synthetic media: the upload must say so. A
    # manifest that does not describe its assets is treated as generated (the conservative case).
    info = manifest.get("asset_info")
    try:
        ensure_disclosed(
            info if isinstance(info, dict) else {"unknown": {"kind": "video"}}, synthetic
        )
    except ValueError as exc:
        raise PublishRefused(str(exc)) from None
    privacy = meta.get("privacy_status", "private")
    if privacy not in {"private", "unlisted", "public"}:
        raise PublishRefused(f"invalid privacy_status {privacy!r}")
    return UploadMetadata(
        title=str(meta.get("title", ""))[:100],
        description=str(meta.get("description", ""))[:5000],
        tags=tuple(str(t) for t in meta.get("tags", [])),
        category_id=str(meta.get("category_id", "22")),
        privacy_status=privacy,
        made_for_kids=bool(meta.get("made_for_kids", False)),
        contains_synthetic_media=synthetic,
    )


class PublishService:
    def __init__(
        self,
        *,
        repo: GatewayRepository,
        verifier: ApprovalVerifier,
        client: YouTubeClient,
        assets: AssetSource,
        secrets: SecretsProvider,
        env: str = "dev",
        upload_units: int = DEFAULT_UPLOAD_UNITS,
        status_units: int = DEFAULT_STATUS_UNITS,
    ) -> None:
        if env == "production" and client.is_fake:
            raise RuntimeError("production refuses the fake YouTube client")
        if env == "production" and secrets.dev_only:
            raise RuntimeError("production refuses dev-only secrets providers")
        self._repo = repo
        self._verifier = verifier
        self._client = client
        self._assets = assets
        self._secrets = secrets
        self._upload_units = upload_units
        self._status_units = status_units

    def _credentials(self, ctx: PublishContext) -> ChannelCredentials:
        assert ctx.credentials_ref is not None and ctx.oauth_client_ref is not None
        return ChannelCredentials(
            refresh_token=self._secrets.get(SecretRef(ctx.credentials_ref)),
            oauth_client=self._secrets.get(SecretRef(ctx.oauth_client_ref)),
        )

    def _verify(self, ctx: PublishContext, token: str) -> ApprovalClaims:
        if ctx.content_hash is None:
            raise PublishRefused("job has no content hash")
        try:
            claims = self._verifier.verify(
                token, job_id=ctx.job_id, channel_id=ctx.channel_id, content_hash=ctx.content_hash
            )
        except ApprovalError as exc:
            raise PublishRefused(f"approval token rejected: {exc}") from None
        if not self._repo.approval_consumed(ctx.job_id, claims.jti, ctx.content_hash):
            raise PublishRefused("no recorded approval for this token and content")
        return claims

    def publish(self, job_id: str, token: str, *, actor: str = "gateway") -> PublishResult:
        with job_span("gateway.publish", job_id) as span:
            try:
                result = self._publish(job_id, token, actor)
            except Exception as exc:
                record_publish(type(exc).__name__)
                raise
            span.set_attribute("publish.duplicate", result.duplicate)
            span.set_attribute("publish.state", result.state.value)
            record_publish("duplicate" if result.duplicate else "ok")
            return result

    def _publish(self, job_id: str, token: str, actor: str) -> PublishResult:
        ctx = self._repo.publish_context(job_id)
        trace.get_current_span().set_attribute("channel.id", ctx.channel_id)
        claims = self._verify(ctx, token)
        assert ctx.content_hash is not None
        key = idempotency_key(job_id, ctx.content_hash)

        existing = self._repo.find_upload(key)
        if existing is not None and existing.status in {"uploaded", "live"}:
            assert existing.youtube_video_id is not None
            state = JobState.LIVE if existing.status == "live" else JobState.PUBLISHED
            return PublishResult(job_id, existing.youtube_video_id, state, existing.id, True)

        if ctx.state not in {JobState.SCHEDULED, JobState.PUBLISHING}:
            raise PublishRefused(f"job is {ctx.state.value}, not scheduled or publishing")
        if not ctx.publishable:
            raise PublishRefused(
                "channel or cluster not active, or autonomy tier does not allow publishing"
            )
        if ctx.manifest is None or content_hash(ctx.manifest) != ctx.content_hash:
            raise PublishRefused("manifest does not match the approved content hash")
        metadata = _metadata(ctx.manifest)
        video_digest = ctx.manifest.get("assets", {}).get("video")
        if not isinstance(video_digest, str):
            raise PublishRefused("manifest has no video asset")
        media = read_verified(self._assets, ctx.storage_prefix, video_digest)
        video_info = (ctx.manifest.get("asset_info") or {}).get("video", {})
        try:
            check_asset("video", str(video_info.get("media_type", "video/mp4")), media)
        except AssetRejected as exc:
            raise PublishRefused(f"video asset rejected: {exc}") from None
        credentials = self._credentials(ctx)

        if existing is not None and existing.status == "pending":
            # Another attempt is uploading, or crashed mid-upload. Uploading again could create a
            # duplicate video, so wait; an interrupted attempt needs operator reconciliation.
            raise UploadInProgress(f"upload {existing.id} is pending")
        upload = self._repo.begin_upload(
            ctx,
            idempotency_key=key,
            approval_jti=claims.jti,
            synthetic_disclosure=metadata.contains_synthetic_media,
            quota_units=self._upload_units,
            actor=actor,
        )
        if upload.status != "pending" or upload.youtube_video_id is not None:
            raise UploadInProgress(f"upload {upload.id} was started by a concurrent attempt")

        # Re-check the kill switches (global, cluster, channel) immediately before the call to
        # YouTube; an operator may have engaged one while this publish was in flight.
        if not self._repo.publish_context(job_id).publishable:
            self._repo.fail_upload(upload.id, "KillSwitch", actor=actor)
            raise PublishRefused("a kill switch was engaged before the upload started")

        try:
            video_id = self._client.upload_resumable(
                credentials=credentials, media=media, metadata=metadata, idempotency_key=key
            )
        except Exception as exc:
            log.warning("upload failed for job %s: %s", job_id, type(exc).__name__)
            self._repo.fail_upload(upload.id, type(exc).__name__, actor=actor)
            raise UploadFailed(f"upload failed: {type(exc).__name__}") from exc
        self._repo.complete_upload(upload.id, video_id, actor=actor)

        state = JobState.PUBLISHED
        try:
            assert ctx.google_project_id is not None
            self._repo.charge_quota(
                ctx.google_project_id,
                self._status_units,
                "videos.list",
                job_id=job_id,
                actor=actor,
            )
            status = self._client.get_status(credentials=credentials, video_id=video_id)
            if status.is_live:
                self._repo.mark_live(upload.id, actor=actor)
                state = JobState.LIVE
        except QuotaExhausted:
            log.info("live check deferred for job %s: quota exhausted", job_id)
        return PublishResult(job_id, video_id, state, upload.id)
