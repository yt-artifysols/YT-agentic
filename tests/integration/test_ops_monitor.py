"""Stuck-job, budget, quota and gateway alerts plus the metrics snapshot on real Postgres."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from tests.harness import Keys, drive_to_awaiting
from ytcore.gateway.assets import InMemoryAssetSource
from ytcore.observability.metrics import collect, render_prometheus
from ytcore.observability.monitor import MonitorThresholds, OpsMonitor
from ytcore.store.postgres import PostgresStore

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]


@pytest.fixture
def monitor(database: str) -> OpsMonitor:
    # plan.missing depends on the wall-clock hour; it has its own test.
    return OpsMonitor(
        lambda: psycopg.connect(database, autocommit=True), MonitorThresholds(plan_due_hour_utc=24)
    )


def _mine(findings: list[Any], channel: str) -> list[Any]:
    return [f for f in findings if f.channel_id == channel]


def test_a_job_idle_in_a_working_state_is_reported_once(
    monitor: OpsMonitor, store: PostgresStore, channel: Callable[..., str]
) -> None:
    ch = channel()
    job = store.create_job(ch, "planner", actor="planner")
    later = datetime.now(UTC) + timedelta(hours=2)
    found = _mine(monitor.check(later), ch)
    assert [f.kind for f in found] == ["job.stuck"] and found[0].job_id == job
    assert _mine(monitor.check(later), ch) == []  # de-duplicated: one page, not one per minute


def test_fresh_jobs_are_not_stuck(
    monitor: OpsMonitor, store: PostgresStore, channel: Callable[..., str]
) -> None:
    ch = channel()
    store.create_job(ch, "planner", actor="planner")
    assert _mine(monitor.findings(datetime.now(UTC)), ch) == []


def test_awaiting_approval_past_its_deadline_is_stuck_but_waiting_is_not(
    monitor: OpsMonitor, store: PostgresStore, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    assets = InMemoryAssetSource()
    waiting = store.create_job(ch, "planner", actor="planner")
    drive_to_awaiting(store, assets, waiting, ch)
    sql("UPDATE video_jobs SET approval_deadline = now() + interval '1 day' WHERE id = %s", waiting)
    assert _mine(monitor.findings(datetime.now(UTC)), ch) == []
    sql(
        "UPDATE video_jobs SET approval_deadline = now() - interval '2 hours' WHERE id = %s",
        waiting,
    )
    kinds = [f.kind for f in _mine(monitor.findings(datetime.now(UTC)), ch)]
    assert kinds == ["job.stuck"]


def test_budget_warning_breach_and_quota_alerts(
    monitor: OpsMonitor, store: PostgresStore, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel(budget=100, quota=1000)
    job = store.create_job(ch, "planner", actor="planner")
    store.record_cost(job, "render", 95, actor="renderer")
    kinds = {f.kind for f in _mine(monitor.findings(), ch)}
    assert "budget.warning" in kinds
    store.record_cost(job, "render", 5, actor="renderer")
    assert "budget.breach" in {f.kind for f in _mine(monitor.findings(), ch)}
    project = sql("SELECT google_project_id FROM channels WHERE id = %s", ch)[0][0]
    sql("INSERT INTO quota_ledger (google_project_id, units, operation) VALUES (%s, 900, 'x')",
        project)  # fmt: skip
    assert any(f.kind == "quota.high" and project in f.subject for f in monitor.findings())


def test_failed_upload_raises_a_gateway_alert(
    monitor: OpsMonitor, store: PostgresStore, keys: Keys, channel: Callable[..., str], sql: SQL
) -> None:
    from tests.harness import make_service
    from ytcore.gateway.clients import FakeYouTubeClient
    from ytcore.workflow.states import JobState

    ch = channel()
    assets, client = InMemoryAssetSource(), FakeYouTubeClient()
    job = store.create_job(ch, "planner", actor="planner")
    chash = drive_to_awaiting(store, assets, job, ch)
    token = keys.token(job, ch, chash)
    store.verify_approval(job, token, actor="approval-hub")
    store.transition(job, JobState.SCHEDULED, actor="scheduler")
    client.fail_uploads = True
    from tests.fakes import fake_secrets

    service = make_service(store, keys, assets, client, fake_secrets())
    with pytest.raises(Exception):  # noqa: B017 - UploadFailed
        service.publish(job, token)
    found = _mine(monitor.findings(), ch)
    assert any(f.kind == "gateway.failure" and f.job_id == job for f in found)


def test_missing_plan_is_reported_after_the_planner_deadline(
    database: str, channel: Callable[..., str]
) -> None:
    monitor = OpsMonitor(lambda: psycopg.connect(database, autocommit=True))
    ch = channel()
    evening = datetime.now(UTC).replace(hour=21, minute=0)
    assert any(f.kind == "plan.missing" for f in _mine(monitor.findings(evening), ch))
    morning = evening.replace(hour=6)
    assert not any(f.kind == "plan.missing" for f in _mine(monitor.findings(morning), ch))


def test_metrics_snapshot_reflects_the_database(
    database: str, store: PostgresStore, channel: Callable[..., str]
) -> None:
    ch = channel()
    job = store.create_job(ch, "planner", actor="planner")
    store.record_cost(job, "render", 40, actor="renderer")
    snap = collect(lambda: psycopg.connect(database, autocommit=True))
    assert snap.queue_depth["planned"] >= 1
    assert snap.cost_per_video_cents[ch] == 40.0
    assert "ytcore_cost_per_video_cents" in render_prometheus(snap)


def test_thresholds_are_configurable(
    database: str, store: PostgresStore, channel: Callable[..., str]
) -> None:
    ch = channel()
    store.create_job(ch, "planner", actor="planner")
    strict = OpsMonitor(
        lambda: psycopg.connect(database, autocommit=True),
        MonitorThresholds(working_state_minutes=1, plan_due_hour_utc=24),
    )
    assert _mine(strict.findings(datetime.now(UTC) + timedelta(minutes=5)), ch)
