"""Step-up: a fresh passkey assertion for each approval, batch, switch release and promotion.
A stolen session cookie alone (or a replayed or mis-targeted assertion) approves nothing."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.integration.hubkit import Hub, build_hub
from ytcore.workflow.states import JobState

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]


@pytest.fixture
def hub(database: str) -> Hub:
    return build_hub(database, require_stepup=True)


async def test_approving_needs_a_fresh_passkey_assertion_for_that_exact_video(
    hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    a, ha = hub.awaiting_job(ch)
    b, hb = hub.awaiting_job(ch)
    async with hub.client() as c:
        csrf = await hub.register(c, "stepper", "approver")
        h = {"x-csrf-token": csrf}
        # the session alone is not enough
        res = await c.post(f"/api/jobs/{a}/approve", json={"content_hash": ha}, headers=h)
        assert res.status_code == 428
        assert hub.store.get_state(a) == JobState.AWAITING_APPROVAL
        # an assertion for ANOTHER video does not approve this one
        wrong = await hub.stepup(c, "stepper", csrf, "approve", [b, hb])
        res = await c.post(
            f"/api/jobs/{a}/approve", json={"content_hash": ha, "stepup": wrong}, headers=h
        )
        assert res.status_code == 428 and hub.store.get_state(a) == JobState.AWAITING_APPROVAL
        # the right one works, once
        proof = await hub.stepup(c, "stepper", csrf, "approve", [a, ha])
        ok = await c.post(
            f"/api/jobs/{a}/approve", json={"content_hash": ha, "stepup": proof}, headers=h
        )
        assert ok.status_code == 200, ok.text
        # replaying the same assertion for the other video fails
        replay = await c.post(
            f"/api/jobs/{b}/approve", json={"content_hash": hb, "stepup": proof}, headers=h
        )
        assert replay.status_code == 428
        assert hub.store.get_state(b) == JobState.AWAITING_APPROVAL
    assert sql("SELECT count(*) FROM approvals WHERE job_id = %s", a)[0][0] == 1


async def test_a_stepup_ticket_works_once_and_only_for_its_owner(
    hub: Hub, channel: Callable[..., str]
) -> None:
    ch = channel()
    job, h1 = hub.awaiting_job(ch)
    async with hub.client() as c, hub.client() as other:
        csrf = await hub.register(c, "owner-user", "approver")
        csrf2 = await hub.register(other, "thief-user", "approver")
        proof = await hub.stepup(c, "owner-user", csrf, "approve", [job, h1])
        # another signed-in approver cannot use somebody else's ticket
        stolen = await other.post(
            f"/api/jobs/{job}/approve",
            json={"content_hash": h1, "stepup": proof},
            headers={"x-csrf-token": csrf2},
        )
        assert stolen.status_code == 428
        # the ticket was consumed by the failed attempt: even the owner needs a new one
        again = await c.post(
            f"/api/jobs/{job}/approve",
            json={"content_hash": h1, "stepup": proof},
            headers={"x-csrf-token": csrf},
        )
        assert again.status_code == 428
        assert hub.store.get_state(job) == JobState.AWAITING_APPROVAL


async def test_the_assertion_must_come_from_the_users_own_passkey(
    hub: Hub, channel: Callable[..., str]
) -> None:
    ch = channel()
    job, h1 = hub.awaiting_job(ch)
    async with hub.client() as c, hub.client() as other:
        csrf = await hub.register(c, "real-user", "approver")
        await hub.register(other, "other-passkey", "approver")
        res = await c.post(
            "/api/auth/stepup/options",
            json={"purpose": "approve", "parts": [job, h1]},
            headers={"x-csrf-token": csrf},
        )
        body = res.json()
        options = {**body["options"], "allowCredentials": []}  # a rogue authenticator ignores it
        forged = hub.authenticators["other-passkey"].get(options)
        bad = await c.post(
            f"/api/jobs/{job}/approve",
            json={"content_hash": h1, "stepup": {"ticket": body["ticket"], "credential": forged}},
            headers={"x-csrf-token": csrf},
        )
    assert bad.status_code == 428 and hub.store.get_state(job) == JobState.AWAITING_APPROVAL


async def test_batch_approval_and_releasing_a_switch_need_proof_but_engaging_does_not(
    hub: Hub, channel: Callable[..., str]
) -> None:
    ch = channel()
    job, h1 = hub.awaiting_job(ch, risk=3)
    hub.promote(ch)
    async with hub.client() as c:
        csrf = await hub.register(c, "ops-user", "approver")
        h = {"x-csrf-token": csrf}
        items = [{"job_id": job, "content_hash": h1}]
        assert (await c.post("/api/jobs/batch-approve", json={"items": items}, headers=h)
                ).status_code == 428  # fmt: skip
        proof = await hub.stepup(c, "ops-user", csrf, "batch", [f"{job}:{h1}"])
        done = await c.post(
            "/api/jobs/batch-approve", json={"items": items, "stepup": proof}, headers=h
        )
        assert done.status_code == 200 and done.json()["results"][0]["ok"] is True
    async with hub.client() as c:
        csrf = await hub.register(c, "switch-op", "operator")
        h = {"x-csrf-token": csrf}
        engage = await c.post(
            "/api/controls/global", json={"paused": True, "reason": "drill"}, headers=h
        )
        assert engage.status_code == 200  # stopping is never blocked by the extra step
        release = await c.post(
            "/api/controls/global", json={"paused": False, "reason": "drill over"}, headers=h
        )
        assert release.status_code == 428
        proof = await hub.stepup(c, "switch-op", csrf, "release", ["global", "*"])
        release = await c.post(
            "/api/controls/global",
            json={"paused": False, "reason": "drill over", "stepup": proof},
            headers=h,
        )
        assert release.status_code == 200


async def test_an_expired_stepup_challenge_is_refused(
    hub: Hub, channel: Callable[..., str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ch = channel()
    job, h1 = hub.awaiting_job(ch)
    async with hub.client() as c:
        csrf = await hub.register(c, "slow-user", "approver")
        proof = await hub.stepup(c, "slow-user", csrf, "approve", [job, h1])
        import time

        real = time.monotonic
        monkeypatch.setattr(time, "monotonic", lambda: real() + 600)
        res = await c.post(
            f"/api/jobs/{job}/approve",
            json={"content_hash": h1, "stepup": proof},
            headers={"x-csrf-token": csrf},
        )
    assert res.status_code == 428
