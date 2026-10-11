"""Cluster rollout: cluster B cannot go live until cluster A has met its gate.

Two layers. The DATABASE enforces the hard part (time live, a sign-off by a human hub admin after
the gate period; channels of a dark cluster cannot be gated). This module computes the evidence a
human reviews, and refuses a sign-off while the evidence is bad, so "stable" is measured, not
asserted:
  * live for the whole gate period
  * no circuit-breaker trip, no unresolved stuck-job / plan alerts, no failed or pending uploads
  * human rejection rate under a limit once there are enough decisions
  * a scorecard for (almost) every day, i.e. the analytics pipeline really ran
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ytcore.store.postgres import Connect, map_db_error

MAX_REJECTION_RATE = 0.4
MIN_DECISIONS = 10
MAX_MISSING_SCORECARD_DAYS = 2


@dataclass
class GateReport:
    cluster_id: str
    live: bool
    live_days: float
    required_days: int
    breaker_trips: int = 0
    stuck_or_plan_alerts: int = 0
    failed_uploads: int = 0
    pending_uploads: int = 0
    decisions: int = 0
    rejection_rate: float | None = None
    scorecard_days: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["ok"] = self.ok
        return out


class RolloutService:
    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    @contextmanager
    def _tx(self, actor: str) -> Iterator[psycopg.Connection[Any]]:
        try:
            with self._connect() as conn, conn.transaction():
                conn.execute("SELECT set_config('app.actor', %s, true)", (actor,))
                yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    def clusters(self) -> list[dict[str, Any]]:
        with self._tx("hub:reader") as conn:
            rows = conn.execute(
                "SELECT k.id, k.display_name, k.niche, k.status, k.live_enabled, k.live_since, "
                "k.requires_cluster, k.gate_days, k.max_concurrent_jobs, "
                "(SELECT count(*) FROM channels c WHERE c.cluster_id = k.id), "
                "(SELECT count(*) FROM video_jobs j JOIN channels c ON c.id = j.channel_id "
                " WHERE c.cluster_id = k.id AND j.state = 'awaiting_approval'), "
                "(SELECT min(e.at) FROM video_jobs j JOIN channels c ON c.id = j.channel_id "
                " JOIN job_events e ON e.job_id = j.id AND e.to_state = 'awaiting_approval' "
                " WHERE c.cluster_id = k.id AND j.state = 'awaiting_approval'), "
                "h.pending, h.running, h.finished, h.failed, h.budget_held, h.reported_at "
                "FROM clusters k LEFT JOIN cluster_health h ON h.cluster_id = k.id ORDER BY k.id"
            ).fetchall()
        return [
            {
                "id": r[0], "display_name": r[1], "niche": r[2], "status": r[3],
                "live_enabled": r[4], "live_since": r[5], "requires_cluster": r[6],
                "gate_days": r[7], "max_concurrent_jobs": r[8], "channels": r[9],
                "awaiting_approval": r[10], "oldest_waiting_since": r[11],
                "health": None if r[12] is None else {
                    "pending": r[12], "running": r[13], "finished": r[14], "failed": r[15],
                    "budget_held": r[16], "reported_at": r[17],
                },
            }
            for r in rows
        ]  # fmt: skip

    def gate(self, cluster_id: str, now: datetime | None = None) -> GateReport:
        now = now or datetime.now(UTC)
        with self._tx("hub:reader") as conn:
            row = conn.execute(
                "SELECT live_enabled, live_since, gate_days FROM clusters WHERE id = %s",
                (cluster_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown cluster {cluster_id}")
            live, since, required = bool(row[0]), row[1], int(row[2])
            report = GateReport(
                cluster_id,
                live,
                (now - since).total_seconds() / 86400 if live and since else 0.0,
                required,
            )
            start = since or now

            def count(sql: str, *params: Any) -> int:
                got = conn.execute(sql, params).fetchone()
                return int(got[0]) if got else 0

            report.breaker_trips = count(
                "SELECT count(*) FROM breaker_trips WHERE cluster_id = %s AND created_at >= %s",
                cluster_id,
                start,
            )
            report.stuck_or_plan_alerts = count(
                "SELECT count(*) FROM alerts a LEFT JOIN channels c ON c.id = a.channel_id "
                "WHERE (c.cluster_id = %s OR a.cluster_id = %s) AND a.created_at >= %s "
                "AND a.kind IN ('job.stuck', 'plan.exhausted', 'plan.missing', 'gateway.failure')",
                cluster_id,
                cluster_id,
                start,
            )
            report.failed_uploads = count(
                "SELECT count(*) FROM uploads u JOIN channels c ON c.id = u.channel_id "
                "WHERE c.cluster_id = %s AND u.status = 'failed' AND u.created_at >= %s",
                cluster_id,
                start,
            )
            report.pending_uploads = count(
                "SELECT count(*) FROM uploads u JOIN channels c ON c.id = u.channel_id "
                "WHERE c.cluster_id = %s AND u.status = 'pending'",
                cluster_id,
            )
            approvals = count(
                "SELECT count(*) FROM approvals a JOIN channels c ON c.id = a.channel_id "
                "WHERE c.cluster_id = %s AND a.issued_at >= %s",
                cluster_id,
                start,
            )
            rejections = count(
                "SELECT count(*) FROM job_rejections r JOIN channels c ON c.id = r.channel_id "
                "WHERE c.cluster_id = %s AND r.created_at >= %s",
                cluster_id,
                start,
            )
            report.decisions = approvals + rejections
            if report.decisions:
                report.rejection_rate = rejections / report.decisions
            report.scorecard_days = count(
                "SELECT count(DISTINCT s.score_date) FROM daily_scorecards s "
                "JOIN channels c ON c.id = s.channel_id WHERE c.cluster_id = %s "
                "AND s.score_date >= %s::date",
                cluster_id,
                start,
            )
        self._judge(report)
        return report

    @staticmethod
    def _judge(r: GateReport) -> None:
        p = r.problems
        if not r.live:
            p.append("the cluster is not live")
        elif r.live_days < r.required_days:
            p.append(f"live for {r.live_days:.1f} of {r.required_days} required days")
        if r.breaker_trips:
            p.append(f"{r.breaker_trips} circuit-breaker trip(s) during the period")
        if r.stuck_or_plan_alerts:
            p.append(f"{r.stuck_or_plan_alerts} stuck-job / plan / gateway alert(s)")
        if r.failed_uploads:
            p.append(f"{r.failed_uploads} failed upload(s)")
        if r.pending_uploads:
            p.append(f"{r.pending_uploads} upload(s) still pending reconciliation")
        if (
            r.decisions >= MIN_DECISIONS
            and r.rejection_rate is not None
            and r.rejection_rate > MAX_REJECTION_RATE
        ):
            p.append(
                f"human rejection rate {r.rejection_rate:.0%} is above {MAX_REJECTION_RATE:.0%}"
            )
        if r.live and r.scorecard_days < max(r.required_days - MAX_MISSING_SCORECARD_DAYS, 1):
            p.append(f"only {r.scorecard_days} daily scorecards: the analytics job is not running")

    def sign_off(
        self, cluster_id: str, reviewer: str, note: str, now: datetime | None = None
    ) -> int:
        """Record a human sign-off. Refused while the measured evidence is bad."""
        report = self.gate(cluster_id, now)
        if not report.ok:
            raise GateNotMet("; ".join(report.problems))
        with self._tx(f"hub:{reviewer}") as conn:
            row = conn.execute(
                "INSERT INTO rollout_signoffs (cluster_id, reviewer, note, evidence) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (cluster_id, reviewer, note, Jsonb(_jsonable(report.to_json()))),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def enable(self, cluster_id: str, actor: str) -> None:
        """Turn the cluster on for live publishing; the database checks the prerequisite."""
        with self._tx(f"hub:{actor}") as conn:
            conn.execute("UPDATE clusters SET live_enabled = true WHERE id = %s", (cluster_id,))

    def scorecards(
        self, *, channel: str | None, cluster: str | None, days: int
    ) -> list[dict[str, Any]]:
        since = datetime.now(UTC).date() - timedelta(days=days)
        with self._tx("hub:reader") as conn:
            rows = conn.execute(
                "SELECT s.channel_id, c.cluster_id, s.score_date, s.jobs_planned, s.jobs_approved, "
                "s.jobs_rejected, s.approval_rate::float8, s.policy_flags, s.videos_published, "
                "s.cost_cents, s.cost_per_video::float8, s.avg_ctr::float8, "
                "s.avg_retention::float8, s.views, s.revenue_cents "
                "FROM daily_scorecards s JOIN channels c ON c.id = s.channel_id "
                "WHERE s.score_date >= %s AND (%s::text IS NULL OR s.channel_id = %s) "
                "AND (%s::text IS NULL OR c.cluster_id = %s) "
                "ORDER BY s.score_date DESC, s.channel_id",
                (since, channel, channel, cluster, cluster),
            ).fetchall()
        keys = [
            "channel_id",
            "cluster_id",
            "score_date",
            "jobs_planned",
            "jobs_approved",
            "jobs_rejected",
            "approval_rate",
            "policy_flags",
            "videos_published",
            "cost_cents",
            "cost_per_video",
            "avg_ctr",
            "avg_retention",
            "views",
            "revenue_cents",
        ]
        return [dict(zip(keys, r, strict=True)) for r in rows]


class GateNotMet(Exception):
    """The measured evidence for a rollout sign-off is not good enough yet."""


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value
