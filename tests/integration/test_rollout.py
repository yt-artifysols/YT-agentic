"""Rollout: cluster B cannot go live until cluster A met its gate and an admin signed it off."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest

from tests.integration.conftest import fresh_database
from tests.integration.hubkit import Hub, build_hub
from ytcore.channels.rollout import GateNotMet, RolloutService
from ytcore.store.errors import RolloutRefused

pytestmark = pytest.mark.integration


@pytest.fixture
def db() -> Iterator[str]:
    with fresh_database() as conninfo:
        yield conninfo


def _sql(db: str, query: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(db, autocommit=True) as conn:
        cur = conn.execute(query, params or None)
        return cur.fetchall() if cur.description else []


def _fleet(db: str) -> RolloutService:
    _sql(db, "INSERT INTO clusters (id, display_name, live_enabled) VALUES ('ca', 'A', true)")
    _sql(db, "INSERT INTO clusters (id, display_name, requires_cluster, live_enabled) "
             "VALUES ('cb', 'B', 'ca', false)")  # fmt: skip
    _sql(db, "INSERT INTO channels (id, display_name, storage_prefix, memory_namespace, cluster_id)"
             " VALUES ('ca-1', 'A1', 'channels/ca-1/', 'mem:ca-1', 'ca')")  # fmt: skip
    return RolloutService(lambda: psycopg.connect(db, autocommit=True))


def _backdate_live(db: str, days: int) -> None:
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute("UPDATE clusters SET live_since = now() - make_interval(days => %s) "
                     "WHERE id = 'ca'", (days,))  # fmt: skip


def _scorecards(db: str, days: int) -> None:
    for d in range(days):
        _sql(
            db,
            "INSERT INTO daily_scorecards (channel_id, score_date, jobs_planned, jobs_approved, "
            "jobs_rejected, policy_flags, videos_published, cost_cents, views) "
            "VALUES ('ca-1', %s, 1, 1, 0, 0, 1, 100, 10)",
            date.today() - timedelta(days=d),
        )


def _admin(db: str) -> None:
    _sql(
        db, "INSERT INTO hub_users (username, display_name, role) VALUES ('boss', 'Boss', 'admin')"
    )


def test_a_young_cluster_does_not_meet_the_gate(db: str) -> None:
    svc = _fleet(db)
    _backdate_live(db, 3)
    report = svc.gate("ca")
    assert not report.ok and any("3.0 of 14" in p or "of 14 required" in p for p in report.problems)
    _admin(db)
    with pytest.raises(GateNotMet):
        svc.sign_off("ca", "boss", "Reviewed three days of cluster A, all good so far.")


def test_a_stable_cluster_passes_and_a_human_admin_unlocks_the_next(db: str) -> None:
    svc = _fleet(db)
    _admin(db)
    _backdate_live(db, 16)
    _scorecards(db, 16)
    report = svc.gate("ca")
    assert report.ok, report.problems

    with pytest.raises(RolloutRefused):  # no sign-off yet
        svc.enable("cb", "boss")
    sid = svc.sign_off("ca", "boss", "Reviewed the first two weeks of cluster A in detail: stable.")
    assert sid > 0
    svc.enable("cb", "boss")
    assert _sql(
        db, "SELECT live_enabled, live_since IS NOT NULL FROM clusters WHERE id = 'cb'"
    ) == [(True, True)]
    ev = _sql(db, "SELECT evidence ->> 'ok' FROM rollout_signoffs ORDER BY id DESC LIMIT 1")
    assert ev == [("true",)]


@pytest.mark.parametrize(
    "setup, needle",
    [
        (
            "INSERT INTO breaker_trips (cluster_id, reasons) VALUES ('ca', '[{\"code\": \"x\"}]')",
            "breaker",
        ),
        (
            "INSERT INTO alerts (channel_id, kind, message) VALUES ('ca-1', 'job.stuck', 'stuck')",
            "alert",
        ),
    ],
)
def test_trips_and_alerts_during_the_period_block_the_gate(
    db: str, setup: str, needle: str
) -> None:
    svc = _fleet(db)
    _backdate_live(db, 16)
    _scorecards(db, 16)
    _sql(db, setup)
    report = svc.gate("ca")
    assert not report.ok and any(needle in p for p in report.problems)


def test_missing_scorecards_mean_the_analytics_job_is_not_running(db: str) -> None:
    svc = _fleet(db)
    _backdate_live(db, 16)
    report = svc.gate("ca")
    assert any("scorecards" in p for p in report.problems)


def test_a_dark_cluster_reports_that_it_is_not_live(db: str) -> None:
    svc = _fleet(db)
    assert any("not live" in p for p in svc.gate("cb").problems)
    with pytest.raises(ValueError):
        svc.gate("nope")


# ------------------------------------------------------------------ through the hub


async def test_only_admins_sign_off_and_enable_and_everyone_can_read_clusters(db: str) -> None:
    _fleet(db)
    _backdate_live(db, 16)
    _scorecards(db, 16)
    hub: Hub = build_hub(db)
    async with hub.client() as admin, hub.client() as approver:
        a_csrf = await hub.register(admin, "ada", "admin")
        p_csrf = await hub.register(approver, "pat", "approver")

        clusters = (await approver.get("/api/clusters")).json()
        assert {c["id"] for c in clusters} == {"ca", "cb"}
        assert next(c for c in clusters if c["id"] == "cb")["requires_cluster"] == "ca"
        cards = (await approver.get("/api/scorecards", params={"cluster": "ca"})).json()
        assert len(cards) == 14 or len(cards) >= 10

        # approvers cannot even read the gate; nobody but an admin can sign off or enable
        assert (await approver.get("/api/rollout/ca/gate")).status_code == 403
        note = {"note": "Reviewed two weeks of cluster A, everything is stable."}
        denied = await approver.post("/api/rollout/ca/signoff", json=note,
                                     headers={"x-csrf-token": p_csrf})  # fmt: skip
        assert denied.status_code == 403
        assert (await admin.post("/api/rollout/cb/enable",
                                 headers={"x-csrf-token": a_csrf})).status_code == 409  # fmt: skip

        gate = (await admin.get("/api/rollout/ca/gate")).json()
        assert gate["ok"] is True
        short = await admin.post("/api/rollout/ca/signoff", json={"note": "ok"},
                                 headers={"x-csrf-token": a_csrf})  # fmt: skip
        assert short.status_code == 422
        done = await admin.post("/api/rollout/ca/signoff", json=note,
                                headers={"x-csrf-token": a_csrf})  # fmt: skip
        assert done.status_code == 200, done.text
        on = await admin.post("/api/rollout/cb/enable", headers={"x-csrf-token": a_csrf})
        assert on.status_code == 200, on.text
    assert _sql(db, "SELECT live_enabled FROM clusters WHERE id = 'cb'") == [(True,)]
    enabled = _sql(db, "SELECT count(*) FROM audit_events WHERE action = 'rollout.enabled' "
                   "AND subject = 'cluster:cb'")  # fmt: skip
    assert enabled[0][0] == 1


async def test_queue_sorting_and_paging(db: str, channel: Callable[..., str] | None = None) -> None:
    _fleet(db)
    hub: Hub = build_hub(db)
    async with hub.client() as c:
        await hub.register(c, "ada", "admin")
        assert (
            await c.get("/api/jobs", params={"sort": "deadline", "limit": 5})
        ).status_code == 200
        assert (await c.get("/api/jobs", params={"sort": "nonsense"})).status_code == 422
        assert (await c.get("/api/jobs", params={"limit": 0})).status_code == 422
    assert datetime.now(UTC)
