"""Postgres ProductionStore for the agent pipeline (ytcore/agents/store.py).

Costs, assets and alerts go to append-only tables whose triggers enforce budgets (YTB01 per
channel/day, YTB02 per job) and licenses (YTL01).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ytcore.agents.context import ChannelProfile
from ytcore.agents.costs import Spend
from ytcore.store.errors import JobNotFound, StoreError
from ytcore.store.postgres import Connect, map_db_error

_COST_KINDS = {"llm": "llm", "media": "media", "render": "render"}


class PostgresProductionStore:
    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    @classmethod
    def from_conninfo(cls, conninfo: str) -> PostgresProductionStore:
        return cls(lambda: psycopg.connect(conninfo, autocommit=True))

    @contextmanager
    def _tx(self, actor: str) -> Iterator[psycopg.Connection[Any]]:
        try:
            with self._connect() as conn, conn.transaction():
                conn.execute("SELECT set_config('app.actor', %s, true)", (actor,))
                yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    def channel_profile(self, channel_id: str) -> ChannelProfile:
        with self._tx("reader") as conn:
            row = conn.execute(
                "SELECT id, storage_prefix, memory_namespace, autonomy_tier FROM channels "
                "WHERE id = %s",
                (channel_id,),
            ).fetchone()
        if row is None:
            raise StoreError(f"unknown channel {channel_id}")
        return ChannelProfile(*row)

    def spend(self, job_id: str, channel_id: str) -> Spend:
        with self._tx("reader") as conn:
            row = conn.execute(
                """
                SELECT (SELECT coalesce(sum(cost_cents), 0) FROM job_costs WHERE job_id = %s),
                       c.job_budget_cents,
                       (SELECT coalesce(sum(cost_cents), 0) FROM job_costs
                        WHERE channel_id = c.id
                          AND cost_date = (now() AT TIME ZONE 'UTC')::date),
                       c.daily_budget_cents
                FROM channels c WHERE c.id = %s
                """,
                (job_id, channel_id),
            ).fetchone()
        if row is None:
            raise StoreError(f"unknown channel {channel_id}")
        return Spend(int(row[0]), int(row[1]), int(row[2]), int(row[3]))

    def record_cost(
        self,
        job_id: str,
        kind: str,
        cost_cents: int,
        *,
        provider: str,
        detail: dict[str, Any],
        actor: str,
        over_cap: bool = False,
    ) -> None:
        with self._tx(actor) as conn:
            cur = conn.execute(
                "INSERT INTO job_costs (job_id, channel_id, kind, cost_cents, provider, detail, "
                "over_cap) SELECT id, channel_id, %s, %s, %s, %s, %s FROM video_jobs WHERE id = %s",
                (
                    _COST_KINDS.get(kind, "other"),
                    cost_cents,
                    provider,
                    Jsonb(detail),
                    over_cap,
                    job_id,
                ),
            )
            if cur.rowcount != 1:
                raise JobNotFound(job_id)

    def alert(self, channel_id: str, job_id: str | None, kind: str, message: str) -> None:
        with self._tx("alerts") as conn:
            conn.execute(
                "INSERT INTO alerts (channel_id, job_id, kind, message) VALUES (%s, %s, %s, %s)",
                (channel_id, job_id, kind, message[:2000]),
            )

    def record_asset(
        self,
        job_id: str,
        channel_id: str,
        *,
        name: str,
        kind: str,
        sha256: str,
        media_type: str,
        provider: str,
        license_ref: str | None,
    ) -> int:
        with self._tx(f"agent:{provider}") as conn:
            row = conn.execute(
                "INSERT INTO job_assets (job_id, channel_id, name, version, kind, sha256, "
                "media_type, license_ref, provider) VALUES (%s, %s, %s, 0, %s, %s, %s, %s, %s) "
                "RETURNING version",
                (job_id, channel_id, name, kind, sha256, media_type, license_ref, provider),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def license_valid(self, license_ref: str, channel_id: str) -> bool:
        with self._tx("reader") as conn:
            row = conn.execute("SELECT license_valid(%s, %s)", (license_ref, channel_id)).fetchone()
        return bool(row and row[0])

    def revision_notes(self, job_id: str) -> list[str]:
        with self._tx("reader") as conn:
            qa = conn.execute(
                "SELECT flags FROM qa_reports WHERE job_id = %s ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            rejected = conn.execute(
                "SELECT reason FROM job_rejections WHERE job_id = %s ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        notes = [
            str(f.get("reason", ""))
            for f in (qa[0] if qa else [])
            if isinstance(f, dict) and f.get("severity") == "block"
        ]
        if rejected:
            notes.append(f"reviewer: {rejected[0]}")
        return notes

    def record_qa_flags(self, job_id: str, notes: list[str]) -> None:
        """No-op: the qa_reports row written by the QA activity is the source of truth."""
