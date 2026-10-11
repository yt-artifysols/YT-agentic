"""Approval hub API against real Postgres: passkeys, roles, CSRF, approve/edit/reject/batch,
kill switches, notifications, rate limits and audit."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.contract import validate
from tests.integration.hubkit import ORIGIN, Hub, build_hub
from ytcore.approvals.service.notify import notify_pending
from ytcore.workflow.states import JobState

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]


@pytest.fixture
def hub(database: str) -> Hub:
    return build_hub(database)


def _audit(sql: SQL, action: str, subject: str) -> int:
    return sql(
        "SELECT count(*) FROM audit_events WHERE action = %s AND subject = %s", action, subject
    )[0][0]


# --------------------------------------------------------------------------- passkeys


async def test_passkey_register_login_and_logout(hub: Hub, sql: SQL) -> None:
    async with hub.client() as c:
        await hub.register(c, "alice", "approver")
        me = await c.get("/api/auth/me")
        validate("/api/auth/me", "get", me.json())
        assert me.json()["username"] == "alice" and me.json()["role"] == "approver"
        csrf = await hub.login(c, "alice")
        assert (await c.post("/api/auth/logout", headers={"x-csrf-token": csrf})).status_code == 200
        assert (await c.get("/api/auth/me")).status_code == 401
    assert _audit(sql, "hub.passkey.registered", "user:alice") == 1
    assert _audit(sql, "hub.login", "user:alice") == 2
    assert _audit(sql, "hub.logout", "user:alice") == 1
    stored = sql("SELECT sign_count FROM webauthn_credentials c JOIN hub_users u "
                 "ON u.id = c.user_id WHERE u.username = 'alice'")  # fmt: skip
    assert stored[0][0] == 1  # counter advanced by the login


async def test_invite_is_single_use_and_secrets_are_hashed(hub: Hub, sql: SQL) -> None:
    _, invite = hub.invite("bob", "approver")
    async with hub.client() as c:
        assert (
            await c.post("/api/auth/register/options", json={"invite_token": invite})
        ).status_code == 200
        from tests.softauthn import SoftAuthenticator

        auth = SoftAuthenticator("localhost", ORIGIN)
        opts = await c.post("/api/auth/register/options", json={"invite_token": invite})
        ok = await c.post(
            "/api/auth/register/verify", json={"credential": auth.create(opts.json())}
        )
        assert ok.status_code == 200
        again = await c.post("/api/auth/register/options", json={"invite_token": invite})
        assert again.status_code == 400
    tokens = sql("SELECT encode(token_hash, 'hex') FROM hub_invites")
    assert all(invite not in t[0] for t in tokens)


async def test_login_fails_for_wrong_key_wrong_origin_and_without_user_verification(
    hub: Hub,
) -> None:
    from tests.softauthn import SoftAuthenticator

    async with hub.client() as c:
        await hub.register(c, "carol", "approver")
        await c.post("/api/auth/logout", headers={"x-csrf-token": "x"})  # ignored, no session use
        c.cookies.clear()
        # A different authenticator (not registered) is refused.
        opts = await c.post("/api/auth/login/options", json={"username": "carol"})
        rogue = SoftAuthenticator("localhost", ORIGIN)
        rogue.credential_id = hub.authenticators["carol"].credential_id
        res = await c.post("/api/auth/login/verify", json={"credential": rogue.get(opts.json())})
        assert res.status_code == 401
        # The right authenticator but no user verification is refused.
        opts = await c.post("/api/auth/login/options", json={"username": "carol"})
        cred = hub.authenticators["carol"].get(opts.json(), user_verified=False)
        assert (
            await c.post("/api/auth/login/verify", json={"credential": cred})
        ).status_code == 401
        # Phishing origin is refused before any ceremony starts.
        evil = await c.post(
            "/api/auth/login/options", json={"username": "carol"},
            headers={"Origin": "https://evil.example"},
        )  # fmt: skip
        assert evil.status_code == 403


async def test_session_idle_timeout(hub: Hub, sql: SQL) -> None:
    async with hub.client() as c:
        await hub.register(c, "dave", "approver")
        sql("UPDATE hub_sessions SET last_seen_at = now() - interval '2 hours'")
        assert (await c.get("/api/jobs")).status_code == 401


async def test_auth_rate_limit(hub: Hub) -> None:
    async with hub.client() as c:
        codes = [
            (await c.post("/api/auth/login/options", json={"username": "nobody"})).status_code
            for _ in range(hub.settings.rate_auth_per_minute + 2)
        ]
    assert codes[0] == 200 and codes[-1] == 429


async def test_rotating_a_fake_session_cookie_does_not_bypass_the_auth_limit(hub: Hub) -> None:
    async with hub.client() as c:
        codes = []
        for i in range(hub.settings.rate_auth_per_minute + 2):
            c.cookies.set("hub_session", f"made-up-{i}")
            res = await c.post("/api/auth/login/options", json={"username": "nobody"})
            codes.append(res.status_code)
    assert codes[-1] == 429


def test_the_rate_limiter_keeps_bounded_memory() -> None:
    from ytcore.approvals.service.security import RateLimiter

    limiter = RateLimiter()
    limiter.MAX_KEYS = 100  # type: ignore[misc]
    for i in range(1000):
        limiter.check(f"client-{i}", "auth", 10)
    assert len(limiter._hits) <= 101


async def test_strict_auth_limit_does_not_lock_out_signed_in_pages(hub: Hub) -> None:
    async with hub.client() as c:
        await hub.register(c, "nina", "approver")
        statuses = {(await c.get("/api/auth/me")).status_code for _ in range(30)}
    assert statuses == {200}


# --------------------------------------------------------------------------- approval actions


async def test_queue_preview_and_approve(hub: Hub, channel: Callable[..., str], sql: SQL) -> None:
    ch = channel()
    hub.promote(ch)
    low, low_hash = hub.awaiting_job(ch, risk=5)
    high, _ = hub.awaiting_job(ch, risk=80, flags=2)
    async with hub.client() as c:
        csrf = await hub.register(c, "erin", "approver")
        queue = (await c.get("/api/jobs", params={"channel": ch})).json()
        validate("/api/jobs", "get", queue)
        assert [q["job_id"] for q in queue] == [high, low]  # risk-sorted, highest first
        assert [q["fast_lane"] for q in queue] == [False, True]
        preview = (await c.get(f"/api/jobs/{low}")).json()
        validate("/api/jobs/{job_id}", "get", preview)
        assert preview["content_hash"] == low_hash and preview["video_url"]
        assert preview["qa"]["sources"][0]["url"] == "https://example.test/source"
        video = await c.get(preview["video_url"])
        assert video.status_code == 200 and video.headers["content-type"] == "video/mp4"

        res = await c.post(f"/api/jobs/{low}/approve", json={"content_hash": low_hash},
                           headers={"x-csrf-token": csrf})  # fmt: skip
        assert res.status_code == 200, res.text
        validate("/api/jobs/{job_id}/approve", "post", res.json())
        jti = res.json()["jti"]

    # Approvals row, state, signal and audit all line up.
    assert sql("SELECT approver, content_hash FROM approvals WHERE token_jti = %s", jti) == [
        ("erin", low_hash)
    ]
    assert hub.store.get_state(low) == JobState.APPROVED
    job_id, decision = hub.signaler.sent[-1]
    assert job_id == low and decision.action == "approve"
    claims = hub.keys.verifier.verify(
        decision.token, job_id=low, channel_id=ch, content_hash=low_hash
    )
    assert claims.jti == jti and claims.approver == "erin"
    assert _audit(sql, "hub.approve", f"job:{low}") == 1
    assert _audit(sql, "approval.consumed", f"job:{low}") == 1


async def test_stale_preview_cannot_be_approved(
    hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    job, old_hash = hub.awaiting_job(channel())
    async with hub.client() as c:
        csrf = await hub.register(c, "frank", "approver")
        edit = await c.post(f"/api/jobs/{job}/edit",
                            json={"content_hash": old_hash, "title": "A better title"},
                            headers={"x-csrf-token": csrf})  # fmt: skip
        assert edit.status_code == 200, edit.text
        validate("/api/jobs/{job_id}/edit", "post", edit.json())
        new_hash = edit.json()["content_hash"]
        assert new_hash != old_hash and edit.json()["manifest_version"] == 2
        assert hub.store.get_state(job) == JobState.QA
        assert sql("SELECT count(*) FROM approvals WHERE job_id = %s", job)[0][0] == 0
        assert hub.signaler.sent[-1][1].action == "edit"
        assert hub.signaler.sent[-1][1].content_hash == new_hash

        stale = await c.post(f"/api/jobs/{job}/approve", json={"content_hash": old_hash},
                             headers={"x-csrf-token": csrf})  # fmt: skip
        assert stale.status_code == 409
    assert sql("SELECT count(*) FROM approvals WHERE job_id = %s", job)[0][0] == 0
    assert _audit(sql, "hub.edit", f"job:{job}") == 1


async def test_edit_shows_diff_against_previous_version(
    hub: Hub, channel: Callable[..., str]
) -> None:
    job, h = hub.awaiting_job(channel())
    async with hub.client() as c:
        csrf = await hub.register(c, "gina", "approver")
        await c.post(f"/api/jobs/{job}/edit", json={"content_hash": h, "title": "Edited"},
                     headers={"x-csrf-token": csrf})  # fmt: skip
        hub.store.transition(job, JobState.AWAITING_APPROVAL, actor="orchestrator")  # QA passed
        preview = (await c.get(f"/api/jobs/{job}")).json()
    assert preview["title"] == "Edited"
    assert preview["previous"]["version"] == 1
    assert preview["previous"]["metadata"]["title"] == "Test video"


async def test_reject_requires_reason_and_stage(
    hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    job, h = hub.awaiting_job(channel())
    async with hub.client() as c:
        csrf = await hub.register(c, "hank", "approver")
        hdr = {"x-csrf-token": csrf}
        short = await c.post(f"/api/jobs/{job}/reject",
                             json={"content_hash": h, "reason": "no", "rework_stage": "script"},
                             headers=hdr)  # fmt: skip
        assert short.status_code == 422
        bad_stage = await c.post(f"/api/jobs/{job}/reject",
                                 json={"content_hash": h, "reason": "unsupported health claim",
                                       "rework_stage": "publish"}, headers=hdr)  # fmt: skip
        assert bad_stage.status_code == 422
        ok = await c.post(f"/api/jobs/{job}/reject",
                          json={"content_hash": h, "reason": "unsupported health claim",
                                "rework_stage": "script"}, headers=hdr)  # fmt: skip
        assert ok.status_code == 200
    assert sql("SELECT rework_stage, rejected_by FROM job_rejections WHERE job_id = %s", job) == [
        ("script", "hank")
    ]
    assert hub.signaler.sent[-1][1].action == "reject"
    assert hub.signaler.sent[-1][1].rework_stage == "script"
    assert _audit(sql, "approval.rejected", f"job:{job}") == 1


async def test_batch_approve_fast_lane_only_with_per_video_hash_check(
    hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    hub.promote(ch)
    a, ha = hub.awaiting_job(ch, risk=5)
    b, hb = hub.awaiting_job(ch, risk=8)
    risky, hr = hub.awaiting_job(ch, risk=70)
    stale, _ = hub.awaiting_job(ch, risk=5)
    async with hub.client() as c:
        csrf = await hub.register(c, "ivy", "approver")
        items = [
            {"job_id": a, "content_hash": ha},
            {"job_id": b, "content_hash": hb},
            {"job_id": risky, "content_hash": hr},
            {"job_id": stale, "content_hash": "sha256:" + "0" * 64},
        ]
        res = await c.post("/api/jobs/batch-approve", json={"items": items},
                           headers={"x-csrf-token": csrf})  # fmt: skip
    validate("/api/jobs/batch-approve", "post", res.json())
    results = {r["job_id"]: r for r in res.json()["results"]}
    assert results[a]["ok"] and results[b]["ok"]
    assert results[a]["jti"] != results[b]["jti"]  # one token per video
    assert not results[risky]["ok"] and "fast lane" in results[risky]["error"]
    assert not results[stale]["ok"] and "changed" in results[stale]["error"]
    assert hub.store.get_state(risky) == JobState.AWAITING_APPROVAL
    assert hub.store.get_state(stale) == JobState.AWAITING_APPROVAL
    assert sql("SELECT count(*) FROM approvals WHERE job_id IN (%s, %s)", a, b)[0][0] == 2


async def test_fast_lane_needs_qa_for_the_current_content(
    hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    hub.promote(ch)
    job, h = hub.awaiting_job(ch, risk=5)
    assert hub.repo.is_fast_lane(job)
    sql("INSERT INTO qa_reports (job_id, content_hash, passed, risk_score) "
        "VALUES (%s, %s, true, 1)", job, "sha256:" + "e" * 64)  # fmt: skip
    assert not hub.repo.is_fast_lane(job)  # latest QA was for other content
    async with hub.client() as c:
        csrf = await hub.register(c, "oscar", "approver")
        res = await c.post("/api/jobs/batch-approve",
                           json={"items": [{"job_id": job, "content_hash": h}]},
                           headers={"x-csrf-token": csrf})  # fmt: skip
    assert res.json()["results"][0]["ok"] is False


# --------------------------------------------------------------------------- security checks


async def test_approval_needs_session_role_csrf_origin_and_post(
    hub: Hub, channel: Callable[..., str]
) -> None:
    job, h = hub.awaiting_job(channel())
    body = {"content_hash": h}
    async with hub.client() as anon:
        assert (await anon.post(f"/api/jobs/{job}/approve", json=body)).status_code == 401
    async with hub.client() as op:
        csrf = await hub.register(op, "olga", "operator")
        res = await op.post(f"/api/jobs/{job}/approve", json=body, headers={"x-csrf-token": csrf})
        assert res.status_code == 403  # operators cannot approve
    async with hub.client() as c:
        csrf = await hub.register(c, "jack", "approver")
        assert (await c.post(f"/api/jobs/{job}/approve", json=body)).status_code == 403  # no CSRF
        bad = await c.post(f"/api/jobs/{job}/approve", json=body,
                           headers={"x-csrf-token": "forged"})  # fmt: skip
        assert bad.status_code == 403
        evil = {"x-csrf-token": csrf, "Origin": "https://evil.example"}
        cross = await c.post(f"/api/jobs/{job}/approve", json=body, headers=evil)
        assert cross.status_code == 403
        assert (await c.get(f"/api/jobs/{job}/approve")).status_code == 405  # never via GET
        # A rotated CSRF token replaces the old one.
        rotated = (await c.post("/api/auth/csrf")).json()["csrf_token"]
        old = await c.post(f"/api/jobs/{job}/approve", json=body, headers={"x-csrf-token": csrf})
        assert old.status_code == 403
        ok = await c.post(f"/api/jobs/{job}/approve", json=body,
                          headers={"x-csrf-token": rotated})  # fmt: skip
        assert ok.status_code == 200
    assert hub.store.get_state(job) == JobState.APPROVED


async def test_kill_switches_are_operator_only_audited_and_notified(
    hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    async with hub.client() as ap:
        csrf = await hub.register(ap, "kate", "approver")
        res = await ap.post("/api/controls/global", json={"paused": True, "reason": "test"},
                            headers={"x-csrf-token": csrf})  # fmt: skip
        assert res.status_code == 403  # approvers cannot change policy
    async with hub.client() as op:
        csrf = await hub.register(op, "liam", "operator")
        hdr = {"x-csrf-token": csrf}
        assert (await op.post("/api/controls/global", json={"paused": True, "reason": "incident"},
                              headers=hdr)).status_code == 200  # fmt: skip
        assert (await op.get("/api/controls")).json()["global_paused"] is True
        await op.post("/api/controls/global", json={"paused": False}, headers=hdr)
        pause = {"paused": True, "reason": "strike"}
        res = await op.post(f"/api/controls/channels/{ch}", json=pause, headers=hdr)
        assert res.status_code == 200
        missing = await op.post("/api/controls/clusters/nope", json={"paused": True}, headers=hdr)
        assert missing.status_code == 404
    assert sql("SELECT status FROM channels WHERE id = %s", ch) == [("paused",)]
    assert _audit(sql, "killswitch.engaged", "global") >= 1
    assert _audit(sql, "killswitch.engaged", f"channel:{ch}") == 1
    assert [n.kind for n in hub.notifier.sent].count("kill_switch") == 3


async def test_admin_invites_users(hub: Hub) -> None:
    async with hub.client() as admin:
        csrf = await hub.register(admin, "root", "admin")
        res = await admin.post("/api/admin/invites",
                               json={"username": "newbie", "display_name": "N", "role": "approver"},
                               headers={"x-csrf-token": csrf})  # fmt: skip
        assert res.status_code == 200 and len(res.json()["invite_token"]) > 20
    async with hub.client() as ap:
        csrf = await hub.register(ap, "mia", "approver")
        res = await ap.post("/api/admin/invites",
                            json={"username": "x-user", "display_name": "X", "role": "admin"},
                            headers={"x-csrf-token": csrf})  # fmt: skip
        assert res.status_code == 403


def test_pending_notifications_are_deduplicated(hub: Hub, channel: Callable[..., str]) -> None:
    job, _ = hub.awaiting_job(channel())
    soon = datetime.now(UTC) + timedelta(hours=1)
    with hub.repo._tx("test") as conn:
        conn.execute("UPDATE video_jobs SET approval_deadline = %s WHERE id = %s", (soon, job))
    first = notify_pending(hub.repo, hub.notifier, now=datetime.now(UTC))
    second = notify_pending(hub.repo, hub.notifier, now=datetime.now(UTC))
    kinds = [n.kind for n in first]
    assert "items_waiting" in kinds and "deadline_approaching" in kinds
    assert second == []


async def test_approval_is_refused_with_423_while_a_kill_switch_is_engaged(
    hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    job, chash = hub.awaiting_job(ch, risk=5)
    sql("UPDATE channels SET status = 'paused' WHERE id = %s", ch)
    async with hub.client() as c:
        csrf = await hub.register(c, "paula", "approver")
        res = await c.post(f"/api/jobs/{job}/approve", json={"content_hash": chash},
                           headers={"x-csrf-token": csrf})  # fmt: skip
    assert res.status_code == 423, res.text
    assert hub.store.get_state(job) == JobState.AWAITING_APPROVAL
    assert sql("SELECT count(*) FROM approvals WHERE job_id = %s", job)[0][0] == 0
    assert hub.signaler.sent == []
