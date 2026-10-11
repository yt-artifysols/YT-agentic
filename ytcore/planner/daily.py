"""Daily planner: one job per channel per day, with a brief budget and a publish slot.

Idempotent by construction: a plan is keyed on (channel, local publish day, attempt) and the job
and its plan are written in one transaction under an advisory lock, so re-running the planner
never duplicates jobs. Starting the workflow is idempotent too (workflow id = job id), and plans
whose workflow was never started (crash between the two steps) are healed on the next run.

Slot rules, per channel: the channel's local publish hour in its audience time zone; channels of
one cluster are staggered by the cluster's slot spacing so a cluster never publishes in the same
minute; the approval deadline is `approval_lead_minutes` before the slot. A job that misses its
deadline is cancelled by the workflow and the plan is replaced by a new attempt on the next free
day (never published late, never automatically).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import psycopg

from ytcore.store.errors import StoreError
from ytcore.store.postgres import Connect, map_db_error

log = logging.getLogger(__name__)
ACTOR = "planner"
MAX_ATTEMPTS = 4  # the original plan plus three replans


@dataclass(frozen=True)
class ChannelSchedule:
    channel_id: str
    cluster_id: str | None
    timezone: str
    publish_hour: int
    approval_lead_minutes: int
    cluster_rank: int  # position among the cluster's channels (sorted by id)
    slot_spacing_minutes: int
    job_budget_cents: int
    daily_budget_cents: int
    window_minutes: int = 0  # >0: the slot is drawn from [hour, hour + window) per day


@dataclass(frozen=True)
class Slot:
    slot_at: datetime  # UTC
    slot_date: date  # channel-local date of the slot
    approval_deadline: datetime  # UTC


def target_date(now: datetime, tz: str) -> date:
    """The channel-local day the planner prepares for: tomorrow, as seen by the channel."""
    return (now.astimezone(ZoneInfo(tz)) + timedelta(days=1)).date()


def compute_slot(s: ChannelSchedule, on: date) -> Slot:
    """Publish slot for local day `on`: local publish hour plus this channel's cluster offset."""
    zone = ZoneInfo(s.timezone)
    offset = s.cluster_rank * s.slot_spacing_minutes
    local = datetime.combine(on, time(s.publish_hour), tzinfo=zone) + timedelta(minutes=offset)
    slot_at = local.astimezone(UTC)
    return Slot(
        slot_at=slot_at,
        slot_date=local.date(),
        approval_deadline=slot_at - timedelta(minutes=s.approval_lead_minutes),
    )


def jitter_minutes(channel_id: str, on: date, window: int) -> int:
    """Deterministic per (channel, day) offset into the publish window: it differs between
    channels and between days, so no two channels share a posting pattern, yet re-running the
    planner yields the same answer."""
    digest = hashlib.sha256(f"{channel_id}|{on.isoformat()}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % max(window, 1)


def candidate_slot(s: ChannelSchedule, on: date) -> Slot:
    """Where the channel would like to publish on local day `on`, before conflicts."""
    if s.window_minutes <= 0:
        return compute_slot(s, on)
    zone = ZoneInfo(s.timezone)
    start = datetime.combine(on, time(s.publish_hour), tzinfo=zone)
    local = start + timedelta(minutes=jitter_minutes(s.channel_id, on, s.window_minutes))
    slot_at = local.astimezone(UTC)
    return Slot(
        slot_at=slot_at,
        slot_date=local.date(),
        approval_deadline=slot_at - timedelta(minutes=s.approval_lead_minutes),
    )


def allocate_slots(
    items: list[tuple[ChannelSchedule, date]],
    taken: Sequence[tuple[str | None, datetime]] = (),
) -> dict[str, Slot]:
    """Slots for several channels at once.

    Rules: a channel publishes inside its own audience window; channels of a cluster are at
    least `slot_spacing_minutes` apart; no two channels in the fleet publish in the same minute
    (identical patterns look automated); `taken` are slots already planned (kept as they are).
    Fixed-hour channels (window 0) keep their rank-staggered slot and are placed first."""
    by_cluster: dict[str | None, list[datetime]] = {}
    minutes: set[datetime] = set()
    for cluster, at in taken:
        by_cluster.setdefault(cluster, []).append(at)
        minutes.add(at.replace(second=0, microsecond=0))
    out: dict[str, Slot] = {}

    def place(s: ChannelSchedule, slot: Slot) -> None:
        out[s.channel_id] = slot
        by_cluster.setdefault(s.cluster_id, []).append(slot.slot_at)
        minutes.add(slot.slot_at.replace(second=0, microsecond=0))

    def free(s: ChannelSchedule, at: datetime) -> bool:
        gap = timedelta(minutes=max(s.slot_spacing_minutes, 1))
        if at.replace(second=0, microsecond=0) in minutes:
            return False
        return all(abs(at - other) >= gap for other in by_cluster.get(s.cluster_id, []))

    ordered = sorted(
        items,
        key=lambda it: (it[0].window_minutes > 0, candidate_slot(*it).slot_at, it[0].channel_id),
    )
    for s, on in ordered:
        cand = candidate_slot(s, on)
        if s.window_minutes <= 0:
            place(s, cand)
            continue
        zone = ZoneInfo(s.timezone)
        start = datetime.combine(on, time(s.publish_hour), tzinfo=zone).astimezone(UTC)
        offset = int((cand.slot_at - start).total_seconds() // 60)
        # nearest free minute at or after the candidate, wrapping inside the window, then beyond
        order = [*range(offset, s.window_minutes), *range(0, offset)]
        order += range(s.window_minutes, s.window_minutes + 24 * 60)
        for minute in order:
            at = start + timedelta(minutes=minute)
            if free(s, at):
                local = at.astimezone(zone)
                place(s, Slot(at, local.date(), at - timedelta(minutes=s.approval_lead_minutes)))
                break
        else:  # pragma: no cover - 24h of conflicts is not a real case
            place(s, cand)
    return out


@dataclass(frozen=True)
class PlannedJob:
    plan_id: str
    job_id: str
    channel_id: str
    plan_date: date
    attempt: int
    slot: Slot
    budget_cents: int
    created: bool


@dataclass(frozen=True)
class Skipped:
    channel_id: str
    reason: str


@dataclass
class PlanRun:
    planned: list[PlannedJob]
    skipped: list[Skipped]
    replanned: list[PlannedJob]
    exhausted: list[str]
    started: list[str]


class WorkflowStarter(Protocol):
    def start(
        self,
        job_id: str,
        channel_id: str,
        approval_timeout_seconds: int,
        approval_deadline: datetime | None = None,
        cluster_id: str | None = None,
    ) -> None:
        """Start the job's workflow (through its cluster's supervisor when there is one) with its
        ABSOLUTE approval deadline. Must be idempotent for a job id that already runs."""
        ...


@dataclass
class FakeWorkflowStarter:
    started: list[tuple[str, str, int]]

    def __init__(self) -> None:
        self.started = []

    def start(
        self,
        job_id: str,
        channel_id: str,
        approval_timeout_seconds: int,
        approval_deadline: datetime | None = None,
        cluster_id: str | None = None,
    ) -> None:
        if all(j != job_id for j, _, _ in self.started):
            self.started.append((job_id, channel_id, approval_timeout_seconds))


class PlanRepository:
    """Postgres access for the planner. Every write sets app.actor and is audited by triggers."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    @classmethod
    def from_conninfo(cls, conninfo: str) -> PlanRepository:
        return cls(lambda: psycopg.connect(conninfo, autocommit=True))

    @contextmanager
    def _tx(self) -> Iterator[psycopg.Connection[Any]]:
        try:
            with self._connect() as conn, conn.transaction():
                conn.execute("SELECT set_config('app.actor', %s, true)", (ACTOR,))
                yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    def schedules(self) -> tuple[list[ChannelSchedule], list[Skipped]]:
        """Plannable channels (active, in an active cluster) and the ones skipped, with why."""
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT c.id, c.cluster_id, c.audience_timezone, c.publish_hour_local, "
                "c.approval_lead_minutes, coalesce(k.slot_spacing_minutes, 30), "
                "c.job_budget_cents, "
                "c.daily_budget_cents, c.status, coalesce(k.status, 'active'), c.autonomy_tier, "
                "rank() OVER (PARTITION BY c.cluster_id ORDER BY c.id) - 1, "
                "c.publish_window_minutes "
                "FROM channels c LEFT JOIN clusters k ON k.id = c.cluster_id ORDER BY c.id"
            ).fetchall()
            paused_globally = conn.execute(
                "SELECT coalesce(publishing_paused, true) FROM global_controls"
            ).fetchone()
        plannable, skipped = [], []
        for r in rows:
            if paused_globally is None or paused_globally[0]:
                skipped.append(Skipped(r[0], "global kill switch engaged"))
            elif r[8] != "active":
                skipped.append(Skipped(r[0], "channel paused"))
            elif r[9] != "active":
                skipped.append(Skipped(r[0], "cluster paused"))
            elif r[10] != "gated":
                skipped.append(Skipped(r[0], "channel is in shadow mode"))
            else:
                plannable.append(
                    ChannelSchedule(
                        channel_id=r[0],
                        cluster_id=r[1],
                        timezone=r[2],
                        publish_hour=int(r[3]),
                        approval_lead_minutes=int(r[4]),
                        cluster_rank=int(r[11]),
                        slot_spacing_minutes=int(r[5]),
                        job_budget_cents=int(r[6]),
                        daily_budget_cents=int(r[7]),
                        window_minutes=int(r[12]),
                    )
                )
        return plannable, skipped

    def create_plan(
        self,
        s: ChannelSchedule,
        plan_date: date,
        attempt: int,
        slot: Slot,
        budget: int,
        origin: str = "planner",
    ) -> tuple[str, str, bool] | None:
        """(plan_id, job_id, created), or None when the slot day is already taken by another
        live plan. Idempotent for (channel, plan_date, attempt)."""
        with self._tx() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"plan:{s.channel_id}:{plan_date}:{attempt}",),
            )
            row = conn.execute(
                "SELECT id::text, job_id::text FROM daily_plans "
                "WHERE channel_id = %s AND plan_date = %s AND attempt = %s",
                (s.channel_id, plan_date, attempt),
            ).fetchone()
            if row is not None:
                return row[0], row[1], False
            taken = conn.execute(
                "SELECT 1 FROM daily_plans WHERE channel_id = %s AND slot_date = %s "
                "AND status IN ('active', 'replan_requested', 'closed')",
                (s.channel_id, slot.slot_date),
            ).fetchone()
            if taken is not None:
                return None
            job = conn.execute(
                "INSERT INTO video_jobs (channel_id, origin, approval_deadline) "
                "VALUES (%s, %s, %s) RETURNING id::text",
                (s.channel_id, origin, slot.approval_deadline),
            ).fetchone()
            assert job is not None
            plan = conn.execute(
                "INSERT INTO daily_plans (channel_id, plan_date, attempt, job_id, slot_at, "
                "slot_date, approval_deadline, budget_cents) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text",
                (s.channel_id, plan_date, attempt, job[0], slot.slot_at, slot.slot_date,
                 slot.approval_deadline, budget),
            ).fetchone()  # fmt: skip
            assert plan is not None
            return plan[0], job[0], True

    def taken_slots(self, around: datetime) -> list[tuple[str | None, datetime, str, date]]:
        """Slots of live plans near `around` (cluster, instant, channel, plan_date)."""
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT c.cluster_id, p.slot_at, p.channel_id, p.plan_date FROM daily_plans p "
                "JOIN channels c ON c.id = p.channel_id WHERE p.status <> 'replanned' "
                "AND p.slot_at BETWEEN %s AND %s",
                (around - timedelta(days=3), around + timedelta(days=10)),
            ).fetchall()
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    def slot_taken(self, channel_id: str, slot_date: date) -> bool:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT 1 FROM daily_plans WHERE channel_id = %s AND slot_date = %s "
                "AND status IN ('active', 'replan_requested', 'closed')",
                (channel_id, slot_date),
            ).fetchone()
        return row is not None

    def unstarted(self) -> list[tuple[str, str, str, datetime, str | None]]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT p.id::text, p.job_id::text, p.channel_id, p.approval_deadline, "
                "c.cluster_id "
                "FROM daily_plans p JOIN channels c ON c.id = p.channel_id "
                "WHERE p.workflow_started_at IS NULL AND p.status = 'active' ORDER BY p.slot_at"
            ).fetchall()
        return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]

    def mark_started(self, plan_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE daily_plans SET workflow_started_at = coalesce(workflow_started_at, now()) "
                "WHERE id = %s",
                (plan_id,),
            )

    def request_replan(self, job_id: str, reason: str) -> bool:
        """Mark the plan of a CANCELLED job for replacement. No-op for jobs without a plan (and
        for jobs that are not cancelled: only cancellation is ever re-planned)."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE daily_plans p SET status = 'replan_requested', replan_reason = %s "
                "FROM video_jobs j WHERE j.id = p.job_id AND p.job_id = %s "
                "AND p.status IN ('active', 'closed') AND j.state = 'cancelled'",
                (reason[:500], job_id),
            )
            return cur.rowcount == 1

    def pending_replans(self) -> list[tuple[str, str, date, int, str]]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT id::text, channel_id, plan_date, attempt, coalesce(replan_reason, '') "
                "FROM daily_plans WHERE status = 'replan_requested' ORDER BY slot_at"
            ).fetchall()
        return [(r[0], r[1], r[2], int(r[3]), r[4]) for r in rows]

    def set_status(self, plan_id: str, status: str) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE daily_plans SET status = %s WHERE id = %s", (status, plan_id))

    def close_finished(self) -> int:
        """Close active plans whose job reached a terminal state (done, or ended otherwise)."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE daily_plans p SET status = 'closed' FROM video_jobs j "
                "JOIN job_states s ON s.state = j.state "
                "WHERE j.id = p.job_id AND p.status = 'active' AND s.is_terminal "
                "AND j.state <> 'cancelled'"
            )
            return cur.rowcount

    def alert(self, channel_id: str, kind: str, message: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO alerts (channel_id, kind, message) VALUES (%s, %s, %s)",
                (channel_id, kind, message[:2000]),
            )


class DailyPlanner:
    def __init__(
        self,
        repo: PlanRepository,
        starter: WorkflowStarter,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repo = repo
        self._starter = starter
        self._clock = clock

    @staticmethod
    def _budget(s: ChannelSchedule) -> int:
        return min(s.job_budget_cents, s.daily_budget_cents)

    def _timeout(self, deadline: datetime, now: datetime) -> int:
        return max(60, int((deadline - now).total_seconds()))

    def run(self, now: datetime | None = None) -> PlanRun:
        """Plan tomorrow (channel-local) for every plannable channel; replace missed plans."""
        now = now or self._clock()
        schedules, skipped = self._repo.schedules()
        self._repo.close_finished()
        run = PlanRun([], list(skipped), [], [], [])
        taken = self._repo.taken_slots(now)
        wanted = [(s, target_date(now, s.timezone)) for s in schedules]
        # Slots already planned for these channel-days are kept; the others are allocated around
        # them together, so the fleet's slots never collide.
        mine = {(ch, d) for _, _, ch, d in taken}
        allocation = allocate_slots(
            [(s, d) for s, d in wanted if (s.channel_id, d) not in mine],
            [(cl, at) for cl, at, _, _ in taken],
        )
        for s, day in wanted:
            slot = allocation.get(s.channel_id) or self._existing_slot(s, day, taken)
            if slot is None:
                continue
            if slot.approval_deadline <= now:
                run.skipped.append(Skipped(s.channel_id, "approval deadline already passed"))
                continue
            try:
                made = self._repo.create_plan(s, day, 1, slot, self._budget(s))
            except StoreError:  # lost a race with a replan for this day: next run sorts it out
                run.skipped.append(Skipped(s.channel_id, "plan conflict, retried next run"))
                continue
            if made is None:
                run.skipped.append(Skipped(s.channel_id, "slot day taken by a replanned job"))
                continue
            plan_id, job_id, created = made
            run.planned.append(
                PlannedJob(plan_id, job_id, s.channel_id, day, 1, slot, self._budget(s), created)
            )
        self._replan(run, {s.channel_id: s for s in schedules}, now)
        self._start_pending(run, now)
        return run

    @staticmethod
    def _existing_slot(
        s: ChannelSchedule, day: date, taken: list[tuple[str | None, datetime, str, date]]
    ) -> Slot | None:
        for _, at, channel, plan_date in taken:
            if channel == s.channel_id and plan_date == day:
                local = at.astimezone(ZoneInfo(s.timezone))
                return Slot(at, local.date(), at - timedelta(minutes=s.approval_lead_minutes))
        return None

    def _replan(self, run: PlanRun, by_channel: dict[str, ChannelSchedule], now: datetime) -> None:
        for plan_id, channel_id, plan_date, attempt, reason in self._repo.pending_replans():
            s = by_channel.get(channel_id)
            if s is None:
                continue  # paused: leave the request pending until the channel is back
            if attempt >= MAX_ATTEMPTS:
                self._repo.set_status(plan_id, "exhausted")
                self._repo.alert(
                    channel_id, "plan.exhausted",
                    f"no approval for {plan_date} after {attempt} attempts; needs an operator",
                )  # fmt: skip
                run.exhausted.append(channel_id)
                continue
            day = target_date(now, s.timezone)
            slot = compute_slot(s, day)
            while (
                self._repo.slot_taken(channel_id, slot.slot_date) or slot.approval_deadline <= now
            ):
                day += timedelta(days=1)
                slot = compute_slot(s, day)
            try:
                made = self._repo.create_plan(s, plan_date, attempt + 1, slot, self._budget(s))
            except StoreError:
                continue
            if made is None:  # lost a race for the day: try again on the next run
                continue
            new_id, job_id, created = made
            self._repo.set_status(plan_id, "replanned")
            run.replanned.append(PlannedJob(new_id, job_id, channel_id, plan_date, attempt + 1,
                                            slot, self._budget(s), created))  # fmt: skip
            log.info("replanned %s for %s: %s", channel_id, plan_date, reason)

    def _start_pending(self, run: PlanRun, now: datetime) -> None:
        for plan_id, job_id, channel_id, deadline, cluster_id in self._repo.unstarted():
            self._starter.start(
                job_id, channel_id, self._timeout(deadline, now), deadline, cluster_id
            )
            self._repo.mark_started(plan_id)
            run.started.append(job_id)
