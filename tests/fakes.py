"""In-memory fakes for tests. Production config and the gateway refuse fakes.

FakeBackend mirrors the database rules (state machine, approval gate and jti consumption,
publish gating, uploads, quota, budget) so workflow and gateway logic can be unit-tested
without Postgres. The DB triggers remain the real enforcement and are tested separately
(tests/schema_guard*_test.sql and tests/integration).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from ytcore.approvals.hashing import content_hash
from ytcore.approvals.token import ApprovalClaims, ApprovalError, ApprovalVerifier
from ytcore.gateway.assets import InMemoryAssetSource
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.secrets.provider import InMemorySecretsProvider
from ytcore.store.errors import (
    ApprovalGateError,
    ApprovalInvalid,
    ApprovalReplayError,
    BudgetExceeded,
    ChannelNotPublishable,
    IllegalTransition,
    JobNotFound,
    QuotaExhausted,
    UploadGuardError,
)
from ytcore.store.models import PublishContext, UploadRecord
from ytcore.workflow.states import APPROVAL_GATED_STATES, JobState, can_transition

__all__ = [
    "FakeBackend",
    "FakeChannel",
    "FakeYouTubeClient",
    "InMemoryAssetSource",
    "fake_secrets",
]

REFRESH_REF = "env://YT_REFRESH_TOKEN"
OAUTH_REF = "env://YT_OAUTH_CLIENT"


def fake_secrets() -> InMemorySecretsProvider:
    return InMemorySecretsProvider(
        {REFRESH_REF: "dummy-refresh", OAUTH_REF: '{"client_id": "x", "client_secret": "y"}'}
    )


@dataclass
class FakeChannel:
    id: str
    storage_prefix: str
    status: str = "active"
    cluster_status: str = "active"
    autonomy_tier: str = "gated"
    daily_budget_cents: int = 10_000
    google_project_id: str = "proj-fake"
    credentials_ref: str = REFRESH_REF
    oauth_client_ref: str = OAUTH_REF

    @property
    def publishable(self) -> bool:
        return (
            self.status == "active"
            and self.cluster_status == "active"
            and self.autonomy_tier == "gated"
        )


@dataclass
class FakeJob:
    id: str
    channel_id: str
    state: JobState = JobState.PLANNED
    content_hash: str | None = None
    manifest: dict[str, Any] | None = None
    render_cost_cents: int = 0
    approval_deadline: datetime | None = None


@dataclass
class FakeApproval:
    job_id: str
    content_hash: str
    expires_at: datetime


class FakeBackend:
    def __init__(self, verifier: ApprovalVerifier, *, daily_quota_units: int = 10_000) -> None:
        self._verifier = verifier
        self.daily_quota_units = daily_quota_units
        self.channels: dict[str, FakeChannel] = {}
        self.jobs: dict[str, FakeJob] = {}
        self.approvals: dict[str, FakeApproval] = {}
        self.uploads: dict[str, UploadRecord] = {}
        self.quota_used: dict[str, int] = {}
        self.costs_today: dict[str, int] = {}
        self.events: list[tuple[str, str, str]] = []  # (job_id, to_state, actor)
        self.qa_reports: list[tuple[str, bool, int]] = []

    # ---------------------------------------------------------------- setup helpers

    def add_channel(self, channel_id: str, **kw: Any) -> FakeChannel:
        ch = FakeChannel(channel_id, f"channels/{channel_id}/", **kw)
        self.channels[channel_id] = ch
        return ch

    def create_job(self, channel_id: str, job_id: str | None = None) -> str:
        if channel_id not in self.channels:
            self.add_channel(channel_id)
        jid = job_id or str(uuid.uuid4())
        self.jobs[jid] = FakeJob(jid, channel_id)
        self.events.append((jid, JobState.PLANNED.value, "planner"))
        return jid

    def _job(self, job_id: str) -> FakeJob:
        if job_id not in self.jobs:
            raise JobNotFound(job_id)
        return self.jobs[job_id]

    def _valid_approval(self, job: FakeJob) -> bool:
        now = datetime.now(UTC)
        return any(
            a.job_id == job.id and a.content_hash == job.content_hash and a.expires_at > now
            for a in self.approvals.values()
        )

    def _upload_for(self, job_id: str, status: str) -> bool:
        return any(u.job_id == job_id and u.status == status for u in self.uploads.values())

    # ---------------------------------------------------------------- Backend

    def get_state(self, job_id: str) -> JobState:
        return self._job(job_id).state

    def transition(self, job_id: str, to_state: JobState, *, actor: str) -> None:
        job = self._job(job_id)
        if not can_transition(job.state, to_state):
            raise IllegalTransition(f"illegal job transition {job.state} -> {to_state}")
        if to_state in APPROVAL_GATED_STATES and not self._valid_approval(job):
            raise ApprovalGateError("approval gate: no valid approval")
        if (
            to_state in {JobState.SCHEDULED, JobState.PUBLISHING}
            and not self.channels[job.channel_id].publishable
        ):
            raise ChannelNotPublishable(job.channel_id)
        if to_state == JobState.PUBLISHED and not self._upload_for(job_id, "uploaded"):
            raise UploadGuardError("published requires a completed upload")
        if to_state == JobState.LIVE and not self._upload_for(job_id, "live"):
            raise UploadGuardError("live requires an upload verified live")
        job.state = to_state
        self.events.append((job_id, to_state.value, actor))

    def submit_for_approval(
        self,
        job_id: str,
        manifest: dict[str, Any],
        *,
        actor: str,
        deadline: datetime | None = None,
    ) -> str:
        job = self._job(job_id)
        job.manifest, job.content_hash = manifest, content_hash(manifest)
        job.approval_deadline = deadline
        self.transition(job_id, JobState.AWAITING_APPROVAL, actor=actor)
        return job.content_hash

    def verify_approval(self, job_id: str, token: str, *, actor: str) -> ApprovalClaims:
        job = self._job(job_id)
        assert job.content_hash is not None
        try:
            claims = self._verifier.verify(
                token, job_id=job_id, channel_id=job.channel_id, content_hash=job.content_hash
            )
        except ApprovalError as exc:
            raise ApprovalInvalid(str(exc)) from None
        if claims.jti in self.approvals:
            raise ApprovalReplayError("approval token already used")
        if job.state != JobState.AWAITING_APPROVAL:
            raise ApprovalGateError("approvals can only be recorded for jobs awaiting approval")
        self.approvals[claims.jti] = FakeApproval(job_id, job.content_hash, claims.expires_at)
        self.transition(job_id, JobState.APPROVED, actor=f"approver:{claims.approver}")
        return claims

    def approval_on_record(self, job_id: str, token: str) -> ApprovalClaims | None:
        job = self._job(job_id)
        try:
            claims = self._verifier.verify(
                token, job_id=job_id, channel_id=job.channel_id, content_hash=job.content_hash or ""
            )
        except ApprovalError:
            return None
        a = self.approvals.get(claims.jti)
        approved = {
            JobState.APPROVED,
            JobState.SCHEDULED,
            JobState.PUBLISHING,
            JobState.PUBLISHED,
            JobState.LIVE,
        }
        ok = a is not None and a.job_id == job_id and a.content_hash == job.content_hash
        return claims if ok and job.state in approved else None

    def record_qa(
        self,
        job_id: str,
        *,
        passed: bool,
        risk_score: int,
        flags: list[dict[str, Any]],
        sources: list[dict[str, Any]],
        actor: str,
    ) -> None:
        self.qa_reports.append((job_id, passed, risk_score))

    def record_cost(self, job_id: str, kind: str, cost_cents: int, *, actor: str) -> None:
        job = self._job(job_id)
        ch = self.channels[job.channel_id]
        spent = self.costs_today.get(ch.id, 0)
        if spent + cost_cents > ch.daily_budget_cents:
            raise BudgetExceeded(f"daily budget exceeded for channel {ch.id}")
        self.costs_today[ch.id] = spent + cost_cents
        if kind == "render":
            job.render_cost_cents += cost_cents

    def storage_prefix(self, channel_id: str) -> str:
        return self.channels[channel_id].storage_prefix

    # ---------------------------------------------------------------- GatewayRepository

    def publish_context(self, job_id: str) -> PublishContext:
        job = self._job(job_id)
        ch = self.channels[job.channel_id]
        return PublishContext(
            job_id=job.id,
            channel_id=ch.id,
            state=job.state,
            content_hash=job.content_hash,
            manifest=job.manifest,
            storage_prefix=ch.storage_prefix,
            channel_status=ch.status,
            cluster_status=ch.cluster_status,
            autonomy_tier=ch.autonomy_tier,
            google_project_id=ch.google_project_id,
            credentials_ref=ch.credentials_ref,
            oauth_client_ref=ch.oauth_client_ref,
        )

    def approval_consumed(self, job_id: str, jti: str, chash: str) -> bool:
        a = self.approvals.get(jti)
        return a is not None and a.job_id == job_id and a.content_hash == chash

    def find_upload(self, idempotency_key: str) -> UploadRecord | None:
        return self.uploads.get(idempotency_key)

    def charge_quota(
        self, google_project_id: str, units: int, operation: str, *, job_id: str, actor: str
    ) -> None:
        used = self.quota_used.get(google_project_id, 0)
        if used + units > self.daily_quota_units:
            raise QuotaExhausted(f"YouTube quota exhausted for project {google_project_id}")
        self.quota_used[google_project_id] = used + units

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
        if idempotency_key in self.uploads:
            return replace(self.uploads[idempotency_key], status="in_progress")
        job = self._job(ctx.job_id)
        before = job.state
        if job.state == JobState.SCHEDULED:
            self.transition(job.id, JobState.PUBLISHING, actor=actor)
        try:
            assert ctx.google_project_id is not None
            self.charge_quota(
                ctx.google_project_id, quota_units, "videos.insert", job_id=job.id, actor=actor
            )
        except QuotaExhausted:
            job.state = before  # the real store rolls the whole transaction back
            raise
        if not self.approval_consumed(job.id, approval_jti, ctx.content_hash or ""):
            raise ApprovalGateError("upload requires a valid approval")
        record = UploadRecord(str(uuid.uuid4()), job.id, idempotency_key, "pending", None)
        self.uploads[idempotency_key] = record
        return record

    def _set_upload(self, upload_id: str, **changes: Any) -> UploadRecord:
        for key, rec in self.uploads.items():
            if rec.id == upload_id:
                self.uploads[key] = replace(rec, **changes)
                return self.uploads[key]
        raise UploadGuardError(upload_id)

    def complete_upload(self, upload_id: str, video_id: str, *, actor: str) -> None:
        rec = self._set_upload(upload_id, status="uploaded", youtube_video_id=video_id)
        self.transition(rec.job_id, JobState.PUBLISHED, actor=actor)

    def mark_live(self, upload_id: str, *, actor: str) -> None:
        rec = self._set_upload(upload_id, status="live")
        self.transition(rec.job_id, JobState.LIVE, actor=actor)

    def fail_upload(self, upload_id: str, reason: str, *, actor: str) -> None:
        rec = self._set_upload(upload_id, status="failed")
        self.transition(rec.job_id, JobState.FAILED, actor=actor)
