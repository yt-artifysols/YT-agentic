"""Safety features against real Postgres: fingerprint index, stored risk, kill switches,
the cluster circuit breaker and DB-level tamper resistance."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.harness import Keys, drive_to_awaiting, make_service
from ytcore.agents.memory import HashEmbedder
from ytcore.approvals.hashing import content_hash
from ytcore.gateway.assets import InMemoryAssetSource
from ytcore.gateway.clients import FakeYouTubeClient
from ytcore.gateway.service import PublishRefused
from ytcore.safety.breaker import BreakerRepository, run_once
from ytcore.safety.duplicates import Fingerprints, PostgresFingerprintIndex
from ytcore.safety.policy import DuplicateThresholds
from ytcore.store.errors import ChannelNotPublishable, ContentFrozen
from ytcore.store.postgres import PostgresStore
from ytcore.store.production import PostgresProductionStore
from ytcore.workflow.states import JobState

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]

SCRIPT = (
    "Index funds track a broad market index and charge very low fees. Over twenty years most "
    "active funds trailed their benchmark. Diversification lowers the risk of one company "
    "failing, which is why many savers hold a total market fund for the long haul."
)


def _fp(text: str = SCRIPT, **kw: Any) -> Fingerprints:
    return Fingerprints.build(
        script_text=text,
        title=kw.get("title", "Why index funds win"),
        thumbnail_text=kw.get("thumbnail", "INDEX FUNDS"),
        media=kw.get("media", {"voiceover": "sha256:" + "a" * 64}),
        embedder=HashEmbedder(),
    )


def _job(store: PostgresStore, channel: str) -> str:
    return store.create_job(channel, "owner", actor="planner")


def test_fingerprint_index_finds_cross_channel_copies_only(
    store: PostgresStore, database: str, channel: Callable[..., str]
) -> None:
    a, b = channel(), channel()
    index = PostgresFingerprintIndex(
        lambda: __import__("psycopg").connect(database, autocommit=True)
    )
    first = _job(store, a)
    index.record(job_id=first, channel_id=a, fp=_fp())
    thr = DuplicateThresholds()

    own = index.find(job_id=_job(store, a), channel_id=a, fp=_fp(), thresholds=thr)
    assert own.matches == []  # a channel's own history is the memory's business, not the index's

    second = _job(store, b)
    copy = index.find(job_id=second, channel_id=b, fp=_fp(), thresholds=thr)
    kinds = {m.kind for m in copy.matches}
    assert {"script", "media"} <= kinds
    assert all(m.other_channel == a for m in copy.matches)

    different = index.find(
        job_id=second,
        channel_id=b,
        fp=_fp("A completely unrelated story about volcanoes, glaciers and tectonic plates "
               "moving slowly under the ocean floor over millions of years.",
               media={"voiceover": "sha256:" + "b" * 64}, title="Volcano facts",
               thumbnail="LAVA"),
        thresholds=thr,
    )  # fmt: skip
    assert different.matches == []


def test_risk_is_stored_on_the_job(
    store: PostgresStore, database: str, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    job = _job(store, ch)
    prod = PostgresProductionStore(lambda: __import__("psycopg").connect(database, autocommit=True))
    prod.record_risk(
        job,
        ch,
        {
            "score": 0.42,
            "escalate": True,
            "block": False,
            "reasons": [{"code": "sensitive_topic:health", "weight": 0.3}],
            "thresholds": {"escalate": 0.35, "block": 0.8},
        },
    )
    row = sql(
        "SELECT score::float8, escalate, blocked, created_by FROM job_risk WHERE job_id = %s", job
    )
    assert row == [(pytest.approx(0.42), True, False, "agent:qa")]
    assert sql(
        "SELECT 1 FROM audit_events WHERE subject = %s AND action = 'risk.assessed'", f"job:{job}"
    )


# ----------------------------------------------------------------------------- kill switches


@pytest.mark.parametrize("level", ["global", "cluster", "channel"])
def test_every_kill_switch_level_blocks_approval_scheduling_and_publish(
    level: str, store: PostgresStore, keys: Keys, channel: Callable[..., str], sql: SQL,
    secrets: Any,
) -> None:  # fmt: skip
    ch = channel()
    assets, client = InMemoryAssetSource(), FakeYouTubeClient()
    service = make_service(store, keys, assets, client, secrets)
    scheduled = _job(store, ch)
    chash = drive_to_awaiting(store, assets, scheduled, ch)
    token = keys.token(scheduled, ch, chash)
    store.verify_approval(scheduled, token, actor="approval-hub")
    store.transition(scheduled, JobState.SCHEDULED, actor="scheduler")
    waiting = _job(store, ch)
    wchash = drive_to_awaiting(store, assets, waiting, ch)

    try:
        if level == "global":
            sql("UPDATE global_controls SET publishing_paused = true")
        elif level == "cluster":
            sql("UPDATE clusters SET status = 'paused' WHERE id = "
                "(SELECT cluster_id FROM channels WHERE id = %s)", ch)  # fmt: skip
        else:
            sql("UPDATE channels SET status = 'paused' WHERE id = %s", ch)
        with pytest.raises(ChannelNotPublishable):
            store.verify_approval(waiting, keys.token(waiting, ch, wchash), actor="approval-hub")
        with pytest.raises(PublishRefused):
            service.publish(scheduled, token)
        assert client.uploads == []
        assert store.get_state(waiting) == JobState.AWAITING_APPROVAL
    finally:
        sql("UPDATE global_controls SET publishing_paused = false")


# ----------------------------------------------------------------------------- tamper


def test_manifest_cannot_be_edited_after_approval_in_the_database(
    store: PostgresStore, keys: Keys, channel: Callable[..., str], sql: SQL
) -> None:
    import psycopg

    ch = channel()
    assets = InMemoryAssetSource()
    job = _job(store, ch)
    chash = drive_to_awaiting(store, assets, job, ch)
    store.verify_approval(job, keys.token(job, ch, chash), actor="approval-hub")
    with pytest.raises(psycopg.Error) as exc:
        sql("UPDATE video_jobs SET content_hash = %s WHERE id = %s", "sha256:" + "c" * 64, job)
    from ytcore.store.postgres import map_db_error

    assert isinstance(map_db_error(exc.value), ContentFrozen)
    row = sql("SELECT manifest, content_hash FROM video_jobs WHERE id = %s", job)[0]
    assert content_hash(row[0]) == row[1] == chash


# ----------------------------------------------------------------------------- breaker


def _reject_jobs(store: PostgresStore, ch: str, sql: SQL, n: int) -> None:
    assets = InMemoryAssetSource()
    for _ in range(n):
        job = _job(store, ch)
        chash = drive_to_awaiting(store, assets, job, ch)
        sql(
            "INSERT INTO job_rejections (job_id, channel_id, content_hash, reason, rework_stage, "
            "rejected_by) VALUES (%s, %s, %s, 'misleading thumbnail', 'render', 'owner')",
            job, ch, chash,
        )  # fmt: skip


def _cluster(sql: SQL, ch: str) -> str:
    return str(sql("SELECT cluster_id FROM channels WHERE id = %s", ch)[0][0])


def test_breaker_pauses_a_cluster_on_rejection_rate_and_alerts(
    store: PostgresStore, database: str, channel: Callable[..., str], sql: SQL
) -> None:
    import psycopg

    ch, quiet = channel(), channel()
    cluster = _cluster(sql, ch)
    sql("INSERT INTO cluster_breaker_config (cluster_id, min_decisions) VALUES (%s, 3)", cluster)
    _reject_jobs(store, ch, sql, 4)
    notified: list[tuple[str, str]] = []
    repo = BreakerRepository(lambda: psycopg.connect(database, autocommit=True))

    trips = run_once(repo, notify=lambda t, b: notified.append((t, b)))

    mine = [t for t in trips if t.cluster_id == cluster]
    assert len(mine) == 1 and mine[0].reasons[0].code == "rejection_rate"
    assert sql("SELECT status FROM clusters WHERE id = %s", cluster) == [("paused",)]
    assert sql("SELECT status FROM clusters WHERE id = %s", _cluster(sql, quiet)) == [("active",)]
    assert sql("SELECT count(*) FROM breaker_trips WHERE cluster_id = %s", cluster)[0][0] == 1
    assert sql("SELECT count(*) FROM alerts WHERE cluster_id = %s AND kind = 'breaker.tripped'",
               cluster)[0][0] == 1  # fmt: skip
    assert any(cluster in title for title, _ in notified)
    # The breaker only pauses: a second run does not re-trip, and nothing is ever resumed.
    assert [t for t in run_once(repo) if t.cluster_id == cluster] == []
    assert sql("SELECT status FROM clusters WHERE id = %s", cluster) == [("paused",)]


def test_breaker_window_restarts_after_a_trip(
    store: PostgresStore, database: str, channel: Callable[..., str], sql: SQL
) -> None:
    import psycopg

    ch = channel()
    cluster = _cluster(sql, ch)
    sql("INSERT INTO cluster_breaker_config (cluster_id, min_decisions) VALUES (%s, 3)", cluster)
    _reject_jobs(store, ch, sql, 4)
    repo = BreakerRepository(lambda: psycopg.connect(database, autocommit=True))
    assert any(t.cluster_id == cluster for t in run_once(repo))
    sql("UPDATE clusters SET status = 'active' WHERE id = %s", cluster)  # human resumes it
    later = datetime.now(UTC) + timedelta(seconds=5)
    assert [t for t in run_once(repo, now=later) if t.cluster_id == cluster] == []


def test_breaker_can_be_disabled_per_cluster(
    store: PostgresStore, database: str, channel: Callable[..., str], sql: SQL
) -> None:
    import psycopg

    ch = channel()
    cluster = _cluster(sql, ch)
    sql("INSERT INTO cluster_breaker_config (cluster_id, enabled, min_decisions) "
        "VALUES (%s, false, 3)", cluster)  # fmt: skip
    _reject_jobs(store, ch, sql, 4)
    repo = BreakerRepository(lambda: psycopg.connect(database, autocommit=True))
    assert [t for t in run_once(repo) if t.cluster_id == cluster] == []
    assert sql("SELECT status FROM clusters WHERE id = %s", cluster) == [("active",)]


def test_a_wrong_dimension_embedding_cannot_poison_the_index(
    store: PostgresStore, database: str, channel: Callable[..., str], sql: SQL
) -> None:
    import psycopg

    a, b = channel(), channel()
    index = PostgresFingerprintIndex(lambda: psycopg.connect(database, autocommit=True))
    index.record(job_id=_job(store, a), channel_id=a, fp=_fp())
    bad = _job(store, a)
    with pytest.raises(psycopg.Error):
        sql(
            "INSERT INTO content_fingerprints (job_id, channel_id, kind, key, minhash, "
            "embedding, embedding_model) VALUES (%s, %s, 'script', 'x', ARRAY[1], "
            "'[1,0,0]', %s)",
            bad, a, HashEmbedder().model,
        )  # fmt: skip
    found = index.find(
        job_id=_job(store, b), channel_id=b, fp=_fp(), thresholds=DuplicateThresholds()
    )
    assert {m.kind for m in found.matches} >= {"script"}
