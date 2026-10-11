"""Starts VideoJobWorkflow runs for planned jobs. Idempotent: the workflow id is derived from the
job id, so starting twice (or after a crash) never creates a second run."""

from __future__ import annotations

import asyncio
from datetime import datetime

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from ytcore.workflow.models import VideoJobInput
from ytcore.workflow.supervisor import ClusterSupervisorWorkflow, JobSpec, SupervisorInput
from ytcore.workflow.video_job import VideoJobWorkflow, workflow_id_for


class TemporalStarter:
    """Sync facade for the (sync) planner over an async Temporal client.

    `loop` is the event loop that owns the client; call the planner from a worker thread
    (asyncio.to_thread) or any thread other than that loop's."""

    def __init__(
        self,
        client: Client,
        loop: asyncio.AbstractEventLoop,
        task_queue: str,
        *,
        supervised: bool = False,
    ) -> None:
        self._client, self._loop, self._queue = client, loop, task_queue
        self._supervised = supervised

    def start(
        self,
        job_id: str,
        channel_id: str,
        approval_timeout_seconds: int,
        approval_deadline: datetime | None = None,
        cluster_id: str | None = None,
    ) -> None:
        future = asyncio.run_coroutine_threadsafe(
            self._start(
                job_id, channel_id, approval_timeout_seconds, approval_deadline, cluster_id
            ),
            self._loop,
        )
        future.result(timeout=60)

    async def _start(
        self,
        job_id: str,
        channel_id: str,
        timeout_s: int,
        deadline: datetime | None,
        cluster_id: str | None,
    ) -> None:
        if self._supervised and cluster_id is not None:
            # Hand the job to its cluster's supervisor (started on first use); it dedupes by job.
            await self._client.start_workflow(
                ClusterSupervisorWorkflow.run,
                SupervisorInput(cluster_id=cluster_id),
                id=f"cluster-{cluster_id}",
                task_queue=self._queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                start_signal="enqueue",
                start_signal_args=[
                    JobSpec(
                        job_id=job_id,
                        channel_id=channel_id,
                        approval_deadline_epoch=deadline.timestamp() if deadline else None,
                        approval_timeout_seconds=timeout_s,
                    )
                ],
            )
            return
        try:
            await self._client.start_workflow(
                VideoJobWorkflow.run,
                VideoJobInput(
                    job_id=job_id,
                    channel_id=channel_id,
                    origin="planner",
                    approval_timeout_seconds=timeout_s,
                    approval_deadline_epoch=deadline.timestamp() if deadline else None,
                ),
                id=workflow_id_for(job_id),
                task_queue=self._queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            return
