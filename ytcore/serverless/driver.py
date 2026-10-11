"""Database-driven job driver: advances jobs a bounded number of steps per invocation (cron),
replacing the Temporal workflow on hosts with no long-running worker (staging).

It follows VideoJobWorkflow step for step and reuses the same activities, so every rule is
enforced where it always was (DB triggers, gateway, the hub-recorded approval):
  * nothing is published without an approval the HUB already recorded (`approve` accepts only
    that), and the gateway re-verifies the token;
  * a missed approval deadline cancels the job and asks for a re-plan;
  * a decision about content that is no longer current is ignored.
State lives in the database: the job's state column says where to resume, `driver_state` holds
the artifacts, and a lease row keeps two overlapping invocations off the same job."""

from __future__ import annotations

import logging
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg
from temporalio.exceptions import ApplicationError

from ytcore.workflow.activities import JobActivities
from ytcore.workflow.models import QAReport
from ytcore.workflow.states import JobState

log = logging.getLogger(__name__)

LEASE_SECONDS = 240
MAX_ATTEMPTS = 5
MAX_QA_REWORK = 2
APPROVAL_TIMEOUT_SECONDS = 48 * 3600
_ACTIVE = (
    "planned", "researching", "scripting", "rendering", "qa",
    "awaiting_approval", "approved", "scheduled", "publishing",
)  # fmt: skip
_STAGE_STATE = {
    "research": JobState.RESEARCHING,
    "script": JobState.SCRIPTING,
    "render": JobState.RENDERING,
}


@dataclass
class TickResult:
    examined: int = 0
    advanced: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped_locked: int = 0


@dataclass(frozen=True)
class _Job:
    id: str
    channel_id: str
    state: JobState
    content_hash: str | None
    deadline: datetime | None


@dataclass(frozen=True)
class _Decision:
    id: int
    action: str
    content_hash: str
    token: str | None
    rework_stage: str | None
    reason: str


class _Retry(Exception):
    """A transient problem: leave the job as it is and try again on the next tick."""


class JobDriver:
    def __init__(self, activities: JobActivities, conninfo: str) -> None:
        self._a = activities
        self._conninfo = conninfo

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self._conninfo, autocommit=True)

    # ------------------------------------------------------------------ db helpers
    def _job(self, job_id: str) -> _Job:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, channel_id, state, content_hash, approval_deadline "
                "FROM video_jobs WHERE id = %s",
                (job_id,),
            ).fetchone()
        assert row is not None
        return _Job(str(row[0]), row[1], JobState(row[2]), row[3], row[4])

    def _claim(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO driver_state (job_id) VALUES (%s) ON CONFLICT DO NOTHING", (job_id,)
            )
            row = conn.execute(
                "UPDATE driver_state SET locked_until = now() + make_interval(secs => %s), "
                "updated_at = now() WHERE job_id = %s AND locked_until < now() "
                "RETURNING artifacts, rework_count, attempts",
                (LEASE_SECONDS, job_id),
            ).fetchone()
        if row is None:
            return None
        return {"artifacts": dict(row[0]), "rework": row[1], "attempts": row[2]}

    def _save(self, job_id: str, artifacts: dict[str, str], rework: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE driver_state SET artifacts = %s::jsonb, rework_count = %s, "
                "updated_at = now() WHERE job_id = %s",
                (psycopg.types.json.Jsonb(artifacts), rework, job_id),
            )

    def _release(self, job_id: str, *, failed: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE driver_state SET locked_until = '-infinity', "
                "attempts = CASE WHEN %s THEN attempts + 1 ELSE 0 END, updated_at = now() "
                "WHERE job_id = %s",
                (failed, job_id),
            )

    def _decisions(self, job_id: str) -> list[_Decision]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, action, content_hash, token, rework_stage, reason "
                "FROM workflow_decisions WHERE job_id = %s AND consumed_at IS NULL ORDER BY id",
                (job_id,),
            ).fetchall()
        return [_Decision(*r) for r in rows]

    def _consume(self, decision_id: int) -> None:
        with self._connect() as conn:  # the guard trigger erases the token with the update
            conn.execute(
                "UPDATE workflow_decisions SET consumed_at = now() WHERE id = %s", (decision_id,)
            )

    # ------------------------------------------------------------------ steps
    async def _to(self, job_id: str, state: JobState) -> None:
        await self._a.transition(job_id, state.value)

    async def _produce(self, job: _Job, artifacts: dict[str, str], rework: int) -> int:
        """Run production from the job's current state until it is sealed for approval or ends.
        Returns the rework count."""
        while True:
            state = self._job(job.id).state
            if state is JobState.PLANNED:
                await self._to(job.id, JobState.RESEARCHING)
            elif state in (JobState.RESEARCHING, JobState.SCRIPTING, JobState.RENDERING):
                stage = {v: k for k, v in _STAGE_STATE.items()}[state]
                res = await self._a.run_stage(job.id, job.channel_id, stage, dict(artifacts))
                artifacts.update(res.artifacts)
                self._save(job.id, artifacts, rework)
                nxt = {"research": JobState.SCRIPTING, "script": JobState.RENDERING}.get(
                    stage, JobState.QA
                )
                await self._to(job.id, nxt)
            elif state is JobState.QA:
                report: QAReport = await self._a.run_qa(job.id, job.channel_id, dict(artifacts))
                if report.passed:
                    await self._a.seal_package(
                        job.id,
                        job.channel_id,
                        dict(artifacts),
                        APPROVAL_TIMEOUT_SECONDS,
                        job.deadline.timestamp() if job.deadline else None,
                    )
                    return rework
                rework += 1
                if rework > MAX_QA_REWORK:
                    await self._a.fail(job.id)
                    return rework
                self._save(job.id, artifacts, rework)
                back = (
                    JobState.SCRIPTING if report.rework_stage == "scripting" else JobState.RENDERING
                )
                await self._to(job.id, back)
            else:
                return rework

    async def _cancel_and_replan(self, job: _Job, reason: str) -> None:
        await self._to(job.id, JobState.CANCELLED)
        await self._a.request_replan(job.id, job.channel_id, reason)

    async def _after_approval(self, job: _Job, token: str) -> None:
        """approved -> scheduled -> publishing -> live, each step idempotent by job state."""
        state = self._job(job.id).state
        if state is JobState.APPROVED:
            try:
                await self._to(job.id, JobState.SCHEDULED)
            except ApplicationError as exc:
                # Paused channel/cluster or shadow tier: never publish; cancel and re-plan.
                await self._cancel_and_replan(job, f"not publishable: {exc.message}")
                return
            state = JobState.SCHEDULED
        if state in (JobState.SCHEDULED, JobState.PUBLISHING):
            await self._a.publish(job.id, token)

    async def _handle_decisions(self, job: _Job, artifacts: dict[str, str], rework: int) -> int:
        for d in self._decisions(job.id):
            current = self._job(job.id)
            if d.action != "edit" and d.content_hash != current.content_hash:
                self._consume(d.id)  # about content that is no longer current
                continue
            if d.action == "edit":
                report = await self._a.run_qa(job.id, job.channel_id, dict(artifacts))
                if not report.passed:
                    await self._a.fail(job.id)
                else:
                    await self._to(job.id, JobState.AWAITING_APPROVAL)
                self._consume(d.id)
            elif d.action == "reject":
                if d.rework_stage in (None, "discard"):
                    await self._to(job.id, JobState.REJECTED)
                    self._consume(d.id)
                else:
                    await self._to(job.id, _STAGE_STATE[d.rework_stage])
                    self._consume(d.id)
                    rework = await self._produce(self._job(job.id), artifacts, rework)
            elif d.action == "approve" and d.token:
                try:
                    await self._a.approve(job.id, d.token)
                except ApplicationError:
                    self._consume(d.id)  # invalid, replayed or mismatched token: ignore it
                    continue
                await self._after_approval(self._job(job.id), d.token)
                self._consume(d.id)  # only now: a crash above retries with the token intact
            if self._job(job.id).state not in (JobState.AWAITING_APPROVAL, JobState.QA):
                break
        return rework

    async def drive(self, job: _Job, artifacts: dict[str, str], rework: int) -> None:
        if job.state in (JobState.APPROVED, JobState.SCHEDULED, JobState.PUBLISHING):
            # A previous invocation stopped after the approval was recorded: finish the job from
            # the (still unconsumed) decision that carries its token.
            await self._finish_after_approval(job)
            return
        producing = job.state in (
            JobState.PLANNED, JobState.RESEARCHING, JobState.SCRIPTING, JobState.RENDERING,
        )  # fmt: skip
        editing = job.state is JobState.QA and any(
            d.action == "edit" for d in self._decisions(job.id)
        )
        if producing or (job.state is JobState.QA and not editing):
            rework = await self._produce(job, artifacts, rework)
            job = self._job(job.id)
        if job.state in (JobState.QA, JobState.AWAITING_APPROVAL):
            rework = await self._handle_decisions(job, artifacts, rework)
            job = self._job(job.id)
            if (
                job.state is JobState.AWAITING_APPROVAL
                and job.deadline is not None
                and datetime.now(UTC) >= job.deadline
            ):
                await self._cancel_and_replan(job, "approval deadline missed")
        self._save(job.id, artifacts, rework)

    async def _finish_after_approval(self, job: _Job) -> None:
        for d in self._decisions(job.id):
            if d.action == "approve" and d.token:
                await self._after_approval(job, d.token)
                self._consume(d.id)

    # ------------------------------------------------------------------ tick
    def _candidates(self, limit: int, only: Collection[str] | None) -> list[_Job]:
        ids = list(only) if only is not None else None
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT j.id, j.channel_id, j.state, j.content_hash, j.approval_deadline "
                "FROM video_jobs j JOIN channels c ON c.id = j.channel_id "
                "WHERE j.state = ANY(%s) AND (%s::uuid[] IS NULL OR j.id = ANY(%s::uuid[])) "
                "ORDER BY j.approval_deadline NULLS LAST, j.created_at LIMIT %s",
                (list(_ACTIVE), ids, ids, limit),
            ).fetchall()
        return [_Job(str(r[0]), r[1], JobState(r[2]), r[3], r[4]) for r in rows]

    async def _fail(self, job_id: str) -> bool:
        """Fail the job; one job that cannot be failed must never abort the whole tick."""
        try:
            await self._a.fail(job_id)
        except Exception as exc:
            log.error("could not fail job %s: %s", job_id, type(exc).__name__)
            return False
        for d in self._decisions(job_id):  # a failed job keeps no approval token around
            self._consume(d.id)
        return True

    async def tick(
        self,
        *,
        max_jobs: int = 20,
        budget_seconds: float = 200.0,
        only: Collection[str] | None = None,
    ) -> TickResult:
        result = TickResult()
        started = time.monotonic()
        for job in self._candidates(max_jobs, only):
            if time.monotonic() - started > budget_seconds:
                break
            result.examined += 1
            claimed = self._claim(job.id)
            if claimed is None:
                result.skipped_locked += 1
                continue
            failed = False
            try:
                await self.drive(self._job(job.id), claimed["artifacts"], claimed["rework"])
                result.advanced.append(job.id)
            except _Retry:
                failed = True
            except ApplicationError as exc:
                if exc.type == "UploadInProgress":  # retryable: a later tick sees the upload
                    failed = True
                    continue
                log.warning("job %s failed: %s: %s", job.id, exc.type, exc.message)
                failed = not await self._fail(job.id)
                result.failed.append(job.id)
            except Exception as exc:
                failed = True
                log.warning("job %s will retry: %s", job.id, type(exc).__name__)
                if claimed["attempts"] + 1 >= MAX_ATTEMPTS:
                    await self._fail(job.id)
                    result.failed.append(job.id)
            finally:
                self._release(job.id, failed=failed)
        return result
