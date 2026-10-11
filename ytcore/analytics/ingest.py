"""Pull per-video metrics into `video_metrics`, learn what worked into per-channel memory, and
compute daily scorecards.

Isolation: each channel is processed alone with its own analytics token and its own memory
namespace; one channel's failure never stops the others. Metrics are numbers, and the memory
notes carry only our own title and numbers (never viewer comments or other external text)."""

from __future__ import annotations

import logging
import statistics
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg

from ytcore.agents.memory import PostgresMemory
from ytcore.analytics.client import AnalyticsClient, VideoMetricsRow
from ytcore.secrets.provider import SecretRef, SecretsProvider
from ytcore.store.postgres import Connect, map_db_error

log = logging.getLogger(__name__)
ACTOR = "analytics"
MIN_VIEWS_FOR_LEARNING = 100
LEARNING_WINDOW_DAYS = 28


@dataclass
class IngestReport:
    ingested: int = 0
    missing: int = 0
    failed_channels: dict[str, str] = field(default_factory=dict)
    learned: int = 0
    scorecards: int = 0


class AnalyticsIngestor:
    def __init__(
        self,
        connect: Connect,
        client: AnalyticsClient,
        secrets: SecretsProvider,
        memory: PostgresMemory,
    ) -> None:
        self._connect = connect
        self._client = client
        self._secrets = secrets
        self._memory = memory

    @contextmanager
    def _tx(self) -> Iterator[psycopg.Connection[Any]]:
        try:
            with self._connect() as conn, conn.transaction():
                conn.execute("SELECT set_config('app.actor', %s, true)", (ACTOR,))
                yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    # ------------------------------------------------------------------ ingestion

    def ingest(self, day: date) -> IngestReport:
        report = IngestReport()
        with self._tx() as conn:
            channels = conn.execute(
                "SELECT id, analytics_credentials_ref FROM channels "
                "WHERE analytics_credentials_ref IS NOT NULL ORDER BY id"
            ).fetchall()
        for channel_id, ref in channels:
            try:
                self._ingest_channel(channel_id, ref, day, report)
            except Exception as exc:  # isolate channels; report the type only (no secrets)
                report.failed_channels[channel_id] = type(exc).__name__
                log.warning("analytics ingestion failed for %s: %s", channel_id, type(exc).__name__)
        return report

    def _ingest_channel(self, channel_id: str, ref: str, day: date, report: IngestReport) -> None:
        token = self._secrets.get(SecretRef(ref))
        with self._tx() as conn:
            videos = conn.execute(
                "SELECT job_id::text, youtube_video_id FROM uploads WHERE channel_id = %s "
                "AND status IN ('uploaded', 'live') AND youtube_video_id IS NOT NULL "
                "AND created_at::date <= %s",
                (channel_id, day),
            ).fetchall()
        for job_id, video_id in videos:
            row = self._client.fetch(token=token, video_id=video_id, day=day)
            if row is None:
                report.missing += 1
                continue
            self._store(channel_id, job_id, row)
            report.ingested += 1

    def _store(self, channel_id: str, job_id: str, row: VideoMetricsRow) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO video_metrics (channel_id, job_id, youtube_video_id, metric_date, "
                "views, impressions, ctr, avg_view_seconds, retention, revenue_cents) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (youtube_video_id, metric_date) DO UPDATE SET views = EXCLUDED.views, "
                "impressions = EXCLUDED.impressions, ctr = EXCLUDED.ctr, "
                "avg_view_seconds = EXCLUDED.avg_view_seconds, retention = EXCLUDED.retention, "
                "revenue_cents = EXCLUDED.revenue_cents, ingested_at = now()",
                (channel_id, job_id, row.youtube_video_id, row.metric_date, row.views,
                 row.impressions, row.ctr, row.avg_view_seconds, row.retention,
                 row.revenue_cents),
            )  # fmt: skip

    # ------------------------------------------------------------------ learning

    def learn(self, today: date) -> int:
        """Write a performance note to each channel's own memory for videos that clearly beat or
        trailed the channel's median (CTR x retention), once per video per ISO week."""
        learned = 0
        since = today - timedelta(days=LEARNING_WINDOW_DAYS)
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT m.channel_id, m.job_id::text, m.youtube_video_id, sum(m.views), "
                "avg(m.ctr), avg(m.retention), "
                "coalesce(max(j.manifest -> 'metadata' ->> 'title'), '') "
                "FROM video_metrics m JOIN video_jobs j ON j.id = m.job_id "
                "WHERE m.metric_date > %s GROUP BY m.channel_id, m.job_id, m.youtube_video_id "
                "HAVING sum(m.views) >= %s",
                (since, MIN_VIEWS_FOR_LEARNING),
            ).fetchall()
        by_channel: dict[str, list[tuple[str, str, int, float, float, str]]] = {}
        for channel_id, job_id, video_id, views, ctr, retention, title in rows:
            if ctr is None or retention is None:
                continue
            by_channel.setdefault(channel_id, []).append(
                (job_id, video_id, int(views), float(ctr), float(retention), title)
            )
        week = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
        for channel_id, vids in by_channel.items():
            if len(vids) < 3:
                continue  # not enough history for a meaningful median
            scores = [ctr * retention for _, _, _, ctr, retention, _ in vids]
            median = statistics.median(scores)
            memory = self._memory.for_channel(channel_id)
            for (job_id, video_id, views, ctr, retention, title), score in zip(
                vids, scores, strict=True
            ):
                if score > median * 1.25:
                    verdict = "above"
                elif score < median * 0.75:
                    verdict = "below"
                else:
                    continue
                with self._tx() as conn:
                    fresh = conn.execute(
                        "INSERT INTO performance_notes (youtube_video_id, iso_week, channel_id, "
                        "verdict) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING 1",
                        (video_id, week, channel_id, verdict),
                    ).fetchone()
                if fresh is None:
                    continue
                memory.add(
                    "performance",
                    f"Video '{title[:120]}' performed {verdict} the channel median over the last "
                    f"{LEARNING_WINDOW_DAYS} days: {views} views, CTR {ctr:.1%}, retention "
                    f"{retention:.1%} (median CTR x retention {median:.4f}).",
                    source_job=job_id,
                )
                learned += 1
        return learned

    # ------------------------------------------------------------------ scorecards

    def scorecards(self, day: date) -> int:
        with self._tx() as conn:
            channels = [r[0] for r in conn.execute("SELECT id FROM channels ORDER BY id")]
            for ch in channels:
                conn.execute(_SCORECARD_SQL, {"ch": ch, "day": day})
        return len(channels)

    def run_daily(self, now: datetime | None = None) -> IngestReport:
        """Yesterday's metrics, learnings, and yesterday's scorecards."""
        now = now or datetime.now(UTC)
        day = (now - timedelta(days=1)).date()
        report = self.ingest(day)
        report.learned = self.learn(now.date())
        report.scorecards = self.scorecards(day)
        return report


_SCORECARD_SQL = """
INSERT INTO daily_scorecards (channel_id, score_date, jobs_planned, jobs_approved, jobs_rejected,
    approval_rate, policy_flags, videos_published, cost_cents, cost_per_video, avg_ctr,
    avg_retention, views, revenue_cents)
SELECT %(ch)s::text, %(day)s::date,
  (SELECT count(*) FROM daily_plans WHERE channel_id = %(ch)s AND slot_date = %(day)s),
  a.n, r.n,
  CASE WHEN a.n + r.n = 0 THEN NULL ELSE a.n::numeric / (a.n + r.n) END,
  q.flags,
  u.n,
  c.total,
  CASE WHEN c.jobs = 0 THEN NULL ELSE c.total::numeric / c.jobs END,
  m.ctr, m.retention, m.views, m.revenue
FROM
  (SELECT count(*) AS n FROM approvals WHERE channel_id = %(ch)s
     AND (issued_at AT TIME ZONE 'UTC')::date = %(day)s) a,
  (SELECT count(*) AS n FROM job_rejections WHERE channel_id = %(ch)s
     AND (created_at AT TIME ZONE 'UTC')::date = %(day)s) r,
  (SELECT coalesce(sum(jsonb_array_length(q.flags)), 0) AS flags FROM qa_reports q
     JOIN video_jobs j ON j.id = q.job_id WHERE j.channel_id = %(ch)s
     AND (q.created_at AT TIME ZONE 'UTC')::date = %(day)s) q,
  (SELECT count(*) AS n FROM uploads WHERE channel_id = %(ch)s AND status IN ('uploaded', 'live')
     AND (created_at AT TIME ZONE 'UTC')::date = %(day)s) u,
  (SELECT coalesce(sum(cost_cents), 0) AS total, count(DISTINCT job_id) AS jobs FROM job_costs
     WHERE channel_id = %(ch)s AND cost_date = %(day)s) c,
  (SELECT coalesce(sum(views), 0) AS views, avg(ctr) AS ctr, avg(retention) AS retention,
          sum(revenue_cents) AS revenue FROM video_metrics
     WHERE channel_id = %(ch)s AND metric_date = %(day)s) m
ON CONFLICT (channel_id, score_date) DO UPDATE SET jobs_planned = EXCLUDED.jobs_planned,
  jobs_approved = EXCLUDED.jobs_approved, jobs_rejected = EXCLUDED.jobs_rejected,
  approval_rate = EXCLUDED.approval_rate, policy_flags = EXCLUDED.policy_flags,
  videos_published = EXCLUDED.videos_published, cost_cents = EXCLUDED.cost_cents,
  cost_per_video = EXCLUDED.cost_per_video, avg_ctr = EXCLUDED.avg_ctr,
  avg_retention = EXCLUDED.avg_retention, views = EXCLUDED.views,
  revenue_cents = EXCLUDED.revenue_cents, computed_at = now()
"""


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI
    import argparse

    parser = argparse.ArgumentParser(prog="python -m ytcore.analytics.ingest")
    parser.add_argument("cmd", choices=["run-daily"])
    parser.parse_args(argv)
    from ytcore.analytics.client import FakeAnalyticsClient, RealYouTubeAnalyticsClient
    from ytcore.config import build_secrets_provider, load_settings
    from ytcore.safety.scrub import install_log_scrubber

    install_log_scrubber()
    settings = load_settings()
    secrets = build_secrets_provider(settings)
    dsn = secrets.get(SecretRef(settings.database_url_ref)).reveal()
    if settings.youtube_backend == "real":
        client: AnalyticsClient = RealYouTubeAnalyticsClient(
            secrets.get(SecretRef("vault://youtube-oauth-client"))
        )
    else:
        client = FakeAnalyticsClient()

    def connect() -> psycopg.Connection[Any]:
        return psycopg.connect(dsn, autocommit=True)

    report = AnalyticsIngestor(connect, client, secrets, PostgresMemory(connect)).run_daily()
    print(
        f"ingested {report.ingested}, learned {report.learned}, scorecards {report.scorecards}, "
        f"failed channels {sorted(report.failed_channels)}"
    )
    return 1 if report.failed_channels else 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
