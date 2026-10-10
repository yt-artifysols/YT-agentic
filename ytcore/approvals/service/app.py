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
from ytcore.approvals.service.repo import Credential, HubRepository, User
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
        return request.client.host if request.client else "unknown"

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

    auth = APIRouter(prefix="/api/auth", tags=["auth"], dependencies=[auth_limit])

    @auth.post("/register/options")
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

    @auth.post("/register/verify", response_model=Me)
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

    @auth.post("/login/options")
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

    @auth.post("/login/verify", response_model=Me)
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

    @auth.post("/logout", response_model=Ok)
    async def logout(ctx: Session, request: Request, response: Response) -> Ok:
        await deps.sessions.end(request, response)
        await asyncio.to_thread(
            deps.repo.audit, f"hub:{ctx.user.username}", "hub.logout",
            f"user:{ctx.user.username}", {},
        )  # fmt: skip
        return Ok()

    @auth.get("/me", response_model=Me)
    async def whoami(ctx: Session) -> Me:
        return me(ctx.user)

    @auth.post("/csrf", response_model=Me)
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

    @app.get("/api/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def export_openapi(app: FastAPI) -> str:
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
