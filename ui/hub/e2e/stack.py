"""Start a full local stack for the UI end-to-end test and keep it running.

Postgres (fresh throwaway DB from PG* env) + Temporal dev server + VideoJobWorkflow worker
(Postgres store, gateway with the fake YouTube client) + approval hub API on 127.0.0.1:8001.
Seeds one gated channel, three workflows waiting for approval and two invites, then writes
e2e/.stack.json for the Playwright test and e2e/verify.py.

Usage (from the repo root): python ui/hub/e2e/stack.py
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
import uvicorn  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from ytcore.approvals.service.main import create_hub_app  # noqa: E402
from ytcore.approvals.service.notify import FakeNotifier  # noqa: E402
from ytcore.approvals.service.repo import HubRepository  # noqa: E402
from ytcore.approvals.service.settings import HubSettings  # noqa: E402
from ytcore.approvals.service.signals import TemporalSignaler  # noqa: E402
from ytcore.approvals.token import ApprovalVerifier  # noqa: E402
from ytcore.gateway.assets import InMemoryAssetSource  # noqa: E402
from ytcore.gateway.clients import FakeYouTubeClient  # noqa: E402
from ytcore.gateway.service import PublishService  # noqa: E402
from ytcore.secrets.provider import InMemorySecretsProvider  # noqa: E402
from ytcore.store.postgres import PostgresStore  # noqa: E402
from ytcore.workflow.activities import JobActivities  # noqa: E402
from ytcore.workflow.models import VideoJobInput  # noqa: E402
from ytcore.workflow.states import JobState  # noqa: E402
from ytcore.workflow.video_job import VideoJobWorkflow, workflow_id_for  # noqa: E402

STACK_FILE = Path(__file__).with_name(".stack.json")
QUEUE = "e2e"


def create_database() -> str:
    admin_db = os.environ.get("PGDATABASE", "postgres")
    name = f"ytcore_e2e_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(f"dbname={admin_db}", autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    env = {**os.environ, "PGDATABASE": name}
    env.pop("DATABASE_URL", None)
    subprocess.run([sys.executable, "-m", "ytcore.db.migrate"], cwd=ROOT, env=env, check=True,
                   capture_output=True)  # fmt: skip
    return name


def seed_channel(conninfo: str) -> str:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.actor', 'e2e-seed', false)")
        conn.execute(
            "INSERT INTO clusters (id, display_name) VALUES ('e2e-cluster', 'E2E cluster')"
        )
        conn.execute("INSERT INTO google_projects (id, daily_quota_units, oauth_client_ref) "
                     "VALUES ('proj-e2e', 100000, 'env://YT_OAUTH_CLIENT')")  # fmt: skip
        conn.execute(
            "INSERT INTO channels (id, display_name, storage_prefix, memory_namespace, cluster_id,"
            " autonomy_tier, daily_budget_cents, google_project_id, youtube_credentials_ref) "
            "VALUES ('e2e-chan', 'E2E Channel', 'channels/e2e-chan/', 'mem:e2e-chan',"
            " 'e2e-cluster', 'gated', 100000, 'proj-e2e', 'env://YT_REFRESH_TOKEN')"
        )
    return "e2e-chan"


async def main() -> None:
    db = create_database()
    conninfo = f"dbname={db}"
    channel = seed_channel(conninfo)
    key = Ed25519PrivateKey.generate()  # hub-only signing key for this run (never written)
    verifier = ApprovalVerifier(key.public_key())
    assets = InMemoryAssetSource()
    secrets_provider = InMemorySecretsProvider(
        {
            "env://YT_REFRESH_TOKEN": "e2e-dummy",
            "env://YT_OAUTH_CLIENT": '{"client_id": "x", "client_secret": "y"}',
        }
    )
    store = PostgresStore.from_conninfo(conninfo, verifier)
    youtube = FakeYouTubeClient()
    publisher = PublishService(repo=store, verifier=verifier, client=youtube, assets=assets,
                               secrets=secrets_provider)  # fmt: skip
    activities = JobActivities(store, publisher, assets)

    async with await WorkflowEnvironment.start_local(namespace="default") as env:
        worker = Worker(env.client, task_queue=QUEUE, workflows=[VideoJobWorkflow],
                        activities=activities.all())  # fmt: skip
        settings = HubSettings(origin="http://localhost:3000", rp_id="localhost")
        app = create_hub_app(settings, conninfo=conninfo, key_provider=key,
                             signaler=TemporalSignaler(env.client), notifier=FakeNotifier(),
                             assets=assets)  # fmt: skip
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=8001, log_level="warning")
        )

        async with worker:
            jobs = {}
            for label in ("approve", "edit", "reject"):
                job = store.create_job(channel, "owner", actor="planner")
                await env.client.start_workflow(
                    VideoJobWorkflow.run,
                    VideoJobInput(job_id=job, channel_id=channel, origin="owner"),
                    id=workflow_id_for(job), task_queue=QUEUE,
                )  # fmt: skip
                jobs[label] = job
            for _ in range(600):
                if all(store.get_state(j) == JobState.AWAITING_APPROVAL for j in jobs.values()):
                    break
                await asyncio.sleep(0.1)
            else:
                raise SystemExit("workflows never reached awaiting_approval")

            repo = HubRepository.from_conninfo(conninfo)
            invites = {}
            for username, role in (("e2e-approver", "approver"), ("e2e-operator", "operator")):
                user = repo.create_user(username, username.replace("-", " ").title(), role,  # type: ignore[arg-type]
                                        actor="hub:e2e")  # fmt: skip
                token = secrets.token_urlsafe(32)
                repo.create_invite(user.id, token, datetime.now(UTC) + timedelta(hours=2),
                                   actor="hub:e2e")  # fmt: skip
                invites[role] = token
            STACK_FILE.write_text(json.dumps(
                {"database": db, "channel": channel, "jobs": jobs, "invites": invites}, indent=2
            ))  # fmt: skip
            print(f"stack ready: db={db} jobs={jobs}", flush=True)

            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)
            serve = asyncio.create_task(server.serve())
            await stop.wait()
            server.should_exit = True
            await serve


def drop_database(name: str) -> None:
    admin_db = os.environ.get("PGDATABASE", "postgres")
    with psycopg.connect(f"dbname={admin_db}", autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        if STACK_FILE.exists():
            drop_database(json.loads(STACK_FILE.read_text())["database"])
            STACK_FILE.unlink()
