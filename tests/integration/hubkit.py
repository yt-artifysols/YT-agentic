"""Shared wiring for approval hub integration tests."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg

from tests.harness import Keys, drive_to_awaiting
from tests.softauthn import SoftAuthenticator
from ytcore.approvals.service.app import HubDeps, create_app
from ytcore.approvals.service.hub import ApprovalHub
from ytcore.approvals.service.notify import FakeNotifier
from ytcore.approvals.service.repo import HubRepository, User
from ytcore.approvals.service.security import CeremonyStore, RateLimiter, SessionManager
from ytcore.approvals.service.settings import HubSettings
from ytcore.approvals.service.signals import FakeSignaler, WorkflowSignaler
from ytcore.channels.rollout import RolloutService
from ytcore.gateway.assets import InMemoryAssetSource
from ytcore.inputs.store import PostgresInputs
from ytcore.knowledge.store import PostgresKnowledge
from ytcore.store.postgres import PostgresStore

ORIGIN = "http://localhost:3000"


@dataclass
class Hub:
    settings: HubSettings
    repo: HubRepository
    store: PostgresStore
    keys: Keys
    signaler: Any
    notifier: FakeNotifier
    assets: InMemoryAssetSource
    app: Any
    authenticators: dict[str, SoftAuthenticator] = field(default_factory=dict)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
        )

    def invite(self, username: str, role: str) -> tuple[User, str]:
        user = self.repo.create_user(username, username.title(), role, actor="hub:test")  # type: ignore[arg-type]
        token = secrets.token_urlsafe(32)
        self.repo.create_invite(
            user.id, token, datetime.now(UTC) + timedelta(hours=1), actor="hub:test"
        )
        return user, token

    async def register(self, client: httpx.AsyncClient, username: str, role: str) -> str:
        """Invite + passkey registration; returns the CSRF token of the new session."""
        _, invite = self.invite(username, role)
        auth = SoftAuthenticator(self.settings.rp_id, ORIGIN)
        self.authenticators[username] = auth
        opts = await client.post("/api/auth/register/options", json={"invite_token": invite})
        assert opts.status_code == 200, opts.text
        res = await client.post(
            "/api/auth/register/verify", json={"credential": auth.create(opts.json())}
        )
        assert res.status_code == 200, res.text
        return str(res.json()["csrf_token"])

    async def login(self, client: httpx.AsyncClient, username: str) -> str:
        auth = self.authenticators[username]
        opts = await client.post("/api/auth/login/options", json={"username": username})
        res = await client.post(
            "/api/auth/login/verify", json={"credential": auth.get(opts.json())}
        )
        assert res.status_code == 200, res.text
        return str(res.json()["csrf_token"])

    def awaiting_job(self, channel_id: str, *, risk: int = 10, flags: int = 0) -> tuple[str, str]:
        job = self.store.create_job(channel_id, "owner", actor="planner")
        chash = drive_to_awaiting(self.store, self.assets, job, channel_id)
        self.store.record_qa(
            job,
            passed=True,
            risk_score=risk,
            flags=[{"claim": f"claim {i}", "reason": "unsupported"} for i in range(flags)],
            sources=[{"url": "https://example.test/source", "title": "Example"}],
            actor="qa-agent",
        )
        return job, chash


def build_hub(
    conninfo: str,
    *,
    signaler: WorkflowSignaler | None = None,
    keys: Keys | None = None,
    **overrides: Any,
) -> Hub:
    settings = HubSettings(origin=ORIGIN, rp_id="localhost", **overrides)
    keys = keys or Keys()
    repo = HubRepository.from_conninfo(conninfo, fast_lane_max_risk=settings.fast_lane_max_risk)
    store = PostgresStore.from_conninfo(conninfo, keys.verifier)
    signaler = signaler or FakeSignaler()
    notifier = FakeNotifier()
    assets = InMemoryAssetSource()
    hub = ApprovalHub(
        repo=repo,
        store=store,
        signer=keys.signer,
        signaler=signaler,
        notifier=notifier,
        token_ttl=timedelta(hours=settings.token_ttl_hours),
        batch_max_items=settings.batch_max_items,
    )
    app = create_app(
        HubDeps(
            settings=settings,
            repo=repo,
            hub=hub,
            assets=assets,
            sessions=SessionManager(repo, settings),
            ceremonies=CeremonyStore(),
            limiter=RateLimiter(),
            inputs=PostgresInputs.from_conninfo(conninfo),
            rollout=RolloutService(lambda: psycopg.connect(conninfo, autocommit=True)),
            knowledge=PostgresKnowledge.from_conninfo(conninfo),
        )
    )
    return Hub(settings, repo, store, keys, signaler, notifier, assets, app)
