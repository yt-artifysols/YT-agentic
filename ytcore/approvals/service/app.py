"""FastAPI app for the approval hub. All state-changing routes are POST, need a valid session,
the right role, a CSRF token and a same-origin request, and are rate limited and audited."""

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ytcore.approvals.service.console import ConsoleRepository
from ytcore.approvals.service.hub import ApprovalHub, HubError
from ytcore.approvals.service.repo import (
    Credential,
    HubConflict,
    HubRepository,
    User,
    _is_uuid,
    sha256,
)
from ytcore.approvals.service.security import (
    CEREMONY_COOKIE,
    AuthContext,
    Ceremonies,
    Ceremony,
    CeremonyStore,
    RateLimiter,
    SessionManager,
    check_origin,
    require_role,
    stepup_subject,
)
from ytcore.approvals.service.settings import HubSettings
from ytcore.channels.rollout import GateNotMet, RolloutService
from ytcore.gateway.assets import AssetSource, read_verified
from ytcore.inputs.models import InputKind, OwnerInput
from ytcore.inputs.store import InputStore
from ytcore.knowledge.diff import line_diff
from ytcore.knowledge.models import KnowledgeDoc, KnowledgeKind
from ytcore.store.errors import (
    InputRejected,
    KnowledgeForbidden,
    KnowledgeStale,
    RolloutRefused,
    StoreError,
    TierRefused,
)

STEPUP_TTL = 120.0  # seconds a step-up challenge stays valid

# ----------------------------------------------------------------------------- schemas

ContentHash = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class Me(BaseModel):
    username: str
    display_name: str
    role: Literal["approver", "operator", "admin"]
    csrf_token: str | None = None


class InviteOptionsIn(BaseModel):
    invite_token: str = Field(min_length=20, max_length=200)


class LoginOptionsIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)


class CredentialIn(BaseModel):
    credential: dict[str, Any]


class QueueItemOut(BaseModel):
    job_id: str
    channel_id: str
    cluster_id: str | None
    title: str
    origin: str
    content_hash: str
    manifest_version: int
    risk_score: int | None
    qa_passed: bool | None
    flag_count: int
    approval_deadline: datetime | None
    fast_lane: bool
    lane: Literal["standard", "fast", "deep"]
    lane_reason: str


class AssetOut(BaseModel):
    name: str
    digest: str
    url: str


class QAOut(BaseModel):
    passed: bool
    risk_score: int
    flags: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    for_current_content: bool


class PreviousVersionOut(BaseModel):
    version: int
    metadata: dict[str, Any]


class PreviewOut(BaseModel):
    job_id: str
    channel_id: str
    state: str
    content_hash: str
    manifest_version: int
    title: str
    description: str
    tags: list[str]
    script: str
    metadata: dict[str, Any]
    assets: list[AssetOut]
    video_url: str | None
    thumbnail_url: str | None
    qa: QAOut | None
    fast_lane: bool
    lane: Literal["standard", "fast", "deep"]
    lane_reason: str
    previous: PreviousVersionOut | None


class TierRowOut(BaseModel):
    channel_id: str
    name: str
    cluster_id: str | None
    tier: int
    tier_since: datetime
    last_change_kind: str | None
    last_change_reason: str | None
    last_change_at: datetime | None
    recommendation_id: int | None
    recommendation_evidence: dict[str, Any] | None


class TierChangeOut(BaseModel):
    id: int
    channel_id: str
    from_tier: int
    to_tier: int
    kind: Literal["promotion", "demotion"]
    reason: str
    actor: str
    evidence: dict[str, Any]
    recommendation_id: int | None
    created_at: datetime


class StepUpProof(BaseModel):
    """A fresh passkey assertion for exactly one action (see /auth/stepup/options)."""

    ticket: Annotated[str, Field(min_length=8, max_length=128)]
    credential: dict[str, Any]


class StepUpIn(BaseModel):
    purpose: Literal[
        "approve", "batch", "release", "tier_confirm", "tier_dismiss", "rollout_enable",
        "rollout_signoff", "invite", "knowledge",
    ]  # fmt: skip
    parts: Annotated[list[Annotated[str, Field(max_length=200)]], Field(max_length=100)]


class EnableIn(BaseModel):
    stepup: StepUpProof | None = None


class StepUpOut(BaseModel):
    ticket: str
    options: dict[str, Any]


class TierDecisionIn(BaseModel):
    note: Annotated[str, Field(min_length=10, max_length=1000)]
    stepup: StepUpProof | None = None


class StrikeIn(BaseModel):
    kind: Literal["strike", "warning", "takedown"]
    note: Annotated[str, Field(min_length=10, max_length=1000)]
    video_ref: Annotated[str | None, Field(max_length=200)] = None


class StrikeOut(BaseModel):
    id: int


class ApproveIn(BaseModel):
    content_hash: ContentHash
    stepup: StepUpProof | None = None


class ApproveOut(BaseModel):
    job_id: str
    content_hash: str
    jti: str
    approver: str


class EditIn(BaseModel):
    content_hash: ContentHash
    title: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=5000)
    tags: list[Annotated[str, Field(max_length=30)]] | None = Field(default=None, max_length=30)
    script: str | None = Field(default=None, max_length=20000)


class EditOut(BaseModel):
    job_id: str
    content_hash: str
    manifest_version: int


class RejectIn(BaseModel):
    content_hash: ContentHash
    reason: str = Field(min_length=10, max_length=2000)
    rework_stage: Literal["research", "script", "render", "discard"]


class BatchItemIn(BaseModel):
    job_id: str
    content_hash: ContentHash


class BatchIn(BaseModel):
    items: list[BatchItemIn] = Field(min_length=1, max_length=100)
    stepup: StepUpProof | None = None


class BatchItemOut(BaseModel):
    job_id: str
    ok: bool
    jti: str | None = None
    error: str | None = None


class BatchOut(BaseModel):
    results: list[BatchItemOut]


class AlertOut(BaseModel):
    id: int
    created_at: datetime
    kind: str
    message: str
    channel_id: str | None
    cluster_id: str | None
    job_id: str | None


class ClusterOut(BaseModel):
    id: str
    display_name: str
    niche: str
    status: str
    live_enabled: bool
    live_since: datetime | None
    requires_cluster: str | None
    gate_days: int
    max_concurrent_jobs: int
    channels: int
    awaiting_approval: int
    oldest_waiting_since: datetime | None
    health: dict[str, Any] | None


class ScorecardOut(BaseModel):
    channel_id: str
    cluster_id: str | None
    score_date: date
    jobs_planned: int
    jobs_approved: int
    jobs_rejected: int
    approval_rate: float | None
    policy_flags: int
    videos_published: int
    cost_cents: int
    cost_per_video: float | None
    avg_ctr: float | None
    avg_retention: float | None
    views: int
    revenue_cents: int | None


class GateOut(BaseModel):
    cluster_id: str
    live: bool
    live_days: float
    required_days: int
    breaker_trips: int
    stuck_or_plan_alerts: int
    failed_uploads: int
    pending_uploads: int
    decisions: int
    rejection_rate: float | None
    scorecard_days: int
    problems: list[str]
    ok: bool


class SignoffIn(BaseModel):
    note: str = Field(min_length=20, max_length=2000)
    stepup: StepUpProof | None = None


class SignoffOut(BaseModel):
    id: int


class KillSwitchIn(BaseModel):
    paused: bool
    reason: str = Field(default="", max_length=500)
    stepup: StepUpProof | None = None  # required to RELEASE a switch (engaging never needs it)


class InviteIn(BaseModel):
    username: str = Field(pattern=r"^[a-z0-9][a-z0-9._@-]{2,63}$")
    display_name: str = Field(min_length=1, max_length=100)
    role: Literal["approver", "operator", "admin"]
    stepup: StepUpProof | None = None


class InviteOut(BaseModel):
    username: str
    invite_token: str
    expires_at: datetime


class Ok(BaseModel):
    ok: bool = True


Sha256 = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
KnowledgeKindName = Literal[
    "brand_voice", "offers", "playbook", "hook_library", "banned_claims", "style_guide", "policy"
]


class KnowledgeDocOut(BaseModel):
    id: str
    channel_id: str
    kind: str
    version: int
    base_version: int | None
    status: str
    content_sha256: str
    proposed_by: str
    proposed_at: datetime | None
    approved_by: str | None
    approved_at: datetime | None


class DiffLineOut(BaseModel):
    op: Literal["equal", "add", "remove"]
    text: str


class KnowledgeDraftOut(KnowledgeDocOut):
    content: str
    base_content: str | None
    current_version: int | None
    stale: bool
    diff: list[DiffLineOut]


class KnowledgeProposeIn(BaseModel):
    kind: KnowledgeKindName
    content: str = Field(min_length=1, max_length=100_000)
    base_version: int | None = Field(default=None, gt=0)


class KnowledgeDecisionIn(BaseModel):
    content_sha256: Sha256
    stepup: StepUpProof | None = None


class KnowledgeRejectIn(KnowledgeDecisionIn):
    reason: str = Field(min_length=3, max_length=2000)


class OwnerInputIn(BaseModel):
    kind: Literal["idea", "footage", "note", "goal"]
    body: str | None = Field(default=None, min_length=1, max_length=20_000)
    uri: str | None = Field(default=None, max_length=2000)
    sha256: Sha256 | None = None


class OwnerInputOut(BaseModel):
    id: str
    channel_id: str
    kind: str
    uri: str | None
    body: str | None
    sha256: str
    status: str
    submitted_by: str
    used_by_job: str | None
    created_at: datetime | None


def _doc_out(d: KnowledgeDoc) -> dict[str, Any]:
    return {
        "id": d.id,
        "channel_id": d.channel_id,
        "kind": str(d.kind),
        "version": d.version,
        "base_version": d.base_version,
        "status": d.status,
        "content_sha256": d.content_sha256,
        "proposed_by": d.proposed_by,
        "proposed_at": d.proposed_at,
        "approved_by": d.approved_by,
        "approved_at": d.approved_at,
    }


def _input_out(i: OwnerInput) -> OwnerInputOut:
    return OwnerInputOut(
        id=i.id,
        channel_id=i.channel_id,
        kind=str(i.kind),
        uri=i.uri,
        body=i.body,
        sha256=i.sha256,
        status=i.status,
        submitted_by=i.submitted_by,
        used_by_job=i.used_by_job,
        created_at=i.created_at,
    )


# ----------------------------------------------------------------------------- app


@dataclass
class HubDeps:
    settings: HubSettings
    repo: HubRepository
    hub: ApprovalHub
    assets: AssetSource
    sessions: SessionManager
    ceremonies: Ceremonies
    limiter: RateLimiter
    inputs: InputStore | None = None
    rollout: RolloutService | None = None
    console: ConsoleRepository | None = None
    knowledge: Any = None  # channel-bound knowledge repositories (proposals only)


def create_app(deps: HubDeps) -> FastAPI:
    s = deps.settings
    app = FastAPI(
        title="yt-agentic approval hub",
        version="1.0.0",
        description="The only place approval tokens are issued. Passkey sign-in; "
        "all state changes are POST with session, role, CSRF token and same-origin checks.",
        docs_url=None if s.env == "production" else "/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(HubError)
    async def hub_error(_: Request, exc: HubError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    def address_key(request: Request) -> str:
        if s.trust_forwarded_for:
            # The proxy in front appends the real client address LAST; earlier entries are
            # client-supplied and spoofable.
            forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
            if forwarded:
                return "f:" + forwarded
        return "a:" + (request.client.host if request.client else "unknown")

    def client_key(request: Request, bucket: str) -> str:
        # The unauthenticated endpoints are limited per client ADDRESS only: a cookie value is
        # attacker-chosen there, so keying on it would let anyone rotate it past the limit.
        # Other buckets run for signed-in callers and are limited per session.
        session = request.cookies.get("hub_session")
        if session and bucket != "auth":
            return "s:" + sha256(session).hex()[:32]
        return address_key(request)

    def limit(bucket: str, per_minute: int) -> Any:
        def dep(request: Request) -> None:
            deps.limiter.check(client_key(request, bucket), bucket, per_minute)

        return Depends(dep)

    async def session(request: Request) -> AuthContext:
        check_origin(request, s)
        return await deps.sessions.authenticate(request)

    Session = Annotated[AuthContext, Depends(session)]
    auth_limit = limit("auth", s.rate_auth_per_minute)
    write_limit = limit("write", s.rate_write_per_minute)
    read_limit = limit("read", s.rate_read_per_minute)

    async def require_stepup(
        ctx: AuthContext, proof: StepUpProof | None, purpose: str, parts: list[str]
    ) -> None:
        """A fresh passkey assertion for exactly this action, used once, within two minutes."""
        if not s.require_stepup:
            return
        refused = HTTPException(428, "confirm with your passkey to continue")
        if proof is None:
            raise refused
        try:
            ceremony = deps.ceremonies.pop(proof.ticket, f"stepup:{purpose}")
        except HTTPException:
            raise refused from None
        if ceremony.user_id != ctx.user.id or ceremony.subject != stepup_subject(purpose, parts):
            raise refused
        try:
            raw_id = base64url_to_bytes(str(proof.credential.get("rawId", "")))
            cred = await asyncio.to_thread(deps.repo.credential, raw_id)
            if cred is None or cred.user_id != ctx.user.id:
                raise refused
            verified = verify_authentication_response(
                credential=proof.credential,
                expected_challenge=ceremony.challenge,
                expected_rp_id=s.rp_id,
                expected_origin=s.origin,
                credential_public_key=cred.public_key,
                credential_current_sign_count=cred.sign_count,
                require_user_verification=True,
            )
        except HTTPException:
            raise
        except Exception:
            await asyncio.to_thread(
                deps.repo.audit, f"hub:{ctx.user.username}", "hub.stepup.failed",
                f"user:{ctx.user.username}", {"purpose": purpose},
            )  # fmt: skip
            raise refused from None
        await asyncio.to_thread(
            deps.repo.bump_sign_count, cred.credential_id, verified.new_sign_count
        )

    def me(user: User, csrf: str | None = None) -> Me:
        return Me(
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            csrf_token=csrf,
        )

    # ------------------------------------------------------------------ auth (passkeys)

    auth = APIRouter(prefix="/api/auth", tags=["auth"])

    @auth.post("/register/options", dependencies=[auth_limit])
    async def register_options(body: InviteOptionsIn, request: Request) -> Response:
        check_origin(request, s)
        user = await asyncio.to_thread(deps.repo.invite_user, body.invite_token)
        if user is None or user.disabled:
            raise HTTPException(400, "invite is invalid, used or expired")
        existing = await asyncio.to_thread(deps.repo.credentials_for, user.id)
        options = generate_registration_options(
            rp_id=s.rp_id,
            rp_name=s.rp_name,
            user_name=user.username,
            user_id=user.id.encode(),
            user_display_name=user.display_name,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=c.credential_id) for c in existing
            ],
        )
        key = deps.ceremonies.put(
            Ceremony(
                options.challenge,
                "register",
                time.monotonic() + CeremonyStore.TTL,
                user_id=user.id,
                invite_token=body.invite_token,
            )
        )
        response = Response(options_to_json(options), media_type="application/json")
        deps.sessions.set_ceremony_cookie(response, key)
        return response

    @auth.post("/register/verify", response_model=Me, dependencies=[auth_limit])
    async def register_verify(body: CredentialIn, request: Request, response: Response) -> Me:
        check_origin(request, s)
        ceremony = deps.ceremonies.pop(request.cookies.get(CEREMONY_COOKIE), "register")
        assert ceremony.user_id is not None and ceremony.invite_token is not None
        try:
            verified = verify_registration_response(
                credential=body.credential,
                expected_challenge=ceremony.challenge,
                expected_rp_id=s.rp_id,
                expected_origin=s.origin,
                require_user_verification=True,
            )
        except InvalidRegistrationResponse as exc:
            raise HTTPException(400, "passkey registration failed") from exc
        transports = body.credential.get("response", {}).get("transports", []) or []
        stored = await asyncio.to_thread(
            deps.repo.register_credential,
            ceremony.invite_token,
            Credential(
                verified.credential_id,
                ceremony.user_id,
                verified.credential_public_key,
                verified.sign_count,
                [str(t) for t in transports][:8],
            ),
            actor="hub:auth",
        )
        if not stored:
            raise HTTPException(400, "invite is invalid, used or expired")
        user = await asyncio.to_thread(deps.repo.user_by_id, ceremony.user_id)
        assert user is not None
        csrf = await deps.sessions.start(response, user)
        await asyncio.to_thread(
            deps.repo.audit, f"hub:{user.username}", "hub.login", f"user:{user.username}",
            {"method": "passkey-registration"},
        )  # fmt: skip
        return me(user, csrf)

    @auth.post("/login/options", dependencies=[auth_limit])
    async def login_options(body: LoginOptionsIn, request: Request) -> Response:
        check_origin(request, s)
        user = await asyncio.to_thread(deps.repo.user_by_name, body.username)
        creds = (
            await asyncio.to_thread(deps.repo.credentials_for, user.id)
            if user and not user.disabled
            else []
        )
        # Same response shape for unknown users (no account enumeration).
        options = generate_authentication_options(
            rp_id=s.rp_id,
            allow_credentials=[PublicKeyCredentialDescriptor(id=c.credential_id) for c in creds],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        key = deps.ceremonies.put(
            Ceremony(
                options.challenge,
                "login",
                time.monotonic() + CeremonyStore.TTL,
                user_id=user.id if user else None,
            )
        )
        response = Response(options_to_json(options), media_type="application/json")
        deps.sessions.set_ceremony_cookie(response, key)
        return response

    @auth.post("/login/verify", response_model=Me, dependencies=[auth_limit])
    async def login_verify(body: CredentialIn, request: Request, response: Response) -> Me:
        check_origin(request, s)
        ceremony = deps.ceremonies.pop(request.cookies.get(CEREMONY_COOKIE), "login")
        failed = HTTPException(401, "passkey sign-in failed")
        try:
            raw_id = base64url_to_bytes(str(body.credential.get("rawId", "")))
        except Exception:
            raise failed from None
        cred = await asyncio.to_thread(deps.repo.credential, raw_id)
        if cred is None or cred.user_id != ceremony.user_id:
            await asyncio.to_thread(
                deps.repo.audit, "hub:auth", "hub.login.failed", "user:?",
                {"credential": bytes_to_base64url(raw_id)[:16]},
            )  # fmt: skip
            raise failed
        try:
            verified = verify_authentication_response(
                credential=body.credential,
                expected_challenge=ceremony.challenge,
                expected_rp_id=s.rp_id,
                expected_origin=s.origin,
                credential_public_key=cred.public_key,
                credential_current_sign_count=cred.sign_count,
                require_user_verification=True,
            )
        except InvalidAuthenticationResponse:
            await asyncio.to_thread(
                deps.repo.audit, "hub:auth", "hub.login.failed", f"userid:{cred.user_id}", {}
            )
            raise failed from None
        await asyncio.to_thread(
            deps.repo.bump_sign_count, cred.credential_id, verified.new_sign_count
        )
        user = await asyncio.to_thread(deps.repo.user_by_id, cred.user_id)
        if user is None or user.disabled:
            raise failed
        csrf = await deps.sessions.start(response, user)
        await asyncio.to_thread(
            deps.repo.audit, f"hub:{user.username}", "hub.login", f"user:{user.username}",
            {"method": "passkey"},
        )  # fmt: skip
        return me(user, csrf)

    @auth.post("/logout", response_model=Ok, dependencies=[write_limit])
    async def logout(ctx: Session, request: Request, response: Response) -> Ok:
        await deps.sessions.end(request, response)
        await asyncio.to_thread(
            deps.repo.audit, f"hub:{ctx.user.username}", "hub.logout",
            f"user:{ctx.user.username}", {},
        )  # fmt: skip
        return Ok()

    @auth.get("/me", response_model=Me, dependencies=[read_limit])
    async def whoami(ctx: Session) -> Me:
        return me(ctx.user)

    @auth.post("/csrf", response_model=Me, dependencies=[write_limit])
    async def rotate_csrf(request: Request) -> Me:
        """Issue a fresh CSRF token for the current session (e.g. after a page reload).

        Needs the session cookie (SameSite=Strict) and a same-origin request; the old token
        stops working.
        """
        check_origin(request, s)
        token = request.cookies.get("hub_session")
        user, csrf = await deps.sessions.rotate_csrf(token)
        return me(user, csrf)

    @auth.post("/stepup/options", response_model=StepUpOut, dependencies=[write_limit])
    async def stepup_options(body: StepUpIn, ctx: Session) -> StepUpOut:
        """A challenge for ONE action; the passkey assertion that answers it is the proof."""
        creds = await asyncio.to_thread(deps.repo.credentials_for, ctx.user.id)
        options = generate_authentication_options(
            rp_id=s.rp_id,
            allow_credentials=[PublicKeyCredentialDescriptor(id=c.credential_id) for c in creds],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        ticket = deps.ceremonies.put(
            Ceremony(
                options.challenge,
                f"stepup:{body.purpose}",
                time.monotonic() + STEPUP_TTL,
                user_id=ctx.user.id,
                subject=stepup_subject(body.purpose, body.parts),
            )
        )
        return StepUpOut(ticket=ticket, options=json.loads(options_to_json(options)))

    app.include_router(auth)

    # ------------------------------------------------------------------ approvals

    api = APIRouter(prefix="/api", tags=["approvals"])

    @api.get("/jobs", response_model=list[QueueItemOut], dependencies=[read_limit])
    async def queue(
        ctx: Session,
        cluster: str | None = None,
        channel: str | None = None,
        min_risk: Annotated[int | None, Query(ge=0, le=100)] = None,
        max_risk: Annotated[int | None, Query(ge=0, le=100)] = None,
        fast_lane: bool = False,
        sort: Literal["risk", "deadline", "channel", "cluster"] = "risk",
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[QueueItemOut]:
        items = await asyncio.to_thread(
            deps.repo.queue,
            cluster=cluster,
            channel=channel,
            min_risk=min_risk,
            max_risk=max_risk,
            fast_lane_only=fast_lane,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        return [QueueItemOut(**i.__dict__) for i in items]

    def rollout_service() -> RolloutService:
        if deps.rollout is None:
            raise HTTPException(503, "rollout is not configured")
        return deps.rollout

    @api.get(
        "/clusters", tags=["scale"], response_model=list[ClusterOut], dependencies=[read_limit]
    )
    async def clusters(ctx: Session) -> list[ClusterOut]:
        rows = await asyncio.to_thread(rollout_service().clusters)
        return [ClusterOut(**r) for r in rows]

    @api.get(
        "/scorecards", tags=["scale"], response_model=list[ScorecardOut], dependencies=[read_limit]
    )
    async def scorecards(
        ctx: Session,
        channel: str | None = None,
        cluster: str | None = None,
        days: Annotated[int, Query(ge=1, le=90)] = 14,
    ) -> list[ScorecardOut]:
        rows = await asyncio.to_thread(
            rollout_service().scorecards, channel=channel, cluster=cluster, days=days
        )
        return [ScorecardOut(**r) for r in rows]

    @api.get(
        "/rollout/{cluster}/gate", tags=["scale"], response_model=GateOut, dependencies=[read_limit]
    )
    async def rollout_gate(cluster: str, ctx: Session) -> GateOut:
        require_role(ctx, "operator", "admin")
        try:
            report = await asyncio.to_thread(rollout_service().gate, cluster)
        except ValueError:
            raise HTTPException(404, "cluster not found") from None
        return GateOut(**report.to_json())

    @api.post(
        "/rollout/{cluster}/signoff", tags=["scale"], response_model=SignoffOut,
        dependencies=[write_limit],
    )  # fmt: skip
    async def rollout_signoff(cluster: str, body: SignoffIn, ctx: Session) -> SignoffOut:
        admin = require_role(ctx, "admin")
        await require_stepup(ctx, body.stepup, "rollout_signoff", [cluster])
        try:
            sid = await asyncio.to_thread(
                rollout_service().sign_off, cluster, admin.username, body.note
            )
        except GateNotMet as exc:
            raise HTTPException(409, f"gate not met: {exc}") from None
        except ValueError:
            raise HTTPException(404, "cluster not found") from None
        return SignoffOut(id=sid)

    @api.post(
        "/rollout/{cluster}/enable", tags=["scale"], response_model=Ok, dependencies=[write_limit]
    )
    async def rollout_enable(cluster: str, ctx: Session, body: EnableIn | None = None) -> Ok:
        admin = require_role(ctx, "admin")
        await require_stepup(ctx, body.stepup if body else None, "rollout_enable", [cluster])
        try:
            await asyncio.to_thread(rollout_service().enable, cluster, admin.username)
        except RolloutRefused as exc:
            raise HTTPException(409, str(exc)) from None
        return Ok()

    def console_repo() -> ConsoleRepository:
        if deps.console is None:
            raise HTTPException(503, "the console is not configured")
        return deps.console

    @api.get(
        "/overview", tags=["console"], response_model=dict[str, Any], dependencies=[read_limit]
    )
    async def overview(ctx: Session) -> dict[str, Any]:
        """Today at a glance: approvals waiting, production, alerts, switches, cost, activity."""
        return await asyncio.to_thread(console_repo().overview)

    @api.get(
        "/pipeline", tags=["console"], response_model=dict[str, Any], dependencies=[read_limit]
    )
    async def pipeline(
        ctx: Session,
        state: str | None = None,
        channel: str | None = None,
        cluster: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                console_repo().pipeline,
                state=state, channel=channel, cluster=cluster, limit=limit, offset=offset,
            )  # fmt: skip
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @api.get(
        "/jobs/{job_id}/timeline", tags=["console"], response_model=list[dict[str, Any]],
        dependencies=[read_limit],
    )  # fmt: skip
    async def job_timeline(job_id: str, ctx: Session) -> list[dict[str, Any]]:
        rows = (
            await asyncio.to_thread(console_repo().timeline, job_id) if _is_uuid(job_id) else None
        )
        if rows is None:
            raise HTTPException(404, "job not found")
        return rows

    @api.get(
        "/channels",
        tags=["console"],
        response_model=list[dict[str, Any]],
        dependencies=[read_limit],
    )
    async def channel_list(ctx: Session) -> list[dict[str, Any]]:
        return await asyncio.to_thread(console_repo().channels)

    @api.get(
        "/channels/{channel_id}", tags=["console"], response_model=dict[str, Any],
        dependencies=[read_limit],
    )  # fmt: skip
    async def channel_detail(channel_id: str, ctx: Session) -> dict[str, Any]:
        row = await asyncio.to_thread(console_repo().channel, channel_id)
        if row is None:
            raise HTTPException(404, "channel not found")
        return row

    @api.get(
        "/admin/users", tags=["admin"], response_model=list[dict[str, Any]],
        dependencies=[read_limit],
    )  # fmt: skip
    async def admin_users(ctx: Session) -> list[dict[str, Any]]:
        require_role(ctx, "admin")
        return await asyncio.to_thread(console_repo().users)

    @api.get("/tiers", tags=["tiers"], response_model=list[TierRowOut], dependencies=[read_limit])
    async def tiers(ctx: Session) -> list[TierRowOut]:
        rows = await asyncio.to_thread(deps.repo.tiers.overview)
        return [TierRowOut(**r) for r in rows]

    @api.get(
        "/tiers/changes", tags=["tiers"], response_model=list[TierChangeOut],
        dependencies=[read_limit],
    )  # fmt: skip
    async def tier_changes(
        ctx: Session,
        channel: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[TierChangeOut]:
        rows = await asyncio.to_thread(deps.repo.tiers.changes, limit, channel)
        return [TierChangeOut(**r) for r in rows]

    @api.get(
        "/tiers/report", tags=["tiers"], response_model=dict[str, Any], dependencies=[read_limit]
    )
    async def tier_report(ctx: Session, channel: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(deps.repo.tiers.disagreement, channel)

    @api.post(
        "/tiers/recommendations/{rid}/confirm", tags=["tiers"], response_model=Ok,
        dependencies=[write_limit],
    )  # fmt: skip
    async def tier_confirm(rid: int, body: TierDecisionIn, ctx: Session) -> Ok:
        admin = require_role(ctx, "admin")
        await require_stepup(ctx, body.stepup, "tier_confirm", [str(rid)])
        try:
            await asyncio.to_thread(deps.repo.tiers.confirm, rid, admin.username, body.note)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except TierRefused as exc:
            raise HTTPException(409, str(exc)) from None
        return Ok()

    @api.post(
        "/tiers/recommendations/{rid}/dismiss", tags=["tiers"], response_model=Ok,
        dependencies=[write_limit],
    )  # fmt: skip
    async def tier_dismiss(rid: int, body: TierDecisionIn, ctx: Session) -> Ok:
        admin = require_role(ctx, "admin")
        await require_stepup(ctx, body.stepup, "tier_dismiss", [str(rid)])
        try:
            await asyncio.to_thread(deps.repo.tiers.dismiss, rid, admin.username, body.note)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except TierRefused as exc:
            raise HTTPException(409, str(exc)) from None
        return Ok()

    @api.post(
        "/channels/{channel}/strikes", tags=["tiers"], response_model=StrikeOut,
        dependencies=[write_limit],
    )  # fmt: skip
    async def record_strike(channel: str, body: StrikeIn, ctx: Session) -> StrikeOut:
        """Record a YouTube strike, warning or takedown. The channel returns to approve-all."""
        admin = require_role(ctx, "admin")
        try:
            sid = await asyncio.to_thread(
                deps.repo.tiers.record_strike, channel, body.kind, body.note,
                video_ref=body.video_ref, actor=f"hub:{admin.username}",
            )  # fmt: skip
        except LookupError:
            raise HTTPException(404, "channel not found") from None
        return StrikeOut(id=sid)

    @api.get("/jobs/{job_id}", response_model=PreviewOut, dependencies=[read_limit])
    async def preview(job_id: str, ctx: Session) -> PreviewOut:
        head = await asyncio.to_thread(deps.repo.head, job_id)
        if head is None or head.manifest is None or head.content_hash is None:
            raise HTTPException(404, "job not found")
        qa = await asyncio.to_thread(deps.repo.qa, job_id)
        lane = await asyncio.to_thread(deps.repo.lane_for, job_id)
        previous = (
            await asyncio.to_thread(deps.repo.manifest_version, job_id, head.manifest_version - 1)
            if head.manifest_version > 1
            else None
        )
        meta = dict(head.manifest.get("metadata", {}))
        assets = [
            AssetOut(
                name=name,
                digest=digest,
                url=f"/api/jobs/{job_id}/assets/{name}?v={head.content_hash[7:19]}",
            )
            for name, digest in sorted(head.manifest.get("assets", {}).items())
        ]
        by_name = {a.name: a.url for a in assets}
        await asyncio.to_thread(
            deps.repo.audit, f"hub:{ctx.user.username}", "hub.preview", f"job:{job_id}",
            {"content_hash": head.content_hash},
        )  # fmt: skip
        return PreviewOut(
            job_id=head.job_id,
            channel_id=head.channel_id,
            state=head.state,
            content_hash=head.content_hash,
            manifest_version=head.manifest_version,
            title=str(meta.get("title", "")),
            description=str(meta.get("description", "")),
            tags=[str(t) for t in meta.get("tags", [])],
            script=str(meta.get("script", "")),
            metadata=meta,
            assets=assets,
            video_url=by_name.get("video"),
            thumbnail_url=by_name.get("thumbnail"),
            qa=QAOut(
                passed=qa.passed,
                risk_score=qa.risk_score,
                flags=qa.flags,
                sources=qa.sources,
                for_current_content=qa.content_hash in (None, head.content_hash),
            )
            if qa
            else None,
            fast_lane=lane is not None and lane.lane == "fast",
            lane=lane.lane if lane else "standard",
            lane_reason=lane.reason if lane else "",
            previous=PreviousVersionOut(
                version=head.manifest_version - 1, metadata=dict(previous.get("metadata", {}))
            )
            if previous
            else None,
        )

    @api.get("/jobs/{job_id}/assets/{name}", dependencies=[read_limit])
    async def asset(job_id: str, name: str, ctx: Session) -> Response:
        head = await asyncio.to_thread(deps.repo.head, job_id)
        digest = (head.manifest or {}).get("assets", {}).get(name) if head else None
        if head is None or not isinstance(digest, str):
            raise HTTPException(404, "asset not found")
        try:
            data = await asyncio.to_thread(read_verified, deps.assets, head.storage_prefix, digest)
        except Exception as exc:
            raise HTTPException(404, "asset not available") from exc
        media = "video/mp4" if name == "video" else "image/png" if name == "thumbnail" else (
            "application/octet-stream"
        )  # fmt: skip
        return Response(
            data,
            media_type=media,
            headers={"Content-Security-Policy": "sandbox; default-src 'none'"},
        )

    @api.post("/jobs/{job_id}/approve", response_model=ApproveOut, dependencies=[write_limit])
    async def approve(job_id: str, body: ApproveIn, ctx: Session) -> ApproveOut:
        user = require_role(ctx, "approver")
        await require_stepup(ctx, body.stepup, "approve", [job_id, body.content_hash])
        res = await deps.hub.approve(job_id, body.content_hash, user)
        return ApproveOut(**res.__dict__)

    @api.post("/jobs/{job_id}/edit", response_model=EditOut, dependencies=[write_limit])
    async def edit(job_id: str, body: EditIn, ctx: Session) -> EditOut:
        user = require_role(ctx, "approver")
        changes = body.model_dump(exclude_none=True, exclude={"content_hash"})
        res = await deps.hub.edit(job_id, body.content_hash, changes, user)
        return EditOut(**res.__dict__)

    @api.post("/jobs/{job_id}/reject", response_model=Ok, dependencies=[write_limit])
    async def reject(job_id: str, body: RejectIn, ctx: Session) -> Ok:
        user = require_role(ctx, "approver")
        await deps.hub.reject(job_id, body.content_hash, body.reason, body.rework_stage, user)
        return Ok()

    @api.post("/jobs/batch-approve", response_model=BatchOut, dependencies=[write_limit])
    async def batch_approve(body: BatchIn, ctx: Session) -> BatchOut:
        user = require_role(ctx, "approver")
        await require_stepup(
            ctx, body.stepup, "batch", [f"{i.job_id}:{i.content_hash}" for i in body.items]
        )
        results = await deps.hub.batch_approve(
            [(i.job_id, i.content_hash) for i in body.items], user
        )
        return BatchOut(results=[BatchItemOut(**r.__dict__) for r in results])

    # ------------------------------------------------------------------ kill switches

    @api.get("/controls", tags=["controls"], dependencies=[read_limit])
    async def controls(ctx: Session) -> dict[str, Any]:
        return await asyncio.to_thread(deps.repo.controls)

    @api.get("/alerts", tags=["controls"], response_model=list[AlertOut], dependencies=[read_limit])
    async def alerts(
        ctx: Session, limit: Annotated[int, Query(ge=1, le=200)] = 100
    ) -> list[AlertOut]:
        """Budget caps, circuit-breaker trips and other operator alerts."""
        require_role(ctx, "operator", "admin")
        return [AlertOut(**a) for a in await asyncio.to_thread(deps.repo.alerts, limit)]

    @api.post("/controls/global", tags=["controls"], response_model=Ok, dependencies=[write_limit])
    async def kill_global(body: KillSwitchIn, ctx: Session) -> Ok:
        user = require_role(ctx, "operator", "admin")
        if not body.paused:  # releasing is the dangerous direction; engaging never needs proof
            await require_stepup(ctx, body.stepup, "release", ["global", "*"])
        await deps.hub.set_kill_switch("global", None, body.paused, body.reason, user)
        return Ok()

    @api.post(
        "/controls/{scope}/{ident}", tags=["controls"], response_model=Ok,
        dependencies=[write_limit],
    )  # fmt: skip
    async def kill_scoped(
        scope: Literal["clusters", "channels"], ident: str, body: KillSwitchIn, ctx: Session
    ) -> Ok:
        user = require_role(ctx, "operator", "admin")
        if not body.paused:
            await require_stepup(ctx, body.stepup, "release", [scope[:-1], ident])
        await deps.hub.set_kill_switch(scope[:-1], ident, body.paused, body.reason, user)
        return Ok()

    # ------------------------------------------------------------------ admin

    @api.post("/admin/invites", tags=["admin"], response_model=InviteOut,
              dependencies=[write_limit])  # fmt: skip
    async def invite(body: InviteIn, ctx: Session) -> InviteOut:
        admin = require_role(ctx, "admin")
        # Minting a user is minting an approver: it needs the same fresh passkey proof.
        await require_stepup(ctx, body.stepup, "invite", [body.username, body.role])
        user = await asyncio.to_thread(
            deps.repo.create_user, body.username, body.display_name, body.role,
            actor=f"hub:{admin.username}",
        )  # fmt: skip
        token = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(hours=s.invite_ttl_hours)
        await asyncio.to_thread(
            deps.repo.create_invite, user.id, token, expires, actor=f"hub:{admin.username}"
        )
        return InviteOut(username=user.username, invite_token=token, expires_at=expires)

    app.include_router(api)

    # ------------------------------------------------------------------ knowledge base

    kb = APIRouter(prefix="/api", tags=["knowledge"])

    def store_error(exc: StoreError) -> HTTPException:
        if isinstance(exc, KnowledgeForbidden):
            return HTTPException(403, str(exc))
        if isinstance(exc, KnowledgeStale | HubConflict):
            return HTTPException(409, str(exc))
        if isinstance(exc, InputRejected):
            return HTTPException(400, str(exc))
        return HTTPException(400, "request refused by a data rule")

    async def draft_out(doc: KnowledgeDoc) -> KnowledgeDraftOut:
        base = (
            await asyncio.to_thread(
                deps.repo.knowledge_version, doc.channel_id, str(doc.kind), doc.base_version
            )
            if doc.base_version
            else None
        )
        current = [
            d
            for d in await asyncio.to_thread(deps.repo.knowledge_current, doc.channel_id)
            if d.kind == doc.kind
        ]
        current_version = current[0].version if current else None
        return KnowledgeDraftOut(
            **_doc_out(doc),
            content=doc.content,
            base_content=base.content if base else None,
            current_version=current_version,
            stale=doc.status == "draft" and doc.base_version != current_version,
            diff=[DiffLineOut(**d) for d in line_diff(base.content if base else "", doc.content)],
        )

    @kb.get("/knowledge/drafts", response_model=list[KnowledgeDocOut], dependencies=[read_limit])
    async def knowledge_drafts(ctx: Session, channel: str | None = None) -> list[KnowledgeDocOut]:
        docs = await asyncio.to_thread(deps.repo.knowledge_drafts, channel)
        return [KnowledgeDocOut(**_doc_out(d)) for d in docs]

    @kb.get("/knowledge/drafts/{doc_id}", response_model=KnowledgeDraftOut,
            dependencies=[read_limit])  # fmt: skip
    async def knowledge_draft(doc_id: str, ctx: Session) -> KnowledgeDraftOut:
        doc = await asyncio.to_thread(deps.repo.knowledge_doc, doc_id)
        if doc is None:
            raise HTTPException(404, "knowledge draft not found")
        return await draft_out(doc)

    @kb.post("/knowledge/drafts/{doc_id}/approve", response_model=KnowledgeDocOut,
             dependencies=[write_limit])  # fmt: skip
    async def knowledge_approve(
        doc_id: str, body: KnowledgeDecisionIn, ctx: Session
    ) -> KnowledgeDocOut:
        user = require_role(ctx, "approver")
        await require_stepup(
            ctx, body.stepup, "knowledge", [doc_id, "approve", body.content_sha256]
        )
        try:
            doc = await asyncio.to_thread(
                deps.repo.decide_knowledge, doc_id, body.content_sha256,
                username=user.username, approve=True,
            )  # fmt: skip
        except StoreError as exc:
            raise store_error(exc) from exc
        return KnowledgeDocOut(**_doc_out(doc))

    @kb.post("/knowledge/drafts/{doc_id}/reject", response_model=KnowledgeDocOut,
             dependencies=[write_limit])  # fmt: skip
    async def knowledge_reject(
        doc_id: str, body: KnowledgeRejectIn, ctx: Session
    ) -> KnowledgeDocOut:
        user = require_role(ctx, "approver")
        await require_stepup(ctx, body.stepup, "knowledge", [doc_id, "reject", body.content_sha256])
        try:
            doc = await asyncio.to_thread(
                deps.repo.decide_knowledge, doc_id, body.content_sha256,
                username=user.username, approve=False, reason=body.reason,
            )  # fmt: skip
        except StoreError as exc:
            raise store_error(exc) from exc
        return KnowledgeDocOut(**_doc_out(doc))

    @kb.get("/channels/{channel_id}/knowledge", response_model=list[KnowledgeDocOut],
            dependencies=[read_limit])  # fmt: skip
    async def knowledge_current(channel_id: str, ctx: Session) -> list[KnowledgeDocOut]:
        docs = await asyncio.to_thread(deps.repo.knowledge_current, channel_id)
        return [KnowledgeDocOut(**_doc_out(d)) for d in docs]

    @kb.post("/channels/{channel_id}/knowledge", response_model=KnowledgeDocOut,
             dependencies=[write_limit])  # fmt: skip
    async def knowledge_propose(
        channel_id: str, body: KnowledgeProposeIn, ctx: Session
    ) -> KnowledgeDocOut:
        """Submit a draft. Drafts never take effect until an approver approves them."""
        user = require_role(ctx, "approver", "operator", "admin")
        if deps.knowledge is None:
            raise HTTPException(503, "knowledge store not configured")
        try:
            doc = await asyncio.to_thread(
                deps.knowledge.channel(channel_id).propose, KnowledgeKind(body.kind),
                body.content, proposed_by=user.username, base_version=body.base_version,
            )  # fmt: skip
        except StoreError as exc:
            raise store_error(exc) from exc
        return KnowledgeDocOut(**_doc_out(doc))

    # ------------------------------------------------------------------ owner inputs

    @kb.get("/channels/{channel_id}/inputs", response_model=list[OwnerInputOut], tags=["inputs"],
            dependencies=[read_limit])  # fmt: skip
    async def list_inputs(
        channel_id: str, ctx: Session, status: Literal["new", "used", "archived"] | None = None
    ) -> list[OwnerInputOut]:
        if deps.inputs is None:
            raise HTTPException(503, "input store not configured")
        items = await asyncio.to_thread(deps.inputs.list_for, channel_id, status)
        return [_input_out(i) for i in items]

    @kb.post("/channels/{channel_id}/inputs", response_model=OwnerInputOut, tags=["inputs"],
             dependencies=[write_limit])  # fmt: skip
    async def add_input(channel_id: str, body: OwnerInputIn, ctx: Session) -> OwnerInputOut:
        """Submit an idea, footage reference, note or goal for the channel."""
        user = require_role(ctx, "operator", "admin")
        if deps.inputs is None:
            raise HTTPException(503, "input store not configured")
        try:
            item = await asyncio.to_thread(
                deps.inputs.add, channel_id, InputKind(body.kind), body=body.body,
                uri=body.uri, sha256=body.sha256, submitted_by=user.username,
            )  # fmt: skip
        except StoreError as exc:
            raise store_error(exc) from exc
        return _input_out(item)

    app.include_router(kb)

    @app.get("/api/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def export_openapi(app: FastAPI) -> str:
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
