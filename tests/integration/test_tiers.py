"""Phase 6 exit gate: approval tiers promote and demote correctly, spot checks and disagreement
are reported, and no tier can bypass the human-issued token or the gateway check."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from tests.harness import make_service
from tests.integration.hubkit import Hub, build_hub
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.gateway.service import PublishRefused
from ytcore.store.errors import StoreError
from ytcore.workflow.states import JobState

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]


@pytest.fixture
def hub(database: str) -> Hub:
    return build_hub(database)


class SeqRng:
    def __init__(self, *values: float) -> None:
        self._values = list(values)

    def random(self) -> float:
        return self._values.pop(0)


def seed_history(
    hub: Hub, sql: SQL, ch: str, *, decisions: int = 25, days: float = 31,
    rejected: int = 0, edited: int = 0, risk: int = 10, spot: bool = False,
) -> None:  # fmt: skip
    """Backdated review outcomes for a channel, oldest `days` ago, spread evenly."""
    now = datetime.now(UTC)
    for i in range(decisions):
        job = hub.store.create_job(ch, "owner", actor="planner")
        decision = "rejected" if i < rejected else "edited" if i < rejected + edited else "approved"
        at = now - timedelta(days=days * (1 - i / max(decisions, 1)))
        sql(
            "INSERT INTO review_outcomes (job_id, channel_id, content_hash, decision, lane, "
            "spot_check, risk_score, flag_count, low_risk, tier, decided_by, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, %s, 'seed', %s)",
            job, ch, "sha256:" + "a" * 64, decision, "deep" if spot else "standard", spot, risk,
            risk <= 20, 2 if spot else 1, at,
        )  # fmt: skip


def tier(sql: SQL, ch: str) -> int:
    return int(sql("SELECT approval_tier FROM channels WHERE id = %s", ch)[0][0])


async def _login(hub: Hub, client: Any, name: str, role: str) -> dict[str, str]:
    csrf = await hub.register(client, name, role)
    return {"x-csrf-token": csrf}


# ------------------------------------------------------------------------------- promotion


async def test_promotion_is_a_recommendation_until_an_admin_confirms(
    hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    seed_history(hub, sql, ch, rejected=1, edited=2)
    recs = [r for r in hub.repo.tiers.evaluate_promotions() if r.channel_id == ch]
    assert len(recs) == 1 and recs[0].evidence["decisions"] == 25
    assert tier(sql, ch) == 1  # recommending never promotes
    assert not [r for r in hub.repo.tiers.evaluate_promotions() if r.channel_id == ch]  # one only
    with pytest.raises(psycopg.Error):  # nobody can set the tier directly
        sql("UPDATE channels SET approval_tier = 2 WHERE id = %s", ch)

    async with hub.client() as c:
        approver = await _login(hub, c, "tier-approver", "approver")
        url = f"/api/tiers/recommendations/{recs[0].id}/confirm"
        body = {"note": "reviewed the first month myself"}
        assert (await c.post(url, json=body, headers=approver)).status_code == 403
    assert tier(sql, ch) == 1
    async with hub.client() as c:
        admin = await _login(hub, c, "tier-boss", "admin")
        assert (await c.post(url, json={"note": "ok"}, headers=admin)).status_code == 422
        assert (await c.post(url, json=body, headers=admin)).status_code == 200
        changes = (await c.get("/api/tiers/changes", params={"channel": ch})).json()
        rows = (await c.get("/api/tiers")).json()
    assert tier(sql, ch) == 2
    assert [(x["kind"], x["to_tier"], x["actor"]) for x in changes] == [
        ("promotion", 2, "hub:tier-boss")
    ]
    assert changes[0]["reason"] == body["note"]
    assert next(r for r in rows if r["channel_id"] == ch)["tier"] == 2
    audit = sql("SELECT count(*) FROM audit_events WHERE action = 'tier.promoted' "
                "AND subject = %s", f"channel:{ch}")  # fmt: skip
    assert audit[0][0] == 1


@pytest.mark.parametrize(
    "history",
    [
        {"days": 10},  # too young
        {"decisions": 8},  # too few decisions
        {"rejected": 6},  # too many rejections
        {"risk": 70},  # risky content
    ],
)
def test_channels_without_a_clean_record_get_no_recommendation(
    history: dict[str, Any], hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    seed_history(hub, sql, ch, **history)
    assert not [r for r in hub.repo.tiers.evaluate_promotions() if r.channel_id == ch]


def test_a_recent_strike_or_policy_flag_blocks_a_recommendation(
    hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    struck, flagged = channel(), channel()
    for ch in (struck, flagged):
        seed_history(hub, sql, ch)
    hub.repo.tiers.record_strike(struck, "warning", "policy warning on video 12", video_ref="v12",
                                 actor="operator:test")  # fmt: skip
    job = hub.store.create_job(flagged, "owner", actor="planner")
    sql("INSERT INTO qa_reports (job_id, passed, risk_score, flags) VALUES (%s, false, 90, %s)",
        job, json.dumps([{"code": "banned_topic", "severity": "block"}]))  # fmt: skip
    made = {r.channel_id for r in hub.repo.tiers.evaluate_promotions()}
    assert struck not in made and flagged not in made


# ------------------------------------------------------------------------------- demotion


async def test_strike_demotes_and_a_queued_fast_lane_video_loses_its_fast_lane(
    hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    hub.promote(ch)
    job, chash = hub.awaiting_job(ch, risk=5)
    assert hub.repo.is_fast_lane(job)
    async with hub.client() as c:
        admin = await _login(hub, c, "strike-boss", "admin")
        queued = (await c.get("/api/jobs", params={"channel": ch})).json()[0]
        assert (queued["lane"], queued["fast_lane"]) == ("fast", True)
        strike = {"kind": "strike", "note": "community guidelines strike on video 3"}
        res = await c.post(f"/api/channels/{ch}/strikes", json=strike, headers=admin)
        assert res.status_code == 200
        queued = (await c.get("/api/jobs", params={"channel": ch})).json()[0]
        assert (queued["lane"], queued["fast_lane"]) == ("standard", False)
    async with hub.client() as c:
        approver = await _login(hub, c, "strike-approver", "approver")
        batch = await c.post("/api/jobs/batch-approve", headers=approver,
                             json={"items": [{"job_id": job, "content_hash": chash}]})  # fmt: skip
        result = batch.json()["results"][0]
    assert result["ok"] is False and "fast lane" in result["error"]
    assert hub.store.get_state(job) == JobState.AWAITING_APPROVAL
    assert sql("SELECT count(*) FROM approvals WHERE job_id = %s", job)[0][0] == 0
    assert tier(sql, ch) == 1
    reason = sql("SELECT reason FROM tier_changes WHERE channel_id = %s AND kind = 'demotion'", ch)
    assert reason[0][0].startswith("strike recorded")
    assert sql("SELECT count(*) FROM alerts WHERE channel_id = %s AND kind = 'tier.demoted'",
               ch)[0][0] == 1  # fmt: skip


async def test_a_batch_cannot_use_a_fast_lane_the_channel_lost_in_between(
    hub: Hub, channel: Callable[..., str], sql: SQL, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the pre-check passed a moment ago, the decision itself re-checks the lane."""
    ch = channel()
    hub.promote(ch)
    job, chash = hub.awaiting_job(ch, risk=5)
    assert hub.repo.is_fast_lane(job)
    hub.repo.tiers.demote(ch, "demoted between the check and the decision", {})
    monkeypatch.setattr(hub.repo, "is_fast_lane", lambda _job: True)  # the stale pre-check
    async with hub.client() as c:
        csrf = await _login(hub, c, "late-approver", "approver")
        res = await c.post("/api/jobs/batch-approve", headers=csrf,
                           json={"items": [{"job_id": job, "content_hash": chash}]})  # fmt: skip
    result = res.json()["results"][0]
    assert result["ok"] is False and "no longer in the fast lane" in result["error"]
    assert sql("SELECT count(*) FROM approvals WHERE job_id = %s", job)[0][0] == 0


async def test_a_rejection_spike_demotes_during_review(
    hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    hub.promote(ch)
    quick, _ = hub.awaiting_job(ch, risk=5)
    risky = [hub.awaiting_job(ch, risk=60)[0] for _ in range(3)]
    async with hub.client() as c:
        csrf = await _login(hub, c, "spike-approver", "approver")
        for i, job in enumerate(risky):
            head = hub.repo.head(job)
            assert head is not None
            res = await c.post(
                f"/api/jobs/{job}/reject", headers=csrf,
                json={"content_hash": head.content_hash, "reason": "claims are unsupported",
                      "rework_stage": "script"},
            )  # fmt: skip
            assert res.status_code == 200
            assert tier(sql, ch) == (2 if i < 2 else 1)
        queued = {q["job_id"]: q for q in (await c.get("/api/jobs", params={"channel": ch})).json()}
    assert queued[quick]["lane"] == "standard"
    reason = sql("SELECT reason FROM tier_changes WHERE channel_id = %s AND kind = 'demotion'", ch)
    assert "rejection spike" in reason[0][0]


def test_a_policy_flag_demotes_on_evaluation(
    hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    hub.promote(ch)
    job, _ = hub.awaiting_job(ch, risk=5)
    sql("INSERT INTO qa_reports (job_id, passed, risk_score, flags) VALUES (%s, false, 95, %s)",
        job, json.dumps([{"code": "duplicate_script", "severity": "block"}]))  # fmt: skip
    demoted = {d.channel_id: d.reason for d in hub.repo.tiers.evaluate_demotions()}
    assert "policy flag" in demoted[ch] and tier(sql, ch) == 1
    assert not hub.repo.tiers.evaluate_demotions()  # already back on approve-all


# ------------------------------------------------------------------------------- spot checks


async def test_spot_checks_send_low_risk_videos_to_deep_review_and_are_remembered(
    hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    hub.promote(ch)
    sql("UPDATE tier_rules SET rules = %s", json.dumps({"spot_check_percent": 50}))
    hub.repo.tiers._rng = SeqRng(0.1, 0.9)  # first draw is a spot check, second is not
    checked, chash = hub.awaiting_job(ch, risk=5)
    passed, _ = hub.awaiting_job(ch, risk=5)
    lanes = {q.job_id: q for q in hub.repo.queue(channel=ch)}
    assert (lanes[checked].lane, lanes[passed].lane) == ("deep", "fast")
    assert "spot check" in lanes[checked].lane_reason
    # the draw is stored: asking again (or changing the percentage) does not re-roll it
    sql("UPDATE tier_rules SET rules = %s", json.dumps({"spot_check_percent": 0}))
    assert hub.repo.lane_for(checked).lane == "deep"  # type: ignore[union-attr]
    assert hub.repo.lane_for(passed).lane == "fast"  # type: ignore[union-attr]
    async with hub.client() as c:
        csrf = await _login(hub, c, "spot-approver", "approver")
        assert (await c.post(f"/api/jobs/{checked}/approve", headers=csrf,
                             json={"content_hash": chash})).status_code == 200  # fmt: skip
    row = sql("SELECT decision, lane, spot_check, low_risk FROM review_outcomes WHERE job_id = %s",
              checked)  # fmt: skip
    assert row == [("approved", "deep", True, True)]


def test_disagreement_between_risk_score_and_humans_is_reported(
    hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    seed_history(hub, sql, ch, decisions=4, days=3, rejected=1, spot=True)
    seed_history(hub, sql, ch, decisions=6, days=3, risk=70)
    report = hub.repo.tiers.disagreement(ch)
    assert report["spot_checks"] == {"n": 4, "disagreements": 1, "rate": 0.25}
    assert report["low_risk_all"]["n"] == 4
    assert report["by_risk_bucket"]["0-19"] == {"n": 4, "not_approved": 1}
    assert report["by_risk_bucket"]["60-79"] == {"n": 6, "not_approved": 0}


# ------------------------------------------------------------------- no new publish path


def test_no_tier_opens_the_gateway_without_a_human_issued_token(
    hub: Hub, channel: Callable[..., str], sql: SQL, secrets: Any
) -> None:
    ch = channel()
    hub.promote(ch)
    client = FakeYouTubeClient()
    service = make_service(hub.store, hub.keys, hub.assets, client, secrets)
    job, chash = hub.awaiting_job(ch, risk=1)  # the lowest possible risk, a fast-lane video
    assert hub.repo.is_fast_lane(job)
    unrecorded = hub.keys.token(job, ch, chash)  # validly signed, but no human approved it
    with pytest.raises(PublishRefused):
        service.publish(job, unrecorded)
    with pytest.raises(PublishRefused):
        service.publish(job, "no-token")
    with pytest.raises(StoreError):  # and the state machine refuses to skip the approval
        hub.store.transition(job, JobState.APPROVED, actor="attacker")
    assert client.uploads == []
    assert sql("SELECT count(*) FROM uploads WHERE job_id = %s", job)[0][0] == 0
    assert sql("SELECT count(*) FROM approvals WHERE job_id = %s", job)[0][0] == 0


# ------------------------------------------------------------------------------ simulation


async def test_simulated_channels_promote_and_demote_correctly(
    hub: Hub, channel: Callable[..., str], sql: SQL
) -> None:
    steady, noisy, young, spiky = channel(), channel(), channel(), channel()
    seed_history(hub, sql, steady, rejected=1)
    seed_history(hub, sql, noisy, rejected=8)
    seed_history(hub, sql, young, days=9)
    seed_history(hub, sql, spiky, rejected=1)
    tiers = hub.repo.tiers
    recommended = {r.channel_id: r for r in tiers.evaluate_promotions()}
    assert {steady, spiky} <= set(recommended) and not {noisy, young} & set(recommended)

    async with hub.client() as c:
        admin = await _login(hub, c, "sim-boss", "admin")
        for ch in (steady, spiky):
            url = f"/api/tiers/recommendations/{recommended[ch].id}/confirm"
            note = {"note": f"promoted {ch} after my own review"}
            res = await c.post(url, json=note, headers=admin)
            assert res.status_code == 200
    tiers_now = (tier(sql, steady), tier(sql, spiky), tier(sql, noisy), tier(sql, young))
    assert tiers_now == (2, 2, 1, 1)

    # an incident on one channel, a run of rejections on another
    tiers.record_strike(steady, "strike", "strike on the video from day 12", video_ref=None,
                        actor="operator:sim")  # fmt: skip
    seed_history(hub, sql, spiky, decisions=3, days=0.5, rejected=3)
    demoted = {d.channel_id for d in tiers.evaluate_demotions()}
    assert demoted == {spiky}  # the struck channel was already demoted by the strike itself
    assert (tier(sql, steady), tier(sql, spiky)) == (1, 1)
    for ch in (steady, spiky):
        kinds = [r[0] for r in sql("SELECT kind FROM tier_changes WHERE channel_id = %s "
                                   "ORDER BY id", ch)]  # fmt: skip
        assert kinds == ["promotion", "demotion"]
        audited = sql(
            "SELECT count(*) FROM audit_events WHERE subject = %s "
            "AND action IN ('tier.promoted', 'tier.demoted')",
            f"channel:{ch}",
        )
        assert audited[0][0] == 2
    assert sql("SELECT audit_chain_first_break()")[0][0] is None
