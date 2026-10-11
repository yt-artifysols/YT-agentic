"""Database side of the cluster supervisors: limits (concurrency, budget, pause) and health."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ytcore.store.postgres import Connect, map_db_error
from ytcore.workflow.supervisor import ClusterHealth, ClusterLimits

ACTOR = "supervisor"


class PostgresClusterState:
    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    @contextmanager
    def _tx(self) -> Iterator[psycopg.Connection[Any]]:
        try:
            with self._connect() as conn, conn.transaction():
                conn.execute("SELECT set_config('app.actor', %s, true)", (ACTOR,))
                yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    def limits(self, cluster_id: str) -> ClusterLimits:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT k.max_concurrent_jobs, k.daily_budget_cents, "
                "k.status <> 'active' AS paused, "
                "(SELECT coalesce(sum(jc.cost_cents), 0) FROM job_costs jc "
                " JOIN channels c ON c.id = jc.channel_id WHERE c.cluster_id = k.id "
                " AND jc.cost_date = (now() AT TIME ZONE 'UTC')::date) "
                "FROM clusters k WHERE k.id = %s",
                (cluster_id,),
            ).fetchone()
            glob = conn.execute("SELECT publishing_paused FROM global_controls").fetchone()
        if row is None:
            return ClusterLimits(max_concurrent=1, budget_remaining_cents=0, paused=True)
        budget = None if row[1] is None else int(row[1]) - int(row[3])
        # A paused cluster or the global kill switch stops STARTING work. A dark cluster
        # (live_enabled = false) still produces in shadow: the gate is on publishing.
        paused = bool(row[2]) or glob is None or bool(glob[0])
        return ClusterLimits(int(row[0]), budget, paused)

    def report(self, health: ClusterHealth) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO cluster_health (cluster_id, pending, running, finished, failed, "
                "budget_held, detail) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (cluster_id) DO UPDATE SET reported_at = now(), "
                "pending = EXCLUDED.pending, running = EXCLUDED.running, "
                "finished = EXCLUDED.finished, failed = EXCLUDED.failed, "
                "budget_held = EXCLUDED.budget_held, detail = EXCLUDED.detail",
                (
                    health.cluster_id,
                    health.pending,
                    health.running,
                    health.finished,
                    health.failed,
                    health.budget_held,
                    Jsonb({"outcomes": health.outcomes, "paused": health.paused}),
                ),
            )
