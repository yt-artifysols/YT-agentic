"""Analytics ingestion, learnings into per-channel memory and daily scorecards (real Postgres)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest

from tests.fakes import fake_secrets
from tests.harness import Keys, drive_to_awaiting, make_service
from ytcore.agents.memory import PostgresMemory
from ytcore.analytics.client import FakeAnalyticsClient, VideoMetricsRow
from ytcore.analytics.ingest import AnalyticsIngestor
from ytcore.gateway.assets import InMemoryAssetSource
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.secrets.provider import InMemorySecretsProvider
from ytcore.store.postgres import PostgresStore
from ytcore.workflow.states import JobState

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]
DAY = datetime.now(UTC).date()  # videos are published 'now' in these tests
REF = "env://YT_ANALYTICS_TOKEN"


class Published:
    def __init__(self, store: PostgresStore, keys: Keys, sql: SQL) -> None:
        self.store, self.keys, self.sql = store, keys, sql
        self.assets, self.client = InMemoryAssetSource(), FakeYouTubeClient()
        self.service = make_service(store, keys, self.assets, self.client, fake_secrets())

    def video(self, channel: str, title: str) -> tuple[str, str]:
        job = self.store.create_job(channel, "planner", actor="planner")
        chash = drive_to_awaiting(self.store, self.assets, job, channel)
        token = self.keys.token(job, channel, chash)
        self.store.verify_approval(job, token, actor="approval-hub")
        self.store.transition(job, JobState.SCHEDULED, actor="scheduler")
        result = self.service.publish(job, token)
        return job, result.video_id


@pytest.fixture
def pub(store: PostgresStore, keys: Keys, sql: SQL) -> Published:
    return Published(store, keys, sql)


def _ingestor(database: str, client: FakeAnalyticsClient) -> AnalyticsIngestor:
    secrets = InMemorySecretsProvider({REF: "analytics-token-dummy"})
    return AnalyticsIngestor(
        lambda: psycopg.connect(database, autocommit=True),
        client,
        secrets,
        PostgresMemory(lambda: psycopg.connect(database, autocommit=True)),
    )


def _row(video: str, views: int, ctr: float, retention: float, day: date = DAY) -> VideoMetricsRow:
    return VideoMetricsRow(video, day, views, views * 10, ctr, 120.0, retention, 250)


def test_ingestion_is_idempotent_restates_and_isolates_channels(
    database: str, pub: Published, channel: Callable[..., str], sql: SQL
) -> None:
    a, b, no_token = channel(), channel(), channel()
    sql("UPDATE channels SET analytics_credentials_ref = %s WHERE id IN (%s, %s)", REF, a, b)
    _, va = pub.video(a, "A")
    _, vb = pub.video(b, "B")
    pub.video(no_token, "C")  # no analytics token: never queried
    client = FakeAnalyticsClient({va: {DAY: _row(va, 500, 0.05, 0.4)},
                                  vb: {DAY: _row(vb, 300, 0.04, 0.3)}})  # fmt: skip
    ing = _ingestor(database, client)

    report = ing.ingest(DAY)
    assert report.ingested == 2 and not report.failed_channels
    assert client.calls == 2
    ing.ingest(DAY)  # re-run: restates, never duplicates
    assert sql("SELECT count(*) FROM video_metrics WHERE channel_id IN (%s, %s)", a, b)[0][0] == 2
    client.data[va][DAY] = _row(va, 650, 0.05, 0.4)
    ing.ingest(DAY)
    assert sql("SELECT views FROM video_metrics WHERE youtube_video_id = %s", va) == [(650,)]

    # One channel's failure (bad credentials) is reported and does not stop the others.
    class Boom(FakeAnalyticsClient):
        def fetch(self, *, token: Any, video_id: str, day: date) -> Any:
            if video_id == va:
                raise RuntimeError("quota")
            return super().fetch(token=token, video_id=video_id, day=day)

    boom = Boom(client.data)
    report = _ingestor(database, boom).ingest(DAY)
    assert report.failed_channels == {a: "RuntimeError"} and report.ingested == 1


def test_learnings_go_to_the_channels_own_memory_once_per_week(
    database: str, pub: Published, channel: Callable[..., str], sql: SQL
) -> None:
    ch, other = channel(), channel()
    sql("UPDATE channels SET analytics_credentials_ref = %s WHERE id IN (%s, %s)", REF, ch, other)
    perf = [(0.09, 0.6), (0.04, 0.4), (0.04, 0.4), (0.01, 0.1)]  # strong, typical x2, weak
    data: dict[str, dict[date, VideoMetricsRow]] = {}
    for i, (ctr, ret) in enumerate(perf):
        _, vid = pub.video(ch, f"v{i}")
        data[vid] = {DAY: _row(vid, 400, ctr, ret)}
    client = FakeAnalyticsClient(data)
    ing = _ingestor(database, client)
    ing.ingest(DAY)

    today = DAY + timedelta(days=1)
    assert ing.learn(today) == 2  # one above, one below; typical videos teach nothing
    assert ing.learn(today) == 0  # idempotent within the ISO week
    notes = sql("SELECT channel_id, kind, content FROM channel_memory WHERE kind = 'performance'")
    assert {n[0] for n in notes} == {ch}  # never the other channel's memory
    assert any("above" in n[2] for n in notes) and any("below" in n[2] for n in notes)
    assert all("sk-" not in n[2] for n in notes)


def test_too_little_history_teaches_nothing(
    database: str, pub: Published, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    sql("UPDATE channels SET analytics_credentials_ref = %s WHERE id = %s", REF, ch)
    _, vid = pub.video(ch, "only")
    client = FakeAnalyticsClient({vid: {DAY: _row(vid, 900, 0.2, 0.9)}})
    ing = _ingestor(database, client)
    ing.ingest(DAY)
    assert ing.learn(DAY + timedelta(days=1)) == 0


def test_daily_scorecard_numbers(
    database: str, pub: Published, store: PostgresStore, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    sql("UPDATE channels SET analytics_credentials_ref = %s WHERE id = %s", REF, ch)
    job, vid = pub.video(ch, "scored")
    store.record_cost(job, "render", 120, actor="renderer")
    store.record_qa(job, passed=True, risk_score=10,
                    flags=[{"code": "x", "severity": "warn"}], sources=[], actor="qa")  # fmt: skip
    rejected = store.create_job(ch, "planner", actor="planner")
    chash = drive_to_awaiting(store, pub.assets, rejected, ch)
    sql("INSERT INTO job_rejections (job_id, channel_id, content_hash, reason, rework_stage, "
        "rejected_by) VALUES (%s, %s, %s, 'misleading thumbnail', 'render', 'owner')",
        rejected, ch, chash)  # fmt: skip
    client = FakeAnalyticsClient({vid: {DAY: _row(vid, 500, 0.05, 0.4)}})
    ing = _ingestor(database, client)
    ing.ingest(DAY)
    today = datetime.now(UTC).date()
    sql("UPDATE video_metrics SET metric_date = %s WHERE youtube_video_id = %s", today, vid)
    ing.scorecards(today)
    row = sql("SELECT jobs_approved, jobs_rejected, approval_rate::float8, policy_flags, "
              "videos_published, cost_cents, cost_per_video::float8, avg_ctr::float8, "
              "avg_retention::float8, views, revenue_cents FROM daily_scorecards "
              "WHERE channel_id = %s AND score_date = %s", ch, today)[0]  # fmt: skip
    assert row == (1, 1, 0.5, 1, 1, 120, 120.0, 0.05, 0.4, 500, 250)
    ing.scorecards(today)  # recomputed in place, not duplicated
    assert sql("SELECT count(*) FROM daily_scorecards WHERE channel_id = %s", ch)[0][0] == 1
