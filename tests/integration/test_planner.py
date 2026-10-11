"""Daily planner against real Postgres: idempotency, kill switches, replans, healing."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from ytcore.planner.daily import DailyPlanner, FakeWorkflowStarter, PlanRepository

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]
NOW = datetime(2026, 5, 4, 6, 0, tzinfo=UTC)


@pytest.fixture
def repo(database: str) -> PlanRepository:
    return PlanRepository.from_conninfo(database)


def _planner(repo: PlanRepository) -> tuple[DailyPlanner, FakeWorkflowStarter]:
    starter = FakeWorkflowStarter()
    return DailyPlanner(repo, starter), starter


def _mine(run_items: list[Any], channels: set[str]) -> list[Any]:
    return [p for p in run_items if p.channel_id in channels]


def test_one_job_per_channel_per_day_and_rerun_never_duplicates(
    repo: PlanRepository, channel: Callable[..., str], sql: SQL
) -> None:
    chans = {channel(), channel()}
    planner, starter = _planner(repo)
    first = planner.run(NOW)
    again = planner.run(NOW + timedelta(hours=3))  # same channel-local day
    mine = _mine(first.planned, chans)
    assert len(mine) == 2 and all(p.created for p in mine)
    assert not any(p.created for p in _mine(again.planned, chans))
    for ch in chans:
        assert sql("SELECT count(*) FROM daily_plans WHERE channel_id = %s", ch)[0][0] == 1
        assert sql("SELECT count(*) FROM video_jobs WHERE channel_id = %s", ch)[0][0] == 1
    assert len([s for s in starter.started if s[1] in chans]) == 2  # workflow started once each


def test_concurrent_planners_create_one_job(
    repo: PlanRepository, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    with ThreadPoolExecutor(8) as pool:
        runs = list(pool.map(lambda _: _planner(repo)[0].run(NOW), range(8)))
    assert sum(1 for r in runs for p in r.planned if p.channel_id == ch and p.created) == 1
    assert sql("SELECT count(*) FROM video_jobs WHERE channel_id = %s", ch)[0][0] == 1


def test_slots_follow_timezone_and_are_staggered_in_a_cluster(
    repo: PlanRepository, channel: Callable[..., str], sql: SQL
) -> None:
    a, b = channel(), channel()
    cluster = sql("SELECT cluster_id FROM channels WHERE id = %s", a)[0][0]
    sql("UPDATE channels SET cluster_id = %s WHERE id = %s", cluster, b)
    sql("UPDATE channels SET audience_timezone = 'Asia/Tokyo', publish_hour_local = 9 "
        "WHERE id IN (%s, %s)", a, b)  # fmt: skip
    sql("UPDATE clusters SET slot_spacing_minutes = 45 WHERE id = %s", cluster)
    run = _planner(repo)[0].run(NOW)
    slots = sorted(p.slot.slot_at for p in _mine(run.planned, {a, b}))
    assert slots[1] - slots[0] == timedelta(minutes=45)
    assert slots[0] == datetime(2026, 5, 5, 0, 0, tzinfo=UTC)  # 09:00 JST on the 5th


def test_paused_channel_cluster_and_global_switch_plan_nothing(
    repo: PlanRepository, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    planner, _ = _planner(repo)
    sql("UPDATE channels SET status = 'paused' WHERE id = %s", ch)
    run = planner.run(NOW)
    assert any(s.channel_id == ch and "paused" in s.reason for s in run.skipped)
    sql("UPDATE channels SET status = 'active' WHERE id = %s", ch)
    sql("UPDATE clusters SET status = 'paused' WHERE id = (SELECT cluster_id FROM channels "
        "WHERE id = %s)", ch)  # fmt: skip
    assert any(s.channel_id == ch for s in planner.run(NOW).skipped)
    sql("UPDATE clusters SET status = 'active'")
    sql("UPDATE global_controls SET publishing_paused = true")
    try:
        assert any(s.channel_id == ch for s in planner.run(NOW).skipped)
    finally:
        sql("UPDATE global_controls SET publishing_paused = false")
    assert sql("SELECT count(*) FROM daily_plans WHERE channel_id = %s", ch)[0][0] == 0


def test_shadow_channels_are_not_planned(
    repo: PlanRepository, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel(autonomy_tier="shadow")
    run = _planner(repo)[0].run(NOW)
    assert any(s.channel_id == ch and "shadow" in s.reason for s in run.skipped)


def test_missed_deadline_is_replanned_on_the_next_free_day_and_capped(
    repo: PlanRepository, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    planner, starter = _planner(repo)
    plan = _mine(planner.run(NOW).planned, {ch})[0]
    # The workflow cancelled the job at its approval deadline and asked for a replan.
    sql("UPDATE video_jobs SET state = 'cancelled' WHERE id = %s", plan.job_id)
    assert repo.request_replan(plan.job_id, "approval deadline missed")
    assert not repo.request_replan(plan.job_id, "again")  # idempotent: already requested
    run = planner.run(NOW + timedelta(hours=1))
    replans = _mine(run.replanned, {ch})
    assert len(replans) == 1 and replans[0].attempt == 2
    assert replans[0].slot.slot_date > plan.slot.slot_date  # a later free day
    assert sql("SELECT status FROM daily_plans WHERE id = %s", plan.plan_id) == [("replanned",)]
    assert planner.run(NOW + timedelta(hours=2)).replanned == []  # re-run: nothing more
    assert len([s for s in starter.started if s[1] == ch]) == 2


def test_replans_stop_after_the_cap_and_alert_an_operator(
    repo: PlanRepository, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    planner, _ = _planner(repo)
    now = NOW
    plan = _mine(planner.run(now).planned, {ch})[0]
    for _ in range(6):
        sql("UPDATE video_jobs SET state = 'cancelled' WHERE id = %s", plan.job_id)
        repo.request_replan(plan.job_id, "approval deadline missed")
        now += timedelta(hours=1)
        run = planner.run(now)
        nxt = _mine(run.replanned, {ch})
        if not nxt:
            break
        plan = nxt[0]
    assert sql("SELECT count(*) FROM alerts WHERE channel_id = %s AND kind = 'plan.exhausted'",
               ch)[0][0] == 1  # fmt: skip
    assert sql("SELECT max(attempt) FROM daily_plans WHERE channel_id = %s", ch)[0][0] == 4


def test_non_cancelled_jobs_are_never_replanned(
    repo: PlanRepository, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    planner, _ = _planner(repo)
    plan = _mine(planner.run(NOW).planned, {ch})[0]
    assert not repo.request_replan(plan.job_id, "nope")  # still 'planned'
    sql("UPDATE video_jobs SET state = 'failed' WHERE id = %s", plan.job_id)
    assert not repo.request_replan(plan.job_id, "failed jobs are an operator matter")


def test_plans_are_audited_immutable_and_append_only(
    repo: PlanRepository, channel: Callable[..., str], sql: SQL
) -> None:
    import psycopg

    ch = channel()
    plan = _mine(_planner(repo)[0].run(NOW).planned, {ch})[0]
    assert sql("SELECT count(*) FROM audit_events WHERE action = 'plan.created' AND subject = %s",
               f"job:{plan.job_id}")[0][0] == 1  # fmt: skip
    with pytest.raises(psycopg.Error):
        sql("UPDATE daily_plans SET slot_at = slot_at + interval '1 day' WHERE id = %s",
            plan.plan_id)  # fmt: skip
    with pytest.raises(psycopg.Error):
        sql("DELETE FROM daily_plans WHERE id = %s", plan.plan_id)
    assert plan.plan_date == date(2026, 5, 5)


def test_unstarted_plans_are_healed_on_the_next_run(
    repo: PlanRepository, channel: Callable[..., str]
) -> None:
    ch = channel()

    class Flaky(FakeWorkflowStarter):
        calls = 0

        def start(
            self,
            job_id: str,
            channel_id: str,
            approval_timeout_seconds: int,
            approval_deadline: Any = None,
            cluster_id: str | None = None,
        ) -> None:
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise RuntimeError("temporal down")
            super().start(
                job_id, channel_id, approval_timeout_seconds, approval_deadline, cluster_id
            )

    starter = Flaky()
    planner = DailyPlanner(repo, starter)
    with pytest.raises(RuntimeError):
        planner.run(NOW)
    planner.run(NOW)  # plan exists, workflow not started: this run starts it
    assert any(s[1] == ch for s in starter.started)
