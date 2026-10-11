"""Fair-share leases on the shared agent pools (research, script_seo, media, gpu_render, qa).

Limits exist at three scopes (global, cluster, channel); a job may run a stage only when it holds
a lease and every applicable scope has headroom. Waiting jobs are ordered by how close they are
to their publish deadline (then by how long they have waited), so a job about to miss its slot
overtakes jobs with days to spare, and one busy channel or cluster cannot starve the others.

Leases expire, so a crashed worker cannot hold a slot forever; waiters that stop asking are
dropped. Everything happens under a per-pool advisory lock in one transaction.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import psycopg

from ytcore.store.postgres import Connect, map_db_error

POOLS = ("research", "script_seo", "media", "gpu_render", "qa")
ACTOR = "scheduler"


@dataclass(frozen=True)
class PoolStats:
    pool: str
    active: int
    waiting: int


class PoolLimiter:
    def __init__(
        self,
        connect: Connect,
        *,
        lease_seconds: int = 1800,
        waiter_ttl_seconds: int = 120,
    ) -> None:
        self._connect = connect
        self._lease = timedelta(seconds=lease_seconds)
        self._waiter_ttl = timedelta(seconds=waiter_ttl_seconds)

    @contextmanager
    def _tx(self) -> Iterator[psycopg.Connection[Any]]:
        try:
            with self._connect() as conn, conn.transaction():
                conn.execute("SELECT set_config('app.actor', %s, true)", (ACTOR,))
                yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    def acquire(self, pool: str, job_id: str) -> bool:
        """Try to take a slot; False means 'keep waiting' (your place in line is kept)."""
        if pool not in POOLS:
            raise ValueError(f"unknown pool {pool!r}")
        with self._tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 7))", (f"pool:{pool}",))
            held = conn.execute(
                "SELECT 1 FROM pool_leases WHERE pool = %s AND job_id = %s AND released_at IS NULL "
                "AND expires_at > now()",
                (pool, job_id),
            ).fetchone()
            if held is not None:
                return True
            conn.execute(
                "UPDATE pool_leases SET released_at = now() WHERE pool = %s "
                "AND released_at IS NULL AND expires_at <= now()",
                (pool,),
            )
            conn.execute(
                "DELETE FROM pool_waiters WHERE pool = %s AND seen_at < now() - %s::interval",
                (pool, self._waiter_ttl),
            )
            conn.execute(
                "INSERT INTO pool_waiters (pool, job_id, channel_id, cluster_id, priority_at) "
                "SELECT %s, j.id, j.channel_id, c.cluster_id, "
                "coalesce(j.approval_deadline, 'infinity'::timestamptz) FROM video_jobs j "
                "JOIN channels c ON c.id = j.channel_id WHERE j.id = %s "
                "ON CONFLICT (pool, job_id) DO UPDATE SET seen_at = now()",
                (pool, job_id),
            )
            limits = {
                (r[0], r[1]): r[2]
                for r in conn.execute(
                    "SELECT scope, scope_id, max_concurrent FROM pool_limits WHERE pool = %s",
                    (pool,),
                ).fetchall()
            }
            active = conn.execute(
                "SELECT channel_id, cluster_id FROM pool_leases WHERE pool = %s "
                "AND released_at IS NULL AND expires_at > now()",
                (pool,),
            ).fetchall()
            waiters = conn.execute(
                "SELECT job_id::text, channel_id, cluster_id FROM pool_waiters WHERE pool = %s "
                "ORDER BY priority_at, since, job_id",
                (pool,),
            ).fetchall()
            total = len(active)
            per_channel: dict[str, int] = {}
            per_cluster: dict[str | None, int] = {}
            for ch, cl in active:
                per_channel[ch] = per_channel.get(ch, 0) + 1
                per_cluster[cl] = per_cluster.get(cl, 0) + 1
            granted = False
            for wid, ch, cl in waiters:
                g = limits.get(("global", ""))
                c_lim = limits.get(("cluster", cl or ""))
                ch_lim = limits.get(("channel", ch))
                ok = (
                    (g is None or total < g)
                    and (c_lim is None or per_cluster.get(cl, 0) < c_lim)
                    and (ch_lim is None or per_channel.get(ch, 0) < ch_lim)
                )
                if not ok:
                    continue  # this waiter is blocked by its own scope; later ones may still fit
                total += 1
                per_cluster[cl] = per_cluster.get(cl, 0) + 1
                per_channel[ch] = per_channel.get(ch, 0) + 1
                if wid == job_id:
                    granted = True
                    break
            if not granted:
                return False
            conn.execute(
                "INSERT INTO pool_leases (pool, job_id, channel_id, cluster_id, expires_at) "
                "SELECT %s, %s, channel_id, cluster_id, now() + %s::interval FROM pool_waiters "
                "WHERE pool = %s AND job_id = %s",
                (pool, job_id, self._lease, pool, job_id),
            )
            conn.execute("DELETE FROM pool_waiters WHERE pool = %s AND job_id = %s", (pool, job_id))
            return True

    def release(self, pool: str, job_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE pool_leases SET released_at = now() WHERE pool = %s AND job_id = %s "
                "AND released_at IS NULL",
                (pool, job_id),
            )
            conn.execute("DELETE FROM pool_waiters WHERE pool = %s AND job_id = %s", (pool, job_id))

    def stats(self) -> list[PoolStats]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT p, (SELECT count(*) FROM pool_leases l WHERE l.pool = p "
                "AND l.released_at IS NULL AND l.expires_at > now()), "
                "(SELECT count(*) FROM pool_waiters w WHERE w.pool = p "
                "AND w.seen_at > now() - %s::interval) FROM unnest(%s::text[]) AS p",
                (self._waiter_ttl, list(POOLS)),
            ).fetchall()
        return [PoolStats(r[0], int(r[1]), int(r[2])) for r in rows]
