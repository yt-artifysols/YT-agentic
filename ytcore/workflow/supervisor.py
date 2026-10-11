"""Cluster supervisor: one long-lived workflow per cluster that fans the day's jobs out.

The planner hands each planned job to its cluster's supervisor (signal-with-start). The
supervisor starts the job's VideoJobWorkflow as a child when the cluster has room (max
concurrent jobs, daily budget), most urgent deadline first, and reports the cluster's health.
It never approves or publishes anything: the children run the normal workflow with the normal
approval gate. Per-pool fair share is enforced separately, per stage, by the pool limiter.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from ytcore.workflow.models import VideoJobInput, VideoJobResult
    from ytcore.workflow.video_job import VideoJobWorkflow, workflow_id_for


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    channel_id: str
    approval_deadline_epoch: float | None = None
    approval_timeout_seconds: int = 48 * 3600


@dataclass(frozen=True)
class SupervisorInput:
    cluster_id: str
    idle_seconds: int = 36 * 3600  # finish after this long with nothing to do
    recheck_seconds: int = 300  # how often to look again when held by the budget


@dataclass(frozen=True)
class ClusterLimits:
    max_concurrent: int
    budget_remaining_cents: int | None  # None = no cluster budget
    paused: bool = False


@dataclass
class ClusterHealth:
    cluster_id: str
    pending: int = 0
    running: int = 0  # jobs in production (the cluster's concurrency cap applies to these)
    awaiting_human: int = 0
    finished: int = 0
    failed: int = 0
    budget_held: bool = False
    paused: bool = False
    oldest_pending_deadline: float | None = None
    outcomes: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SupervisorResult:
    cluster_id: str
    started: int
    outcomes: dict[str, int]


class ClusterActivities:
    """Database access for supervisors; the callables are injected (and faked in tests)."""

    def __init__(
        self,
        limits_fn: Callable[[str], ClusterLimits],
        report_fn: Callable[[ClusterHealth], None],
    ) -> None:
        self._limits, self._report = limits_fn, report_fn

    @activity.defn
    async def cluster_limits(self, cluster_id: str) -> ClusterLimits:
        return await asyncio.to_thread(self._limits, cluster_id)

    @activity.defn
    async def report_cluster_health(self, health: ClusterHealth) -> None:
        await asyncio.to_thread(self._report, health)

    def all(self) -> list[Callable[..., Any]]:
        return [self.cluster_limits, self.report_cluster_health]


_TIMEOUT = timedelta(minutes=2)
_RETRY = RetryPolicy(maximum_attempts=3)


@workflow.defn
class ClusterSupervisorWorkflow:
    def __init__(self) -> None:
        self._pending: dict[str, JobSpec] = {}
        self._known: set[str] = set()
        self._producing: set[str] = set()  # started, not yet waiting for a human
        self._running = 0  # started, not yet finished (producing + awaiting a human)
        self._started = 0
        self._outcomes: dict[str, int] = {}
        self._health = ClusterHealth("")
        self._dirty = True
        self._tasks: set[asyncio.Future[None]] = set()

    @workflow.signal
    def enqueue(self, spec: JobSpec) -> None:
        if spec.job_id not in self._known:  # the planner may re-send after a crash
            self._known.add(spec.job_id)
            self._pending[spec.job_id] = spec
            self._dirty = True

    @workflow.signal
    def produced(self, job_id: str) -> None:
        """A job finished production and now waits for a human: its slot is free again."""
        if job_id in self._producing:
            self._producing.discard(job_id)
            self._dirty = True

    @workflow.query
    def health(self) -> ClusterHealth:
        return self._snapshot()

    def _snapshot(self) -> ClusterHealth:
        h = self._health
        h.pending, h.running = len(self._pending), len(self._producing)
        h.awaiting_human = self._running - len(self._producing)
        h.finished = sum(v for k, v in self._outcomes.items() if k == "live")
        h.failed = sum(v for k, v in self._outcomes.items() if k in ("failed", "error"))
        h.outcomes = dict(self._outcomes)
        deadlines = [
            s.approval_deadline_epoch
            for s in self._pending.values()
            if s.approval_deadline_epoch is not None
        ]
        h.oldest_pending_deadline = min(deadlines) if deadlines else None
        return h

    async def _run_child(self, spec: JobSpec, child: object) -> None:
        try:
            result: VideoJobResult = await child  # type: ignore[misc]
            key = result.final_state
        except Exception:
            key = "error"
        self._outcomes[key] = self._outcomes.get(key, 0) + 1
        self._running -= 1
        self._producing.discard(spec.job_id)
        self._dirty = True

    @workflow.run
    async def run(self, inp: SupervisorInput) -> SupervisorResult:
        self._health = ClusterHealth(inp.cluster_id)
        idle_since = workflow.now()
        while True:
            limits: ClusterLimits | None = None
            if self._pending:
                limits = await workflow.execute_activity_method(
                    ClusterActivities.cluster_limits,
                    inp.cluster_id,
                    start_to_close_timeout=_TIMEOUT,
                    retry_policy=_RETRY,
                )
            started_one = False
            if limits and not limits.paused and len(self._producing) < limits.max_concurrent:
                budget_ok = (
                    limits.budget_remaining_cents is None or limits.budget_remaining_cents > 0
                )
                self._health.budget_held = not budget_ok
                if budget_ok:
                    urgent = min(
                        self._pending.values(),
                        key=lambda s: (s.approval_deadline_epoch or float("inf"), s.job_id),
                    )
                    del self._pending[urgent.job_id]
                    handle = await workflow.start_child_workflow(
                        VideoJobWorkflow.run,
                        VideoJobInput(
                            job_id=urgent.job_id,
                            channel_id=urgent.channel_id,
                            origin="planner",
                            approval_timeout_seconds=urgent.approval_timeout_seconds,
                            approval_deadline_epoch=urgent.approval_deadline_epoch,
                            supervisor_id=workflow.info().workflow_id,
                        ),
                        id=workflow_id_for(urgent.job_id),
                        parent_close_policy=ParentClosePolicy.ABANDON,
                    )
                    self._running += 1
                    self._producing.add(urgent.job_id)
                    self._started += 1
                    self._tasks.add(asyncio.ensure_future(self._run_child(urgent, handle)))
                    started_one = True
                    idle_since = workflow.now()
            elif limits is not None:
                self._health.paused = limits.paused
            if self._dirty:
                self._dirty = False
                await workflow.execute_activity_method(
                    ClusterActivities.report_cluster_health,
                    self._snapshot(),
                    start_to_close_timeout=_TIMEOUT,
                    retry_policy=_RETRY,
                )
            if started_one:
                continue  # fill the cluster up before waiting
            if not self._pending and not self._running:
                if workflow.now() - idle_since >= timedelta(seconds=inp.idle_seconds):
                    return SupervisorResult(inp.cluster_id, self._started, dict(self._outcomes))
            else:
                idle_since = workflow.now()
            wait = timedelta(seconds=inp.recheck_seconds if self._pending else inp.idle_seconds)
            with contextlib.suppress(TimeoutError):
                await workflow.wait_condition(lambda: self._dirty, timeout=wait)


__all__ = [
    "ClusterActivities",
    "ClusterHealth",
    "ClusterLimits",
    "ClusterSupervisorWorkflow",
    "JobSpec",
    "SupervisorInput",
    "SupervisorResult",
]
