"""Per-service database roles: each service can do its own job and nothing human-only.

Connections use libpq's `options=-c role=...`, so the same code paths run with exactly the
privileges that service has in production.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from tests.harness import Keys, drive_to_awaiting, make_service
from ytcore.gateway.assets import InMemoryAssetSource
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.store.errors import StoreError
from ytcore.store.postgres import PostgresStore
from ytcore.workflow.states import JobState

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]


def as_role(db: str, role: str) -> str:
    return f"{db} options='-c role={role}'"


def run_as(db: str, role: str, query: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(as_role(db, role), autocommit=True) as conn:
        cur = conn.execute(query, params or None)
        return cur.fetchall() if cur.description else []


@pytest.fixture
def stores(database: str, keys: Keys) -> dict[str, PostgresStore]:
    return {
        role: PostgresStore.from_conninfo(as_role(database, f"yt_{role}"), keys.verifier)
        for role in ("hub", "worker", "gateway", "ops")
    }


def test_a_job_goes_live_with_every_service_using_only_its_own_role(
    stores: dict[str, PostgresStore],
    keys: Keys,
    channel: Callable[..., str],
    secrets: Any,
    sql: SQL,
) -> None:
    ch = channel()
    assets, client = InMemoryAssetSource(), FakeYouTubeClient()
    job = stores["ops"].create_job(ch, "owner", actor="planner")  # the planner (ops job)
    chash = drive_to_awaiting(stores["worker"], assets, job, ch)  # production (worker)
    stores["worker"].record_qa(
        job, passed=True, risk_score=5, flags=[], sources=[{"url": "https://x.test"}], actor="qa"
    )
    token = keys.token(job, ch, chash)
    stores["hub"].verify_approval(job, token, actor="hub:alice")  # the human decision (hub)
    stores["worker"].transition(job, JobState.SCHEDULED, actor="workflow")
    service = make_service(stores["gateway"], keys, assets, client, secrets)  # the gateway
    result = service.publish(job, token)
    assert result.state in (JobState.PUBLISHED, JobState.LIVE)
    assert len(client.uploads) == 1
    assert sql("SELECT audit_chain_first_break()")[0][0] is None


@pytest.mark.parametrize("role", ["yt_worker", "yt_gateway", "yt_ops"])
@pytest.mark.parametrize(
    "forbidden",
    [
        "INSERT INTO approvals (job_id, channel_id, content_hash, token_jti, approver, "
        "expires_at) VALUES (gen_random_uuid(), 'c', 'sha256:" + "0" * 64 + "', 'j', 'alice', "
        "now() + interval '1 hour')",
        "UPDATE knowledge_docs SET status = 'approved', approved_by = 'alice'",
        "UPDATE global_controls SET publishing_paused = false",
        "INSERT INTO rollout_signoffs (cluster_id, reviewer, note) "
        "VALUES ('x', 'alice', 'a forged sign-off for the cluster')",
        "UPDATE channels SET autonomy_tier = 'gated'",
        "INSERT INTO hub_users (username, display_name, role) VALUES ('mallory', 'M', 'admin')",
        "UPDATE hub_users SET role = 'admin'",
        "INSERT INTO tier_rules (singleton, rules) VALUES (true, '{}') ON CONFLICT DO NOTHING",
        "DELETE FROM audit_events",
    ],
)
def test_no_service_other_than_the_hub_can_do_human_only_things(
    database: str, role: str, forbidden: str
) -> None:
    if role == "yt_ops" and forbidden.startswith("UPDATE channels SET autonomy_tier"):
        pytest.skip("ops may update only status and approval tier columns")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run_as(database, role, forbidden)


@pytest.mark.parametrize("role", ["yt_worker", "yt_gateway", "yt_ops"])
def test_self_asserted_hub_actor_gives_no_privilege(database: str, role: str) -> None:
    """The old weakness: set_config('app.actor', 'hub:alice') then approve. It no longer works."""
    with psycopg.connect(as_role(database, role), autocommit=True) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.actor', 'hub:alice', true)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE knowledge_docs SET status = 'approved', approved_by = 'alice'")


def test_the_worker_cannot_touch_uploads_and_the_hub_cannot_either(database: str) -> None:
    for role in ("yt_worker", "yt_hub"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            run_as(database, role, "UPDATE uploads SET status = 'live'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            run_as(database, role, "UPDATE quota_ledger SET units = 0")


def test_the_worker_and_gateway_cannot_read_sessions_or_passkeys(database: str) -> None:
    for role in ("yt_worker", "yt_gateway", "yt_ops"):
        for table in ("hub_sessions", "hub_invites", "webauthn_credentials"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                run_as(database, role, f"SELECT * FROM {table}")  # noqa: S608


def test_the_circuit_breaker_role_can_pause_but_not_resume_through_other_columns(
    database: str, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    cluster = sql("SELECT cluster_id FROM channels WHERE id = %s", ch)[0][0]
    run_as(database, "yt_ops", "UPDATE clusters SET status = 'paused' WHERE id = %s", cluster)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run_as(database, "yt_ops", "UPDATE clusters SET live_enabled = true WHERE id = %s", cluster)
    assert json.dumps(sql("SELECT status FROM clusters WHERE id = %s", cluster)) == '[["paused"]]'


# ------------------------------------------------------- the ops jobs run as yt_ops


def test_the_circuit_breaker_runs_as_the_ops_role(
    stores: dict[str, PostgresStore],
    database: str,
    channel: Callable[..., str],
    sql: SQL,
) -> None:
    from ytcore.safety.breaker import BreakerRepository, run_once

    ch = channel()
    cluster = sql("SELECT cluster_id FROM channels WHERE id = %s", ch)[0][0]
    sql("INSERT INTO cluster_breaker_config (cluster_id, min_decisions) VALUES (%s, 3)", cluster)
    assets = InMemoryAssetSource()
    for _ in range(4):
        job = stores["ops"].create_job(ch, "owner", actor="planner")
        chash = drive_to_awaiting(stores["worker"], assets, job, ch)
        sql(
            "INSERT INTO job_rejections (job_id, channel_id, content_hash, reason, "
            "rework_stage, rejected_by) VALUES (%s, %s, %s, 'misleading thumbnail', 'render', "
            "'owner')",
            job, ch, chash,
        )  # fmt: skip
    repo = BreakerRepository(lambda: psycopg.connect(as_role(database, "yt_ops"), autocommit=True))
    trips = run_once(repo)
    assert [t.cluster_id for t in trips if t.cluster_id == cluster] == [cluster]
    assert sql("SELECT status FROM clusters WHERE id = %s", cluster) == [("paused",)]
    assert sql("SELECT count(*) FROM alerts WHERE cluster_id = %s", cluster)[0][0] == 1


def test_the_monitor_and_tier_jobs_run_as_the_ops_role(
    stores: dict[str, PostgresStore], database: str, channel: Callable[..., str], sql: SQL
) -> None:
    from datetime import UTC, datetime, timedelta

    from ytcore.observability.monitor import MonitorThresholds, OpsMonitor
    from ytcore.tiers.service import TierService

    def connect() -> psycopg.Connection[Any]:
        return psycopg.connect(as_role(database, "yt_ops"), autocommit=True)

    ch = channel()
    stores["ops"].create_job(ch, "planner", actor="planner")
    monitor = OpsMonitor(connect, MonitorThresholds(plan_due_hour_utc=24))
    found = [f for f in monitor.check(datetime.now(UTC) + timedelta(hours=2)) if f.channel_id == ch]
    assert [f.kind for f in found] == ["job.stuck"]

    # tier evaluation: a clean history earns a recommendation; a strike demotes at once
    for i in range(25):
        job = stores["ops"].create_job(ch, "owner", actor="planner")
        sql(
            "INSERT INTO review_outcomes (job_id, channel_id, content_hash, decision, lane, "
            "risk_score, low_risk, tier, decided_by, created_at) VALUES (%s, %s, %s, 'approved', "
            "'standard', 10, true, 1, 'seed', now() - make_interval(days => %s))",
            job, ch, "sha256:" + "a" * 64, 31 - i // 1,
        )  # fmt: skip
    svc = TierService(connect)
    recs = [r for r in svc.evaluate_promotions() if r.channel_id == ch]
    assert len(recs) == 1
    sql("INSERT INTO hub_users (username, display_name, role) VALUES ('rolesadmin', 'R', 'admin')")
    TierService(lambda: psycopg.connect(as_role(database, "yt_hub"), autocommit=True)).confirm(
        recs[0].id, "rolesadmin", "reviewed the channel myself"
    )
    assert sql("SELECT approval_tier FROM channels WHERE id = %s", ch)[0][0] == 2
    svc.record_strike(
        ch, "strike", "strike on video 3 recorded", video_ref=None, actor="operator:o"
    )
    assert sql("SELECT approval_tier FROM channels WHERE id = %s", ch)[0][0] == 1


def test_the_ops_role_cannot_confirm_a_promotion(
    database: str, channel: Callable[..., str], sql: SQL
) -> None:
    from ytcore.tiers.service import TierService

    ch = channel()
    sql("INSERT INTO hub_users (username, display_name, role) VALUES ('rolesadmin2', 'R', 'admin')")
    ops = TierService(lambda: psycopg.connect(as_role(database, "yt_ops"), autocommit=True))
    rec = ops.recommend(ch, {})
    with pytest.raises(StoreError, match="42501"):
        ops.confirm(rec.id, "rolesadmin2", "an ops job trying to promote", recheck=False)
    assert sql("SELECT approval_tier FROM channels WHERE id = %s", ch)[0][0] == 1
