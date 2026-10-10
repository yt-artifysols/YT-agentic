"""Operational alert checks: stuck jobs, budget breaches, gateway failures, quota, missing plans.

`OpsMonitor.check` is read-only apart from inserting `alerts` rows (audited by the database),
de-duplicated per (kind, subject) within a window so a stuck job pages once, not every minute.
Run it on a schedule: `python -m ytcore.observability.monitor check`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ytcore.store.postgres import Connect, map_db_error

log = logging.getLogger(__name__)
ACTOR = "monitor"


@dataclass(frozen=True)
class MonitorThresholds:
    # Longest a job may sit in an automated state before it counts as stuck.
    working_state_minutes: int = 45
    approved_state_minutes: int = 20
    publishing_minutes: int = 150
    published_minutes: int = 60
    # An awaiting_approval job is stuck when it is this far past its approval deadline: the
    # workflow should have cancelled it.
    past_deadline_minutes: int = 20
    budget_warn_ratio: float = 0.9
    quota_warn_ratio: float = 0.8
    failed_uploads_1h: int = 1
    dedupe_hours: int = 6
    # Local hour by which tomorrow's plan must exist for every active gated channel.
    plan_due_hour_utc: int = 20


@dataclass(frozen=True)
class Finding:
    kind: str
    subject: str  # job:<id> | channel:<id> | project:<id>
    channel_id: str | None
    job_id: str | None
    message: str


WORKING = ("planned", "researching", "scripting", "rendering", "qa")


class OpsMonitor:
    def __init__(self, connect: Connect, thresholds: MonitorThresholds | None = None) -> None:
        self._connect = connect
        self._t = thresholds or MonitorThresholds()

    @contextmanager
    def _tx(self) -> Iterator[psycopg.Connection[Any]]:
        try:
            with self._connect() as conn, conn.transaction():
                conn.execute("SELECT set_config('app.actor', %s, true)", (ACTOR,))
                yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    def findings(self, now: datetime | None = None) -> list[Finding]:
        now = now or datetime.now(UTC)
        t, out = self._t, []
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT j.id::text, j.channel_id, j.state, j.approval_deadline, "
                "coalesce((SELECT max(at) FROM job_events e WHERE e.job_id = j.id), j.created_at) "
                "FROM video_jobs j JOIN job_states s ON s.state = j.state WHERE NOT s.is_terminal"
            ).fetchall()
            for job_id, channel, state, deadline, since in rows:
                age = now - since
                limit: timedelta | None = None
                if state in WORKING:
                    limit = timedelta(minutes=t.working_state_minutes)
                elif state in ("approved", "scheduled"):
                    limit = timedelta(minutes=t.approved_state_minutes)
                elif state == "publishing":
                    limit = timedelta(minutes=t.publishing_minutes)
                elif state == "published":
                    limit = timedelta(minutes=t.published_minutes)
                elif state == "awaiting_approval" and deadline is not None:
                    if now - deadline > timedelta(minutes=t.past_deadline_minutes):
                        out.append(
                            Finding(
                                "job.stuck",
                                f"job:{job_id}",
                                channel,
                                job_id,
                                f"awaiting approval {now - deadline} past its deadline "
                                "(workflow should have cancelled it)",
                            )
                        )
                    continue
                if limit is not None and age > limit:
                    out.append(
                        Finding(
                            "job.stuck",
                            f"job:{job_id}",
                            channel,
                            job_id,
                            f"in {state} for {age} (limit {limit})",
                        )
                    )
            for channel, spent, cap in conn.execute(
                "SELECT c.id, coalesce(sum(k.cost_cents), 0), c.daily_budget_cents FROM channels c "
                "LEFT JOIN job_costs k ON k.channel_id = c.id AND k.cost_date = "
                "(now() AT TIME ZONE 'UTC')::date WHERE c.daily_budget_cents > 0 "
                "GROUP BY c.id, c.daily_budget_cents"
            ).fetchall():
                if cap and spent >= cap * t.budget_warn_ratio:
                    out.append(
                        Finding(
                            "budget.breach" if spent >= cap else "budget.warning",
                            f"channel:{channel}",
                            channel,
                            None,
                            f"{spent}/{cap} cents of today's budget used",
                        )
                    )
            n_over = conn.execute("SELECT count(*) FROM job_costs WHERE over_cap").fetchone()
            if n_over and n_over[0]:
                out.append(
                    Finding(
                        "budget.overrun",
                        "platform",
                        None,
                        None,
                        f"{n_over[0]} cost rows crossed a cap",
                    )
                )
            for project, used, cap in conn.execute(
                "SELECT g.id, coalesce(sum(q.units), 0), g.daily_quota_units "
                "FROM google_projects g "
                "LEFT JOIN quota_ledger q ON q.google_project_id = g.id "
                "AND q.usage_date = youtube_quota_day() GROUP BY g.id, g.daily_quota_units"
            ).fetchall():
                if cap and used >= cap * t.quota_warn_ratio:
                    out.append(
                        Finding(
                            "quota.high",
                            f"project:{project}",
                            None,
                            None,
                            f"{used}/{cap} quota units used today",
                        )
                    )
            for upload_id, channel, job_id, reason in conn.execute(
                "SELECT id::text, channel_id, job_id::text, coalesce(failure_reason, '?') "
                "FROM uploads WHERE status = 'failed' AND updated_at > %s",
                (now - timedelta(hours=1),),
            ).fetchall():
                out.append(
                    Finding(
                        "gateway.failure",
                        f"upload:{upload_id}",
                        channel,
                        job_id,
                        f"upload failed: {reason}",
                    )
                )
            if now.hour >= t.plan_due_hour_utc:
                tomorrow = (now + timedelta(days=1)).date()
                for (channel,) in conn.execute(
                    "SELECT c.id FROM channels c LEFT JOIN clusters k ON k.id = c.cluster_id "
                    "WHERE c.status = 'active' AND coalesce(k.status, 'active') = 'active' "
                    "AND c.autonomy_tier = 'gated' AND NOT EXISTS (SELECT 1 FROM daily_plans p "
                    "WHERE p.channel_id = c.id AND p.slot_date <= %s AND p.slot_at > %s)",
                    (tomorrow + timedelta(days=1), now),
                ).fetchall():
                    out.append(
                        Finding(
                            "plan.missing",
                            f"channel:{channel}",
                            channel,
                            None,
                            "no upcoming plan: the daily planner did not run",
                        )
                    )
        return out

    def check(self, now: datetime | None = None) -> list[Finding]:
        """Record alerts for new findings; return the ones that were newly raised."""
        now = now or datetime.now(UTC)
        raised: list[Finding] = []
        with self._tx() as conn:
            for f in self.findings(now):
                recent = conn.execute(
                    "SELECT 1 FROM alerts WHERE kind = %s AND message LIKE %s AND created_at > %s",
                    (f.kind, f"[{f.subject}]%", now - timedelta(hours=self._t.dedupe_hours)),
                ).fetchone()
                if recent is not None:
                    continue
                conn.execute(
                    "INSERT INTO alerts (channel_id, job_id, kind, message) "
                    "VALUES (%s, %s, %s, %s)",
                    (f.channel_id, f.job_id, f.kind, f"[{f.subject}] {f.message}"[:2000]),
                )
                raised.append(f)
        return raised


Notify = Callable[[str, str], None]


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI
    parser = argparse.ArgumentParser(prog="python -m ytcore.observability.monitor")
    parser.add_argument("cmd", choices=["check"])
    parser.parse_args(argv)
    from ytcore.config import build_secrets_provider, load_settings
    from ytcore.safety.scrub import install_log_scrubber
    from ytcore.secrets.provider import SecretRef

    install_log_scrubber()
    core = load_settings()
    dsn = build_secrets_provider(core).get(SecretRef(core.database_url_ref)).reveal()
    raised = OpsMonitor(lambda: psycopg.connect(dsn, autocommit=True)).check()
    for f in raised:
        print(f"{f.kind} {f.subject}: {f.message}")
    print(f"{len(raised)} new alert(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
