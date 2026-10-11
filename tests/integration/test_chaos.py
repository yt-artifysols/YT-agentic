"""Chaos: kill workers mid-render, restart Temporal, crash mid-upload. Jobs must resume with no
duplicate uploads. Real processes (SIGKILL), a persistent local Temporal server, real Postgres."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from temporalio.client import Client, WorkflowHandle
from temporalio.testing import WorkflowEnvironment

from tests.fakes import fake_secrets
from tests.harness import Keys, drive_to_awaiting, make_service
from ytcore.gateway.assets import LocalDirAssetSource
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.gateway.service import UploadInProgress
from ytcore.ops import reconcile
from ytcore.store.postgres import PostgresStore
from ytcore.workflow.models import ApprovalDecision, VideoJobInput, VideoJobResult
from ytcore.workflow.states import JobState
from ytcore.workflow.video_job import VideoJobWorkflow, workflow_id_for

pytestmark = [pytest.mark.integration, pytest.mark.chaos]
SQL = Callable[..., list[tuple[Any, ...]]]
ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Chaos:
    def __init__(self, tmp: Path, database: str, keys: Keys) -> None:
        self.tmp, self.database, self.keys = tmp, database, keys
        self.port = _free_port()
        self.address = f"127.0.0.1:{self.port}"
        self.temporal_db = str(tmp / "temporal.db")
        self.queue = f"chaos-{self.port}"
        self.uploads = tmp / "uploads.jsonl"
        self.marker = tmp / "marker"
        self.env: WorkflowEnvironment | None = None
        self.procs: list[subprocess.Popen[bytes]] = []
        (tmp / "assets").mkdir(exist_ok=True)

    async def start_temporal(self) -> Client:
        self.env = await WorkflowEnvironment.start_local(
            port=self.port, dev_server_extra_args=["--db-filename", self.temporal_db]
        )
        return self.env.client

    async def stop_temporal(self) -> None:
        assert self.env is not None
        await self.env.shutdown()
        self.env = None

    def _env(self, **extra: str) -> dict[str, str]:
        public = self.keys.signer.public_key().public_bytes_raw().hex()
        return {
            **os.environ,
            "CHAOS_PUBKEY_HEX": public,
            "CHAOS_CONNINFO": self.database,
            "CHAOS_ASSETS": str(self.tmp / "assets"),
            "CHAOS_UPLOADS": str(self.uploads),
            "CHAOS_MARKER": str(self.marker),
            "CHAOS_READY": str(self.tmp / f"ready-{len(self.procs)}"),
            "CHAOS_TEMPORAL": self.address,
            "CHAOS_QUEUE": self.queue,
            "PYTHONPATH": str(ROOT),
            **extra,
        }

    def spawn(self, *args: str, **env: str) -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(
            [sys.executable, "-m", "tests.chaos.worker_main", *args],
            cwd=ROOT,
            env=self._env(**env),
        )
        self.procs.append(proc)
        return proc

    def uploaded(self) -> list[dict[str, str]]:
        if not self.uploads.exists():
            return []
        return [json.loads(line) for line in self.uploads.read_text().splitlines() if line]

    def cleanup(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)


@pytest.fixture
async def chaos(tmp_path: Path, database: str, keys: Keys) -> AsyncIterator[Chaos]:
    c = Chaos(tmp_path, database, keys)
    try:
        yield c
    finally:
        c.cleanup()
        if c.env is not None:
            await c.stop_temporal()


async def _until(cond: Callable[[], Any], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = cond()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


async def _start_job(
    c: Chaos, client: Client, store: PostgresStore, channel: str
) -> tuple[str, WorkflowHandle[Any, Any]]:
    job = store.create_job(channel, "planner", actor="planner")
    handle = await client.start_workflow(
        VideoJobWorkflow.run,
        VideoJobInput(
            job_id=job,
            channel_id=channel,
            origin="planner",
            approval_timeout_seconds=3600,
            heartbeat_timeout_seconds=3,
        ),
        id=workflow_id_for(job),
        task_queue=c.queue,
    )
    return job, handle


async def _await_state(handle: WorkflowHandle[Any, Any], state: JobState, timeout: float) -> None:
    async def at_state() -> bool:
        return bool(await handle.query(VideoJobWorkflow.state) == state.value)

    await _until(at_state, timeout, f"workflow state {state.value}")


def _approve_signal(c: Chaos, store: PostgresStore, job: str, channel: str) -> ApprovalDecision:
    chash = store.publish_context(job).content_hash
    assert chash is not None
    return ApprovalDecision(True, "owner", c.keys.token(job, channel, chash))


async def test_worker_killed_mid_render_resumes_and_publishes_exactly_once(
    chaos: Chaos, store: PostgresStore, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    client = await chaos.start_temporal()
    slow = chaos.spawn("worker", CHAOS_SLOW_RENDER_SECONDS="120")
    await _until(lambda: (chaos.tmp / "ready-0").exists(), 60, "first worker")
    job, handle = await _start_job(chaos, client, store, ch)
    await _until(chaos.marker.exists, 60, "render to start")

    os.kill(slow.pid, signal.SIGKILL)  # the worker dies mid-render
    slow.wait(timeout=10)
    assert store.get_state(job) == JobState.RENDERING  # killed with the render in flight

    chaos.spawn("worker")  # a replacement worker picks the job up after the heartbeat timeout
    await _await_state(handle, JobState.AWAITING_APPROVAL, 150)
    await handle.signal(VideoJobWorkflow.submit_approval, _approve_signal(chaos, store, job, ch))
    result = await asyncio.wait_for(handle.result(), 150)

    assert result.final_state == JobState.LIVE.value
    assert len(chaos.uploaded()) == 1
    assert sql("SELECT count(*) FROM uploads WHERE job_id = %s", job)[0][0] == 1
    # The render was paid for once: the killed attempt never recorded its cost.
    assert (
        sql("SELECT count(*) FROM job_costs WHERE job_id = %s AND kind = 'render'", job)[0][0] == 1
    )
    states = [
        r[0] for r in sql("SELECT to_state FROM job_events WHERE job_id = %s ORDER BY id", job)
    ]
    assert states[-3:] == ["publishing", "published", "live"]
    assert sql("SELECT audit_chain_first_break()")[0][0] is None


async def test_temporal_restart_mid_job_resumes_without_duplicates(
    chaos: Chaos, store: PostgresStore, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    client = await chaos.start_temporal()
    chaos.spawn("worker")
    await _until(lambda: (chaos.tmp / "ready-0").exists(), 60, "worker")
    job, handle = await _start_job(chaos, client, store, ch)
    await _await_state(handle, JobState.AWAITING_APPROVAL, 60)

    await chaos.stop_temporal()  # Temporal goes away while the job waits for a human
    await asyncio.sleep(2)
    client = await chaos.start_temporal()  # same port, same persisted history
    handle = client.get_workflow_handle(workflow_id_for(job), result_type=VideoJobResult)
    await _await_state(handle, JobState.AWAITING_APPROVAL, 60)

    await handle.signal(VideoJobWorkflow.submit_approval, _approve_signal(chaos, store, job, ch))
    result = await asyncio.wait_for(handle.result(), 150)
    assert result.final_state == JobState.LIVE.value
    assert len(chaos.uploaded()) == 1
    assert sql("SELECT count(*) FROM uploads WHERE job_id = %s", job)[0][0] == 1


async def test_crash_after_youtube_accepted_the_video_never_uploads_twice(
    chaos: Chaos, store: PostgresStore, keys: Keys, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    assets = LocalDirAssetSource(chaos.tmp / "assets")
    job = store.create_job(ch, "planner", actor="planner")
    chash = drive_to_awaiting(store, assets, job, ch)  # type: ignore[arg-type]
    token = keys.token(job, ch, chash)
    store.verify_approval(job, token, actor="approval-hub")
    store.transition(job, JobState.SCHEDULED, actor="scheduler")

    crashing = chaos.spawn("publish", job, token, CHAOS_HANG_AFTER_UPLOAD_SECONDS="120")
    await _until(chaos.marker.exists, 60, "upload to be accepted")
    os.kill(
        crashing.pid, signal.SIGKILL
    )  # dies after YouTube took the video, before we recorded it
    crashing.wait(timeout=10)
    assert len(chaos.uploaded()) == 1

    # Retrying is refused: a second attempt could create a duplicate video.
    service = make_service(store, keys, assets, FakeYouTubeClient(), fake_secrets())
    with pytest.raises(UploadInProgress):
        service.publish(job, token)
    assert len(chaos.uploaded()) == 1
    (pending,) = reconcile.pending_uploads(lambda: __import__("psycopg").connect(chaos.database))
    assert pending.job_id == job

    # The operator confirms the video exists on YouTube and adopts it; the retry is a no-op.
    reconcile.adopt_upload(
        store, pending.upload_id, chaos.uploaded()[0]["video_id"], operator="ada"
    )
    again = service.publish(job, token)
    assert again.duplicate and again.video_id == chaos.uploaded()[0]["video_id"]
    assert store.get_state(job) == JobState.PUBLISHED
    assert sql("SELECT count(*) FROM uploads WHERE job_id = %s", job)[0][0] == 1
    assert sql("SELECT count(*) FROM audit_events WHERE actor = 'operator:ada'")[0][0] >= 1


def test_release_requires_a_reason(store: PostgresStore) -> None:
    with pytest.raises(ValueError):
        reconcile.release_upload(store, "x", "short", operator="ada")
    with pytest.raises(ValueError):
        reconcile.adopt_upload(store, "x", "../etc", operator="ada")
