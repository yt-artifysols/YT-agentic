"""The two Vercel entrypoints boot from environment variables alone and do their job on the
real database under their own roles."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.integration.conftest import fresh_database
from tests.integration.test_agents_postgres import _seed_knowledge
from tests.integration.test_roles import as_role
from ytcore.agents.fake import FAKE_MUSIC_LICENSE, FAKE_STOCK_LICENSE
from ytcore.serverless.runtime import build_hub_app
from ytcore.store.postgres import PostgresStore

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def _load(path: str) -> Any:
    spec = importlib.util.spec_from_file_location("vercel_entry", ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)


async def test_hub_boots_from_env_and_rejects_anonymous(
    database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _env(
        monkeypatch,
        ENV="staging",
        YT_HUB_DATABASE_URL=as_role(database, "yt_hub"),
        YT_HUB_SIGNING_KEY_PEM=pem.decode(),
        YT_HUB_ORIGIN="https://hub.example.test",
        YT_HUB_RP_ID="hub.example.test",
        YT_HUB_COOKIE_SECURE="true",
    )
    app = build_hub_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://hub.example.test"
    ) as c:
        assert (await c.get("/api/auth/me")).status_code == 401
        assert (await c.get("/api/jobs")).status_code == 401
        # the ceremony table is shared state: a login ceremony survives across app instances
        opts = await c.post(
            "/api/auth/login/options",
            json={"username": "nobody"},
            headers={"Origin": "https://hub.example.test"},
        )
        assert opts.status_code == 200


def test_jobs_entrypoint_is_authenticated_and_drives_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A private database: QA's cross-channel duplicate check must not see other tests' videos.
    with fresh_database() as db, psycopg.connect(db, autocommit=True) as conn:
        _run_jobs_entrypoint(db, conn, monkeypatch)


def _run_jobs_entrypoint(
    db: str, conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from tests.harness import Keys

    public_pem_text = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    _env(
        monkeypatch,
        ENV="staging",
        YT_SECRETS_BACKEND="platform_env",
        YT_DATABASE_URL_REF="env://YT_JOBS_DATABASE_URL",
        YT_JOBS_DATABASE_URL=as_role(db, "yt_worker"),
        APPROVAL_PUBLIC_KEY=public_pem_text,
        CRON_SECRET="s3cret-for-tests",
    )
    module = _load("deploy/vercel/jobs/api/cron.py")
    conn.execute("INSERT INTO clusters (id, display_name) VALUES ('e-cl', 'e')")
    conn.execute(
        "INSERT INTO google_projects (id, daily_quota_units, oauth_client_ref) "
        "VALUES ('e-proj', 100000, 'env://YT_OAUTH_CLIENT')"
    )
    conn.execute(
        "INSERT INTO channels (id, display_name, storage_prefix, memory_namespace, cluster_id,"
        " autonomy_tier, daily_budget_cents, google_project_id, youtube_credentials_ref) "
        "VALUES ('e-ch', 'e', 'channels/e-ch/', 'mem:e-ch', 'e-cl', 'gated', 10000, 'e-proj', "
        "'env://YT_REFRESH_TOKEN')"
    )
    _seed_knowledge(db, "e-ch", "entry-approver")  # the real pipeline needs approved brand/policy
    for ref, kind in ((FAKE_MUSIC_LICENSE, "music"), (FAKE_STOCK_LICENSE, "stock")):
        conn.execute(
            "INSERT INTO rights_register (license_ref, asset_kind, provider) "
            "VALUES (%s, %s, 'fake-stock') ON CONFLICT DO NOTHING",
            (ref, kind),
        )
    worker = PostgresStore.from_conninfo(as_role(db, "yt_worker"), Keys().verifier)
    job = worker.create_job("e-ch", "owner", actor="planner")

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=module.app), base_url="https://jobs.example.test"
        ) as c:
            assert (await c.get("/api/cron/tick")).status_code == 401
            bad = await c.get("/api/cron/tick", headers={"Authorization": "Bearer wrong"})
            assert bad.status_code == 401
            auth = {"Authorization": "Bearer s3cret-for-tests"}
            ok = await c.get("/api/cron/tick", headers=auth)
            assert ok.status_code == 200, ok.text
            assert ok.json()["advanced"] == 1
            # no YT_OPS_DATABASE_URL: the planner is off, not guessed
            assert (await c.get("/api/cron/plan", headers=auth)).status_code == 503

    asyncio.run(run())
    state = conn.execute("SELECT state FROM video_jobs WHERE id = %s", (job,)).fetchone()
    assert state == ("awaiting_approval",)


def test_services_refuse_the_owner_or_the_wrong_role(database: str) -> None:
    from ytcore.serverless.runtime import require_service_role

    require_service_role(as_role(database, "yt_hub"), "yt_hub")
    require_service_role(as_role(database, "yt_worker"), "yt_worker")
    with pytest.raises(RuntimeError, match="limited member"):
        require_service_role(database, "yt_hub")  # the owner/migration login
    with pytest.raises(RuntimeError, match="limited member"):
        require_service_role(as_role(database, "yt_worker"), "yt_hub")  # wrong service
