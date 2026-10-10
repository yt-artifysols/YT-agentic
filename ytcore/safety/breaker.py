"""Cluster circuit breaker.

Looks at each ACTIVE cluster over a sliding window and pauses it (the cluster kill switch, so
the database refuses approvals, scheduling and uploads for every channel in it) when:
  * the rejection rate of human decisions passes a threshold,
  * policy flags per QA'd video pass a threshold, or
  * the average cost per job jumps against the cluster's own previous week.
A trip is recorded (append-only, audited) and operators get an alert. The breaker only ever
PAUSES; resuming is a human decision in the approval hub. After a trip the window restarts, so
a resumed cluster is judged on fresh data only.

    python -m ytcore.safety.breaker run-once
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ytcore.store.postgres import Connect, map_db_error

log = logging.getLogger(__name__)
BASELINE_DAYS = 7
ACTOR = "breaker:auto"


@dataclass(frozen=True)
class BreakerThresholds:
    window_hours: int = 24
    min_decisions: int = 5
    max_rejection_rate: float = 0.5
    max_flags_per_job: float = 3.0
    max_cost_ratio: float = 3.0


@dataclass(frozen=True)
class ClusterStats:
    cluster_id: str
    window_start: datetime
    approvals: int = 0
    rejections: int = 0
    qa_reports: int = 0
    flags: int = 0
    recent_jobs: int = 0
    recent_cost_avg: float = 0.0
    baseline_jobs: int = 0
    baseline_cost_avg: float = 0.0

    @property
    def decisions(self) -> int:
        return self.approvals + self.rejections

    @property
    def rejection_rate(self) -> float:
        return self.rejections / self.decisions if self.decisions else 0.0

    @property
    def flags_per_job(self) -> float:
        return self.flags / self.qa_reports if self.qa_reports else 0.0

    @property
    def cost_ratio(self) -> float:
        return self.recent_cost_avg / self.baseline_cost_avg if self.baseline_cost_avg else 0.0


@dataclass(frozen=True)
class BreakerReason:
    code: str
    value: float
    limit: float
    detail: str


def evaluate(stats: ClusterStats, thr: BreakerThresholds) -> list[BreakerReason]:
    reasons: list[BreakerReason] = []
    if stats.decisions >= thr.min_decisions and stats.rejection_rate > thr.max_rejection_rate:
        reasons.append(
            BreakerReason(
                "rejection_rate",
                round(stats.rejection_rate, 3),
                thr.max_rejection_rate,
                f"{stats.rejections} of {stats.decisions} human decisions were rejections",
            )
        )
    if stats.qa_reports >= thr.min_decisions and stats.flags_per_job > thr.max_flags_per_job:
        reasons.append(
            BreakerReason(
                "policy_flags",
                round(stats.flags_per_job, 2),
                thr.max_flags_per_job,
                f"{stats.flags} policy flags over {stats.qa_reports} QA reports",
            )
        )
    if (
        stats.recent_jobs >= thr.min_decisions
        and stats.baseline_jobs >= thr.min_decisions
        and stats.cost_ratio > thr.max_cost_ratio
    ):
        reasons.append(
            BreakerReason(
                "cost_anomaly",
                round(stats.cost_ratio, 2),
                thr.max_cost_ratio,
                f"average cost per job {stats.recent_cost_avg:.0f}c vs "
                f"{stats.baseline_cost_avg:.0f}c in the previous {BASELINE_DAYS} days",
            )
        )
    return reasons


@dataclass(frozen=True)
class Trip:
    cluster_id: str
    reasons: list[BreakerReason]
    stats: ClusterStats

    def message(self) -> str:
        return "; ".join(f"{r.code}: {r.detail} (limit {r.limit})" for r in self.reasons)


class BreakerRepository:
    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    @classmethod
    def from_conninfo(cls, conninfo: str) -> BreakerRepository:
        return cls(lambda: psycopg.connect(conninfo, autocommit=True))

    @contextmanager
    def _tx(self) -> Iterator[psycopg.Connection[Any]]:
        try:
            with self._connect() as conn, conn.transaction():
                conn.execute("SELECT set_config('app.actor', %s, true)", (ACTOR,))
                yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    def active_clusters(self) -> list[str]:
        with self._tx() as conn:
            rows = conn.execute("SELECT id FROM clusters WHERE status = 'active' ORDER BY id")
            return [r[0] for r in rows.fetchall()]

    def thresholds(self, cluster_id: str) -> tuple[bool, BreakerThresholds]:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT enabled, window_hours, min_decisions, max_rejection_rate, "
                "max_flags_per_job, max_cost_ratio FROM cluster_breaker_config "
                "WHERE cluster_id = %s",
                (cluster_id,),
            ).fetchone()
        if row is None:
            return True, BreakerThresholds()
        return bool(row[0]), BreakerThresholds(
            int(row[1]), int(row[2]), float(row[3]), float(row[4]), float(row[5])
        )

    def stats(self, cluster_id: str, thr: BreakerThresholds, now: datetime) -> ClusterStats:
        with self._tx() as conn:
            last = conn.execute(
                "SELECT max(created_at) FROM breaker_trips WHERE cluster_id = %s", (cluster_id,)
            ).fetchone()
            start = now - timedelta(hours=thr.window_hours)
            if last and last[0] is not None:
                start = max(start, last[0])  # a resumed cluster is judged on fresh data only

            def one(sql: str, *params: Any) -> tuple[Any, ...]:
                row = conn.execute(sql, params).fetchone()
                assert row is not None
                return tuple(row)

            approvals = one(
                "SELECT count(*) FROM approvals a JOIN channels c ON c.id = a.channel_id "
                "WHERE c.cluster_id = %s AND a.issued_at >= %s",
                cluster_id,
                start,
            )[0]
            rejections = one(
                "SELECT count(*) FROM job_rejections r JOIN channels c ON c.id = r.channel_id "
                "WHERE c.cluster_id = %s AND r.created_at >= %s",
                cluster_id,
                start,
            )[0]
            qa_reports, flags = one(
                "SELECT count(*), coalesce(sum(jsonb_array_length(q.flags)), 0) "
                "FROM qa_reports q JOIN video_jobs j ON j.id = q.job_id "
                "JOIN channels c ON c.id = j.channel_id "
                "WHERE c.cluster_id = %s AND q.created_at >= %s",
                cluster_id,
                start,
            )
            cost_sql = (
                "SELECT count(*), coalesce(avg(t), 0) FROM (SELECT sum(k.cost_cents) AS t "
                "FROM job_costs k JOIN channels c ON c.id = k.channel_id "
                "WHERE c.cluster_id = %s AND k.at >= %s AND k.at < %s GROUP BY k.job_id) x"
            )
            recent_jobs, recent_avg = one(cost_sql, cluster_id, start, now + timedelta(days=1))
            baseline_jobs, baseline_avg = one(
                cost_sql, cluster_id, start - timedelta(days=BASELINE_DAYS), start
            )
        return ClusterStats(
            cluster_id,
            start,
            int(approvals),
            int(rejections),
            int(qa_reports),
            int(flags),
            int(recent_jobs),
            float(recent_avg),
            int(baseline_jobs),
            float(baseline_avg),
        )

    def trip(self, trip: Trip) -> bool:
        """Pause the cluster, record the trip and raise an operator alert, atomically.
        Returns False if the cluster was no longer active."""
        with self._tx() as conn:
            paused = conn.execute(
                "UPDATE clusters SET status = 'paused' WHERE id = %s AND status = 'active' "
                "RETURNING id",
                (trip.cluster_id,),
            ).fetchone()
            if paused is None:
                return False
            conn.execute(
                "INSERT INTO breaker_trips (cluster_id, reasons, stats) VALUES (%s, %s, %s)",
                (
                    trip.cluster_id,
                    Jsonb([asdict(r) for r in trip.reasons]),
                    Jsonb(asdict(trip.stats), dumps=_dumps),
                ),
            )
            conn.execute(
                "INSERT INTO alerts (cluster_id, kind, message) VALUES (%s, 'breaker.tripped', %s)",
                (
                    trip.cluster_id,
                    f"cluster paused by the circuit breaker: {trip.message()}"[:2000],
                ),
            )
        return True


def _dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, default=lambda o: o.isoformat() if isinstance(o, datetime) else str(o))


Notify = Callable[[str, str], None]


def run_once(
    repo: BreakerRepository, *, now: datetime | None = None, notify: Notify | None = None
) -> list[Trip]:
    now = now or datetime.now(UTC)
    tripped: list[Trip] = []
    for cluster_id in repo.active_clusters():
        enabled, thr = repo.thresholds(cluster_id)
        if not enabled:
            continue
        stats = repo.stats(cluster_id, thr, now)
        reasons = evaluate(stats, thr)
        if not reasons:
            continue
        trip = Trip(cluster_id, reasons, stats)
        if repo.trip(trip):
            tripped.append(trip)
            log.warning("circuit breaker paused cluster %s: %s", cluster_id, trip.message())
            if notify is not None:
                notify(f"Cluster {cluster_id} paused by the circuit breaker", trip.message())
    return tripped


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI
    parser = argparse.ArgumentParser(prog="python -m ytcore.safety.breaker")
    parser.add_argument("cmd", choices=["run-once"])
    parser.parse_args(argv)
    from ytcore.approvals.service.notify import Notification, build_notifier
    from ytcore.approvals.service.settings import HubSettings
    from ytcore.config import build_secrets_provider, load_settings
    from ytcore.safety.scrub import install_log_scrubber
    from ytcore.secrets.provider import SecretRef

    install_log_scrubber()
    core = load_settings()
    secrets = build_secrets_provider(core)
    dsn = secrets.get(SecretRef(core.database_url_ref)).reveal()
    hub = HubSettings()
    notifier = build_notifier(hub, secrets)

    def notify(title: str, body: str) -> None:
        notifier.send(Notification("breaker_tripped", title, body, hub.origin))

    trips = run_once(BreakerRepository.from_conninfo(dsn), notify=notify)
    print(f"{len(trips)} cluster(s) paused")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
