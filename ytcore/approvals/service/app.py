"""FastAPI app for the approval hub. All state-changing routes are POST, need a valid session,
the right role, a CSRF token and a same-origin request, and are rate limited and audited."""

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

from ytcore.approvals.service.hub import ApprovalHub, HubError
from ytcore.approvals.service.repo import Credential, HubConflict, HubRepository, User, sha256
from ytcore.approvals.service.security import (
    CEREMONY_COOKIE,
    AuthContext,
    Ceremony,
    CeremonyStore,
    RateLimiter,
    SessionManager,
    check_origin,
    require_role,
)
from ytcore.approvals.service.settings import HubSettings
from ytcore.gateway.assets import AssetSource, read_verified
from ytcore.inputs.models import InputKind, OwnerInput
from ytcore.inputs.store import InputStore
from ytcore.knowledge.diff import line_diff
from ytcore.knowledge.models import KnowledgeDoc, KnowledgeKind
from ytcore.store.errors import (
    InputRejected,
    KnowledgeForbidden,
    KnowledgeStale,
    StoreError,
)

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
    previous: PreviousVersionOut | None


class ApproveIn(BaseModel):
    content_hash: ContentHash


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


class BatchItemOut(BaseModel):
    job_id: str
    ok: bool
    jti: str | None = None
    error: str | None = None


class BatchOut(BaseModel):
    results: list[BatchItemOut]


class KillSwitchIn(BaseModel):
    paused: bool
    reason: str = Field(default="", max_length=500)


class InviteIn(BaseModel):
    username: str = Field(pattern=r"^[a-z0-9][a-z0-9._@-]{2,63}$")
    display_name: str = Field(min_length=1, max_length=100)
    role: Literal["approver", "operator", "admin"]


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
    ceremonies: CeremonyStore
    limiter: RateLimiter
    inputs: InputStore | None = None
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

    def client_key(request: Request) -> str:
        # Signed-in users are limited per session; anonymous callers per client address.
        session = request.cookies.get("hub_session")
        if session:
            return "s:" + sha256(session).hex()[:32]
        if s.trust_forwarded_for:
            forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            if forwarded:
                return "f:" + forwarded
        return "a:" + (request.client.host if request.client else "unknown")

    def limit(bucket: str, per_minute: int) -> Any:
        def dep(request: Request) -> None:
            deps.limiter.check(client_key(request), bucket, per_minute)

        return Depends(dep)

    async def session(request: Request) -> AuthContext:
        check_origin(request, s)
        return await deps.sessions.authenticate(request)

    Session = Annotated[AuthContext, Depends(session)]
    auth_limit = limit("auth", s.rate_auth_per_minute)
    write_limit = limit("write", s.rate_write_per_minute)
    read_limit = limit("read", s.rate_read_per_minute)

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
    ) -> list[QueueItemOut]:
        items = await asyncio.to_thread(
            deps.repo.queue,
            cluster=cluster,
            channel=channel,
            min_risk=min_risk,
            max_risk=max_risk,
            fast_lane_only=fast_lane,
        )
        return [QueueItemOut(**i.__dict__) for i in items]

    @api.get("/jobs/{job_id}", response_model=PreviewOut, dependencies=[read_limit])
    async def preview(job_id: str, ctx: Session) -> PreviewOut:
        head = await asyncio.to_thread(deps.repo.head, job_id)
        if head is None or head.manifest is None or head.content_hash is None:
            raise HTTPException(404, "job not found")
        qa = await asyncio.to_thread(deps.repo.qa, job_id)
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
            fast_lane=await asyncio.to_thread(deps.repo.is_fast_lane, job_id),
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
        results = await deps.hub.batch_approve(
            [(i.job_id, i.content_hash) for i in body.items], user
        )
        return BatchOut(results=[BatchItemOut(**r.__dict__) for r in results])

    # ------------------------------------------------------------------ kill switches

    @api.get("/controls", tags=["controls"], dependencies=[read_limit])
    async def controls(ctx: Session) -> dict[str, Any]:
        return await asyncio.to_thread(deps.repo.controls)

    @api.post("/controls/global", tags=["controls"], response_model=Ok, dependencies=[write_limit])
    async def kill_global(body: KillSwitchIn, ctx: Session) -> Ok:
        user = require_role(ctx, "operator", "admin")
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
        await deps.hub.set_kill_switch(scope[:-1], ident, body.paused, body.reason, user)
        return Ok()

    # ------------------------------------------------------------------ admin

    @api.post("/admin/invites", tags=["admin"], response_model=InviteOut,
              dependencies=[write_limit])  # fmt: skip
    async def invite(body: InviteIn, ctx: Session) -> InviteOut:
        admin = require_role(ctx, "admin")
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
