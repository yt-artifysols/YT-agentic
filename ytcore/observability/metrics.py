"""Metrics.

Two sources, one naming scheme:
  * event metrics (counters/histograms through the OpenTelemetry metrics API; no-ops until an SDK
    meter provider is installed): stage duration, activity outcomes, publishes, rework
  * state metrics computed from the database by `collect` (queue depth, stage latency, rework
    rate, approval time, cost per video, quota use), rendered for Prometheus by `render_prometheus`
    and served by `python -m ytcore.observability.exporter`.
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ParamSpec, TypeVar

import psycopg
from opentelemetry import metrics
from temporalio import activity

from ytcore.observability.tracing import job_span
from ytcore.store.postgres import Connect, map_db_error

P = ParamSpec("P")
R = TypeVar("R")

_meter = metrics.get_meter("ytcore")
_activity_seconds = _meter.create_histogram(
    "ytcore_activity_duration_seconds", unit="s", description="Workflow activity duration"
)
_activity_total = _meter.create_counter(
    "ytcore_activity_total", description="Workflow activity outcomes"
)
_publish_total = _meter.create_counter("ytcore_publish_total", description="Gateway publishes")


def record_publish(outcome: str) -> None:
    _publish_total.add(1, {"outcome": outcome})


async def _heartbeat_loop() -> None:
    """Heartbeat while an activity runs so a dead worker is noticed quickly."""
    try:
        timeout = activity.info().heartbeat_timeout
    except RuntimeError:  # not inside an activity (direct calls in tests)
        return
    if timeout is None:
        return
    period = max(timeout.total_seconds() / 3, 0.5)
    while True:
        await asyncio.sleep(period)
        activity.heartbeat()


def observed(name: str) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Wrap an async activity (a method whose first argument after self is the job id) in a span
    and record its duration and outcome. Applied UNDER @activity.defn."""

    def deco(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            job_id = str(args[1] if len(args) > 1 else kwargs.get("job_id", "unknown"))
            started = time.perf_counter()
            outcome = "ok"
            beat = asyncio.ensure_future(_heartbeat_loop())
            try:
                with job_span(f"activity.{name}", job_id):
                    return await fn(*args, **kwargs)
            except Exception as exc:
                outcome = type(exc).__name__
                raise
            finally:
                beat.cancel()
                labels = {"activity": name, "outcome": outcome}
                _activity_seconds.record(time.perf_counter() - started, labels)
                _activity_total.add(1, labels)

        return wrapper

    return deco


# ----------------------------------------------------------------------------- DB snapshot

ACTIVE_STATES = (
    "planned",
    "researching",
    "scripting",
    "rendering",
    "qa",
    "awaiting_approval",
    "approved",
    "scheduled",
    "publishing",
    "published",
)


@dataclass
class Snapshot:
    queue_depth: dict[str, int] = field(default_factory=dict)  # state -> jobs
    stage_latency_seconds: dict[str, dict[str, float]] = field(default_factory=dict)
    rework_rate: float = 0.0  # reworks per job that reached QA (last 7 days)
    approval_seconds_avg: float = 0.0
    cost_per_video_cents: dict[str, float] = field(default_factory=dict)  # channel -> avg cents
    quota_used_ratio: dict[str, float] = field(default_factory=dict)  # google project -> 0..1
    uploads_failed_1h: int = 0
    open_alerts_24h: int = 0


def collect(connect: Connect) -> Snapshot:
    snap = Snapshot()
    try:
        with connect() as conn:
            for state, n in conn.execute(
                "SELECT state, count(*) FROM video_jobs GROUP BY state"
            ).fetchall():
                snap.queue_depth[state] = int(n)
            # Time spent in each stage = gap to the job's next event (last 7 days).
            rows = conn.execute(
                "WITH e AS (SELECT job_id, to_state, at, lead(at) OVER (PARTITION BY job_id "
                "ORDER BY id) AS next_at FROM job_events WHERE at > now() - interval '7 days') "
                "SELECT to_state, percentile_cont(0.5) WITHIN GROUP "
                "(ORDER BY extract(epoch FROM next_at - at)), percentile_cont(0.95) WITHIN GROUP "
                "(ORDER BY extract(epoch FROM next_at - at)) FROM e WHERE next_at IS NOT NULL "
                "AND to_state IN ('researching', 'scripting', 'rendering', 'qa', 'publishing') "
                "GROUP BY to_state"
            ).fetchall()
            for state, p50, p95 in rows:
                snap.stage_latency_seconds[state] = {"p50": float(p50), "p95": float(p95)}
            row = conn.execute(
                "SELECT count(*) FILTER (WHERE to_state = 'rendering'), "
                "count(DISTINCT job_id) FILTER (WHERE to_state = 'qa') FROM job_events "
                "WHERE at > now() - interval '7 days'"
            ).fetchone()
            renders, qa_jobs = (row[0], row[1]) if row else (0, 0)
            snap.rework_rate = max(renders - qa_jobs, 0) / qa_jobs if qa_jobs else 0.0
            row = conn.execute(
                "SELECT coalesce(avg(extract(epoch FROM a.issued_at - s.at)), 0) FROM approvals a "
                "JOIN LATERAL (SELECT max(at) AS at FROM job_events e WHERE e.job_id = a.job_id "
                "AND e.to_state = 'awaiting_approval' AND e.at <= a.issued_at) s ON true "
                "WHERE a.issued_at > now() - interval '7 days'"
            ).fetchone()
            snap.approval_seconds_avg = float(row[0]) if row else 0.0
            for channel, avg in conn.execute(
                "SELECT channel_id, avg(total) FROM (SELECT channel_id, job_id, sum(cost_cents) "
                "AS total FROM job_costs WHERE at > now() - interval '7 days' "
                "GROUP BY channel_id, job_id) x GROUP BY channel_id"
            ).fetchall():
                snap.cost_per_video_cents[channel] = float(avg)
            for project, used, cap in conn.execute(
                "SELECT g.id, coalesce(sum(q.units), 0), g.daily_quota_units "
                "FROM google_projects g "
                "LEFT JOIN quota_ledger q ON q.google_project_id = g.id "
                "AND q.usage_date = youtube_quota_day() GROUP BY g.id, g.daily_quota_units"
            ).fetchall():
                snap.quota_used_ratio[project] = float(used) / float(cap) if cap else 0.0
            row = conn.execute(
                "SELECT count(*) FROM uploads WHERE status = 'failed' "
                "AND updated_at > now() - interval '1 hour'"
            ).fetchone()
            snap.uploads_failed_1h = int(row[0]) if row else 0
            row = conn.execute(
                "SELECT count(*) FROM alerts WHERE created_at > now() - interval '24 hours'"
            ).fetchone()
            snap.open_alerts_24h = int(row[0]) if row else 0
    except psycopg.Error as exc:
        raise map_db_error(exc) from exc
    return snap


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render_prometheus(snap: Snapshot) -> str:
    lines: list[str] = []

    def metric(name: str, help_: str, samples: list[tuple[dict[str, str], float]]) -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} gauge")
        for labels, value in samples:
            tag = ",".join(f'{k}="{_label(v)}"' for k, v in labels.items())
            lines.append(f"{name}{{{tag}}} {value}" if tag else f"{name} {value}")

    metric(
        "ytcore_queue_depth",
        "Jobs per state",
        [({"state": s}, float(snap.queue_depth.get(s, 0))) for s in ACTIVE_STATES],
    )
    metric(
        "ytcore_stage_latency_seconds",
        "Time jobs spend in a stage (last 7 days)",
        [
            ({"stage": stage, "quantile": q}, v)
            for stage, qs in sorted(snap.stage_latency_seconds.items())
            for q, v in qs.items()
        ],
    )
    metric("ytcore_rework_rate", "QA rework loops per job (7d)", [({}, snap.rework_rate)])
    metric(
        "ytcore_approval_seconds_avg",
        "Mean time from awaiting_approval to approval (7d)",
        [({}, snap.approval_seconds_avg)],
    )
    metric(
        "ytcore_cost_per_video_cents",
        "Average cost per video by channel (7d)",
        [({"channel": c}, v) for c, v in sorted(snap.cost_per_video_cents.items())],
    )
    metric(
        "ytcore_quota_used_ratio",
        "Share of today's YouTube API quota used per Google project",
        [({"project": p}, v) for p, v in sorted(snap.quota_used_ratio.items())],
    )
    metric(
        "ytcore_uploads_failed_1h",
        "Failed uploads in the last hour",
        [({}, snap.uploads_failed_1h)],
    )
    metric("ytcore_alerts_24h", "Alerts raised in the last 24 hours", [({}, snap.open_alerts_24h)])
    return "\n".join(lines) + "\n"


def collect_dict(connect: Connect) -> dict[str, Any]:  # convenience for the CLI
    from dataclasses import asdict

    return asdict(collect(connect))
