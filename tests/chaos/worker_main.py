"""A real worker process the chaos tests can SIGKILL.

    python -m tests.chaos.worker_main worker            # run activities against Temporal
    python -m tests.chaos.worker_main publish JOB TOKEN  # one gateway publish, then exit

Configuration comes from env vars set by the test. The YouTube client appends every upload to a
JSONL file, so the test can count uploads that reached "YouTube" across process restarts."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from temporalio.client import Client
from temporalio.worker import Worker

from tests.fakes import fake_secrets
from ytcore.approvals.token import ApprovalVerifier
from ytcore.gateway.assets import LocalDirAssetSource
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.gateway.service import PublishService
from ytcore.store.postgres import PostgresStore
from ytcore.workflow.activities import JobActivities
from ytcore.workflow.video_job import VideoJobWorkflow

ENV = os.environ


class SlowStore(PostgresStore):
    """Sleeps inside the render stage (after leaving a marker) so the test can kill the worker."""

    def record_cost(self, job_id: str, kind: str, cost_cents: int, *, actor: str) -> None:
        slow = float(ENV.get("CHAOS_SLOW_RENDER_SECONDS", "0"))
        if kind == "render" and slow > 0:
            Path(ENV["CHAOS_MARKER"]).write_text(f"{os.getpid()}\n")
            time.sleep(slow)
        super().record_cost(job_id, kind, cost_cents, actor=actor)


class RecordingYouTube(FakeYouTubeClient):
    """Logs each upload to a file; optionally hangs AFTER YouTube accepted it (crash window)."""

    def upload_resumable(self, **kw: Any) -> str:
        video_id = super().upload_resumable(**kw)
        with open(ENV["CHAOS_UPLOADS"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"video_id": video_id, "key": kw["idempotency_key"]}) + "\n")
        hang = float(ENV.get("CHAOS_HANG_AFTER_UPLOAD_SECONDS", "0"))
        if hang > 0:
            Path(ENV["CHAOS_MARKER"]).write_text(f"{os.getpid()}\n")
            time.sleep(hang)
        return video_id


def _stack() -> tuple[SlowStore, PublishService, LocalDirAssetSource]:
    public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(ENV["CHAOS_PUBKEY_HEX"]))
    verifier = ApprovalVerifier(public)
    store = SlowStore.from_conninfo(ENV["CHAOS_CONNINFO"], verifier)
    assets = LocalDirAssetSource(Path(ENV["CHAOS_ASSETS"]))
    service = PublishService(
        repo=store,
        verifier=verifier,
        client=RecordingYouTube(),
        assets=assets,
        secrets=fake_secrets(),
    )
    return store, service, assets


async def _worker() -> None:
    store, service, assets = _stack()
    client = await Client.connect(ENV["CHAOS_TEMPORAL"])
    activities = JobActivities(store, service, assets)
    worker = Worker(
        client,
        task_queue=ENV["CHAOS_QUEUE"],
        workflows=[VideoJobWorkflow],
        activities=activities.all(),
    )
    Path(ENV["CHAOS_READY"]).write_text(f"{os.getpid()}\n")
    await worker.run()


def main() -> int:
    mode = sys.argv[1]
    if mode == "worker":
        asyncio.run(_worker())
        return 0
    if mode == "publish":
        _, service, _ = _stack()
        service.publish(sys.argv[2], sys.argv[3])
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
