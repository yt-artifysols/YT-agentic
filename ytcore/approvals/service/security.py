"""Sessions, CSRF, origin checks, roles and rate limiting for the approval hub.

Passkey (WebAuthn) verification is done by py_webauthn (see auth routes); this module never
implements cryptography itself. Session and CSRF secrets are random tokens stored only as
SHA-256 hashes server-side.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, Response

from ytcore.approvals.service.repo import HubRepository, Role, User, sha256
from ytcore.approvals.service.settings import HubSettings

SESSION_COOKIE = "hub_session"
CEREMONY_COOKIE = "hub_ceremony"
CSRF_HEADER = "x-csrf-token"


@dataclass(frozen=True)
class AuthContext:
    user: User
    token: str


class RateLimiter:
    """Sliding-window limiter per (client, bucket). In-process: run one hub instance or move
    this to a shared store before scaling out."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, client: str, bucket: str, per_minute: int) -> None:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[(client, bucket)]
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= per_minute:
                raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})
            hits.append(now)


@dataclass
class Ceremony:
    challenge: bytes
    kind: str  # register | login
    expires: float
    user_id: str | None = None
    invite_token: str | None = None


class CeremonyStore:
    """Short-lived WebAuthn challenges, bound to an HttpOnly cookie. In-process."""

    TTL = 300.0

    def __init__(self) -> None:
        self._items: dict[str, Ceremony] = {}
        self._lock = threading.Lock()

    def put(self, ceremony: Ceremony) -> str:
        key = secrets.token_urlsafe(32)
        with self._lock:
            now = time.monotonic()
            self._items = {k: v for k, v in self._items.items() if v.expires > now}
            self._items[key] = ceremony
        return key

    def pop(self, key: str | None, kind: str) -> Ceremony:
        with self._lock:
            ceremony = self._items.pop(key, None) if key else None
        if ceremony is None or ceremony.kind != kind or ceremony.expires < time.monotonic():
            raise HTTPException(400, "no active passkey ceremony; start again")
        return ceremony


class SessionManager:
    def __init__(self, repo: HubRepository, settings: HubSettings) -> None:
        self._repo = repo
        self._s = settings

    def _cookie(self, response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            httponly=True,
            secure=self._s.cookie_secure,
            samesite="strict",
            path="/",
        )

    def set_ceremony_cookie(self, response: Response, key: str) -> None:
        self._cookie(response, CEREMONY_COOKIE, key, int(CeremonyStore.TTL))

    async def start(self, response: Response, user: User) -> str:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(hours=self._s.session_absolute_hours)
        await asyncio.to_thread(
            self._repo.create_session, user.id, token, csrf, expires, actor=f"hub:{user.username}"
        )
        self._cookie(response, SESSION_COOKIE, token, self._s.session_absolute_hours * 3600)
        response.delete_cookie(CEREMONY_COOKIE, path="/")
        return csrf

    async def end(self, request: Request, response: Response) -> None:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            await asyncio.to_thread(self._repo.revoke_session, token)
        response.delete_cookie(SESSION_COOKIE, path="/")

    async def rotate_csrf(self, token: str | None) -> tuple[User, str]:
        if not token:
            raise HTTPException(401, "not signed in")
        sess = await asyncio.to_thread(self._repo.session, token)
        if sess is None or sess.user.disabled or self._expired(sess.expires_at, sess.last_seen_at):
            raise HTTPException(401, "not signed in")
        csrf = secrets.token_urlsafe(32)
        await asyncio.to_thread(self._repo.rotate_csrf, token, csrf)
        return sess.user, csrf

    def _expired(self, expires_at: datetime, last_seen_at: datetime) -> bool:
        now = datetime.now(UTC)
        idle = timedelta(minutes=self._s.session_idle_minutes)
        return now >= expires_at or now - last_seen_at > idle

    async def authenticate(self, request: Request) -> AuthContext:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise HTTPException(401, "not signed in")
        sess = await asyncio.to_thread(self._repo.session, token)
        if sess is None or sess.user.disabled:
            raise HTTPException(401, "not signed in")
        if self._expired(sess.expires_at, sess.last_seen_at):
            await asyncio.to_thread(self._repo.revoke_session, token)
            raise HTTPException(401, "session expired")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            sent = request.headers.get(CSRF_HEADER, "")
            if not sent or not hmac.compare_digest(sha256(sent), sess.csrf_hash):
                raise HTTPException(403, "missing or invalid CSRF token")
        await asyncio.to_thread(self._repo.touch_session, token)
        return AuthContext(sess.user, token)


def check_origin(request: Request, settings: HubSettings) -> None:
    """Unsafe methods must come from the hub's own origin (defense in depth over SameSite)."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if request.headers.get("origin") != settings.origin:
        raise HTTPException(403, "cross-origin request refused")


def require_role(ctx: AuthContext, *roles: Role) -> User:
    if ctx.user.role not in roles:
        raise HTTPException(403, f"requires role: {' or '.join(roles)}")
    return ctx.user
