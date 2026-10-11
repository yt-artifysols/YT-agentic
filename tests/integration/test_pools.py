"""Fair-share leases on the shared agent pools (real Postgres)."""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest

from ytcore.scheduling.pools import PoolLimiter
from ytcore.store.postgres import PostgresStore

pytestmark = pytest.mark.integration
SQL = Callable[..., list[tuple[Any, ...]]]


@pytest.fixture
def limiter(database: str) -> PoolLimiter:
    return PoolLimiter(lambda: psycopg.connect(database, autocommit=True), waiter_ttl_seconds=2)


def _job(store: PostgresStore, channel: str, deadline_hours: float | None, sql: SQL) -> str:
    job = store.create_job(channel, "planner", actor="planner")
    if deadline_hours is not None:
        sql("UPDATE video_jobs SET approval_deadline = now() + make_interval(secs => %s) "
            "WHERE id = %s", deadline_hours * 3600, job)  # fmt: skip
    return job


def _limit(sql: SQL, scope: str, scope_id: str, pool: str, n: int) -> None:
    sql(
        "INSERT INTO pool_limits (scope, scope_id, pool, max_concurrent) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (scope, scope_id, pool) "
        "DO UPDATE SET max_concurrent = EXCLUDED.max_concurrent",
        scope,
        scope_id,
        pool,
        n,
    )


def test_a_limit_admits_only_that_many_and_release_lets_the_next_in(
    limiter: PoolLimiter, store: PostgresStore, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    _limit(sql, "channel", ch, "gpu_render", 2)
    jobs = [_job(store, ch, 10 + i, sql) for i in range(4)]
    granted = {j: limiter.acquire("gpu_render", j) for j in jobs}
    assert list(granted.values()) == [True, True, False, False]  # most urgent first
    limiter.release("gpu_render", jobs[0])
    assert limiter.acquire("gpu_render", jobs[2])
    assert not limiter.acquire("gpu_render", jobs[3])
    for j in jobs:
        limiter.release("gpu_render", j)


def test_a_global_limit_applies_across_channels(
    limiter: PoolLimiter, store: PostgresStore, channel: Callable[..., str], sql: SQL
) -> None:
    a, b = channel(), channel()
    _limit(sql, "global", "", "research", 1)
    try:
        ja, jb = _job(store, a, 3, sql), _job(store, b, 4, sql)
        assert limiter.acquire("research", ja)
        assert not limiter.acquire("research", jb)
        limiter.release("research", ja)
        assert limiter.acquire("research", jb)
        limiter.release("research", jb)
    finally:
        _limit(sql, "global", "", "research", 10_000)


def test_jobs_nearest_their_deadline_go_first(
    limiter: PoolLimiter, store: PostgresStore, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    _limit(sql, "channel", ch, "research", 1)
    holder = _job(store, ch, 50, sql)
    relaxed = _job(store, ch, 40, sql)
    urgent = _job(store, ch, 2, sql)
    assert limiter.acquire("research", holder)
    assert not limiter.acquire("research", relaxed)  # waits
    assert not limiter.acquire("research", urgent)  # waits, but is ahead of `relaxed`
    limiter.release("research", holder)
    assert not limiter.acquire("research", relaxed)  # the urgent job is first in line
    assert limiter.acquire("research", urgent)
    limiter.release("research", urgent)
    assert limiter.acquire("research", relaxed)
    limiter.release("research", relaxed)


def test_one_busy_channel_cannot_starve_its_cluster_mates(
    limiter: PoolLimiter, store: PostgresStore, channel: Callable[..., str], sql: SQL
) -> None:
    a, b = channel(), channel()
    cluster = sql("SELECT cluster_id FROM channels WHERE id = %s", a)[0][0]
    sql("UPDATE channels SET cluster_id = %s WHERE id = %s", cluster, b)
    _limit(sql, "cluster", cluster, "media", 3)
    _limit(sql, "channel", a, "media", 1)  # A may use only one media slot
    a_jobs = [_job(store, a, 1 + i, sql) for i in range(3)]  # A's jobs are the most urgent
    b_job = _job(store, b, 30, sql)
    granted_a = [j for j in a_jobs if limiter.acquire("media", j)]
    assert len(granted_a) == 1
    assert limiter.acquire("media", b_job)  # B still gets in despite A's queue
    for j in [*a_jobs, b_job]:
        limiter.release("media", j)


def test_cluster_limits_isolate_clusters_from_each_other(
    limiter: PoolLimiter, store: PostgresStore, channel: Callable[..., str], sql: SQL
) -> None:
    a, b = channel(), channel()  # different clusters
    ca = sql("SELECT cluster_id FROM channels WHERE id = %s", a)[0][0]
    _limit(sql, "cluster", ca, "qa", 1)
    ja1, ja2, jb = _job(store, a, 5, sql), _job(store, a, 6, sql), _job(store, b, 7, sql)
    assert limiter.acquire("qa", ja1)
    assert not limiter.acquire("qa", ja2)
    assert limiter.acquire("qa", jb)
    for j in (ja1, ja2, jb):
        limiter.release("qa", j)


def test_acquire_is_idempotent_for_the_holder_and_crashed_holders_expire(
    database: str, store: PostgresStore, channel: Callable[..., str], sql: SQL
) -> None:
    short = PoolLimiter(
        lambda: psycopg.connect(database, autocommit=True), lease_seconds=1, waiter_ttl_seconds=2
    )
    ch = channel()
    _limit(sql, "channel", ch, "script_seo", 1)
    j1, j2 = _job(store, ch, 5, sql), _job(store, ch, 6, sql)
    assert short.acquire("script_seo", j1) and short.acquire("script_seo", j1)
    assert not short.acquire("script_seo", j2)
    time.sleep(1.3)  # the holder "crashed": its lease runs out
    assert short.acquire("script_seo", j2)
    short.release("script_seo", j2)


def test_waiters_that_stop_asking_lose_their_place(
    limiter: PoolLimiter, store: PostgresStore, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    _limit(sql, "channel", ch, "qa", 1)
    holder, ghost, real = (_job(store, ch, h, sql) for h in (9, 1, 5))
    assert limiter.acquire("qa", holder)
    assert not limiter.acquire("qa", ghost)  # most urgent, but never asks again
    limiter.release("qa", holder)
    assert not limiter.acquire("qa", real)  # still reserved for the ghost
    time.sleep(2.3)
    assert limiter.acquire("qa", real)
    limiter.release("qa", real)


def test_concurrent_acquires_never_exceed_the_limit(
    limiter: PoolLimiter, store: PostgresStore, channel: Callable[..., str], sql: SQL
) -> None:
    ch = channel()
    _limit(sql, "channel", ch, "gpu_render", 3)
    jobs = [_job(store, ch, 5 + i, sql) for i in range(12)]
    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(lambda j: limiter.acquire("gpu_render", j), jobs))
    assert sum(results) <= 3
    held = sql("SELECT count(*) FROM pool_leases WHERE channel_id = %s AND released_at IS NULL", ch)
    assert held[0][0] == sum(results) <= 3
    for j in jobs:
        limiter.release("gpu_render", j)
