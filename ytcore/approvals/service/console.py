"""Read-only views for the operator console: today's overview, the whole pipeline, a job's
timeline, channels and users. Nothing here changes state; every decision still goes through
the hub's guarded endpoints (and a fresh passkey assertion)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ytcore.store.postgres import Connect, map_db_error

PRODUCTION_STATES = ("planned", "researching", "scripting", "rendering", "qa")
JOB_STATES = (
    "planned",
    "researching",
    "scripting",
    "rendering",
    "qa",
    "awaiting_approval",
    "approved",
    "scheduled",
    "publishing",
    "published",
    "live",
    "rejected",
    "failed",
    "cancelled",
)


class ConsoleRepository:
    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    @contextmanager
    def _conn(self) -> Any:
        try:
            with self._connect() as conn:
                conn.execute("SELECT set_config('app.actor', 'hub:console', false)")
                yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    # ------------------------------------------------------------------ overview

    def overview(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        day_ago, soon = now - timedelta(hours=24), now + timedelta(hours=2)
        with self._conn() as conn:
            q = conn.execute
            awaiting = q(
                "SELECT count(*), min(approval_deadline), "
                "count(*) FILTER (WHERE approval_deadline <= %s) "
                "FROM video_jobs WHERE state = 'awaiting_approval'",
                (soon,),
            ).fetchone()
            in_production = q(
                "SELECT count(*) FROM video_jobs WHERE state = ANY(%s)", (list(PRODUCTION_STATES),)
            ).fetchone()
            done = dict(
                q(
                    "SELECT to_state, count(*) FROM job_events WHERE at >= %s "
                    "AND to_state IN ('live', 'published', 'failed', 'cancelled', 'rejected') "
                    "GROUP BY to_state",
                    (day_ago,),
                ).fetchall()
            )
            kill = q("SELECT publishing_paused FROM global_controls").fetchone()
            paused = q(
                "SELECT (SELECT count(*) FROM clusters WHERE status <> 'active'), "
                "(SELECT count(*) FROM channels WHERE status <> 'active')"
            ).fetchone()
            cost = q(
                "SELECT coalesce(sum(cost_cents), 0) FROM job_costs "
                "WHERE cost_date = (now() AT TIME ZONE 'UTC')::date"
            ).fetchone()
            budget = q(
                "SELECT coalesce(sum(daily_budget_cents), 0) FROM channels "
                "WHERE autonomy_tier = 'gated'"
            ).fetchone()
            alerts = q(
                "SELECT id, kind, message, channel_id, cluster_id, created_at FROM alerts "
                "ORDER BY id DESC LIMIT 6"
            ).fetchall()
            alert_count = q("SELECT count(*) FROM alerts WHERE created_at >= %s", (day_ago,))
            activity = q(
                "SELECT e.id, e.job_id::text, j.channel_id, e.from_state, e.to_state, e.actor, "
                "e.at, coalesce(j.manifest->'metadata'->>'title', '') FROM job_events e "
                "JOIN video_jobs j ON j.id = e.job_id ORDER BY e.id DESC LIMIT 12"
            ).fetchall()
            clusters = q(
                "SELECT k.id, k.display_name, k.status, k.live_enabled, "
                "count(c.id), count(j.id) FILTER (WHERE j.state = 'awaiting_approval') "
                "FROM clusters k LEFT JOIN channels c ON c.cluster_id = k.id "
                "LEFT JOIN video_jobs j ON j.channel_id = c.id GROUP BY k.id ORDER BY k.id"
            ).fetchall()
            tiers = q(
                "SELECT (SELECT count(*) FROM tier_recommendations WHERE status = 'pending'), "
                "(SELECT count(*) FROM tier_changes WHERE kind = 'demotion' AND created_at >= %s)",
                (now - timedelta(days=7),),
            ).fetchone()
            channels = q("SELECT count(*) FROM channels").fetchone()
        assert awaiting and in_production and kill and paused and cost and budget and tiers
        return {
            "generated_at": now,
            "awaiting_approval": {
                "count": awaiting[0],
                "oldest_deadline": awaiting[1],
                "due_within_2h": awaiting[2],
            },
            "in_production": in_production[0],
            "last_24h": {
                "live": done.get("live", 0) + done.get("published", 0),
                "failed": done.get("failed", 0),
                "cancelled": done.get("cancelled", 0),
                "rejected": done.get("rejected", 0),
            },
            "kill_switch": {
                "global_paused": bool(kill[0]),
                "paused_clusters": paused[0],
                "paused_channels": paused[1],
            },
            "cost_today_cents": int(cost[0]),
            "daily_budget_cents": int(budget[0]),
            "alerts_24h": q_scalar(alert_count),
            "alerts": [
                {
                    "id": a[0],
                    "kind": a[1],
                    "message": a[2],
                    "channel_id": a[3],
                    "cluster_id": a[4],
                    "created_at": a[5],
                }
                for a in alerts
            ],
            "activity": [
                {
                    "id": a[0],
                    "job_id": a[1],
                    "channel_id": a[2],
                    "from_state": a[3],
                    "to_state": a[4],
                    "actor": a[5],
                    "at": a[6],
                    "title": a[7],
                }
                for a in activity
            ],
            "clusters": [
                {
                    "id": c[0],
                    "name": c[1],
                    "status": c[2],
                    "live_enabled": c[3],
                    "channels": c[4],
                    "awaiting_approval": c[5],
                }
                for c in clusters
            ],
            "tiers": {"pending_recommendations": tiers[0], "demotions_7d": tiers[1]},
            "channels": channels[0] if channels else 0,
        }

    # ------------------------------------------------------------------ pipeline

    def pipeline(
        self,
        *,
        state: str | None = None,
        channel: str | None = None,
        cluster: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if state is not None and state not in JOB_STATES:
            raise ValueError(f"unknown state {state!r}")
        params = {
            "state": state,
            "channel": channel,
            "cluster": cluster,
            "limit": max(1, min(limit, 200)),
            "offset": max(0, offset),
        }
        with self._conn() as conn:
            sql = (
                "SELECT j.id::text, j.channel_id, c.cluster_id, j.state, j.origin, "
                "coalesce(j.manifest->'metadata'->>'title', ''), j.created_at, j.updated_at, "
                "j.approval_deadline, qa.risk_score, up.youtube_video_id "
                "FROM video_jobs j JOIN channels c ON c.id = j.channel_id "
                "LEFT JOIN LATERAL (SELECT risk_score FROM qa_reports q WHERE q.job_id = j.id "
                "                   ORDER BY q.id DESC LIMIT 1) qa ON true "
                "LEFT JOIN LATERAL (SELECT youtube_video_id FROM uploads u WHERE u.job_id = j.id "
                "                   ORDER BY u.created_at DESC LIMIT 1) up ON true "
                "WHERE (%(state)s::text IS NULL OR j.state = %(state)s) "
                "AND (%(channel)s::text IS NULL OR j.channel_id = %(channel)s) "
                "AND (%(cluster)s::text IS NULL OR c.cluster_id = %(cluster)s) "
                "ORDER BY j.updated_at DESC, j.id LIMIT %(limit)s OFFSET %(offset)s"
            )
            rows = conn.execute(sql, params).fetchall()
            counts = conn.execute(
                "SELECT state, count(*) FROM video_jobs GROUP BY state"
            ).fetchall()
        return {
            "items": [
                {
                    "job_id": r[0],
                    "channel_id": r[1],
                    "cluster_id": r[2],
                    "state": r[3],
                    "origin": r[4],
                    "title": r[5],
                    "created_at": r[6],
                    "updated_at": r[7],
                    "approval_deadline": r[8],
                    "risk_score": r[9],
                    "youtube_video_id": r[10],
                }
                for r in rows
            ],
            "counts": {s: 0 for s in JOB_STATES} | {r[0]: r[1] for r in counts},
        }

    def timeline(self, job_id: str) -> list[dict[str, Any]] | None:
        with self._conn() as conn:
            exists = conn.execute("SELECT 1 FROM video_jobs WHERE id = %s", (job_id,)).fetchone()
            if exists is None:
                return None
            rows = conn.execute(
                "SELECT id, from_state, to_state, actor, at, payload FROM job_events "
                "WHERE job_id = %s ORDER BY id",
                (job_id,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "from_state": r[1],
                "to_state": r[2],
                "actor": r[3],
                "at": r[4],
                "payload": r[5],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ channels

    def channels(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT c.id, c.display_name, c.cluster_id, c.status, c.autonomy_tier, "
                "c.approval_tier, c.daily_budget_cents, c.audience_timezone, "
                "count(j.id) FILTER (WHERE j.state = 'awaiting_approval'), "
                "count(j.id) FILTER (WHERE j.state = ANY(%s)), "
                "count(j.id) FILTER (WHERE j.state IN ('published', 'live') "
                "                    AND j.updated_at >= now() - interval '7 days'), "
                "(SELECT min(slot_at) FROM daily_plans p WHERE p.channel_id = c.id "
                " AND p.status = 'active' AND p.slot_at > now()) "
                "FROM channels c LEFT JOIN video_jobs j ON j.channel_id = c.id "
                "GROUP BY c.id ORDER BY c.cluster_id NULLS LAST, c.id",
                (list(PRODUCTION_STATES),),
            ).fetchall()
            rates = {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT channel_id, avg((decision = 'approved')::int)::float "
                    "FROM review_outcomes WHERE created_at >= now() - interval '30 days' "
                    "GROUP BY channel_id"
                ).fetchall()
            }
        return [
            {
                "id": r[0],
                "name": r[1],
                "cluster_id": r[2],
                "status": r[3],
                "autonomy": r[4],
                "approval_tier": r[5],
                "daily_budget_cents": r[6],
                "timezone": r[7],
                "awaiting_approval": r[8],
                "in_production": r[9],
                "published_7d": r[10],
                "next_slot": r[11],
                "approval_rate_30d": rates.get(r[0]),
            }
            for r in rows
        ]

    def channel(self, channel_id: str) -> dict[str, Any] | None:
        rows = [c for c in self.channels() if c["id"] == channel_id]
        if not rows:
            return None
        detail = dict(rows[0])
        with self._conn() as conn:
            detail["knowledge"] = [
                {"kind": r[0], "version": r[1], "status": r[2], "proposed_at": r[3]}
                for r in conn.execute(
                    "SELECT DISTINCT ON (kind, status) kind, version, status, proposed_at "
                    "FROM knowledge_docs WHERE channel_id = %s AND status IN ('approved', 'draft') "
                    "ORDER BY kind, status, version DESC",
                    (channel_id,),
                ).fetchall()
            ]
            detail["recent_jobs"] = [
                {"job_id": r[0], "state": r[1], "title": r[2], "updated_at": r[3]}
                for r in conn.execute(
                    "SELECT id::text, state, coalesce(manifest->'metadata'->>'title', ''), "
                    "updated_at FROM video_jobs WHERE channel_id = %s "
                    "ORDER BY updated_at DESC LIMIT 8",
                    (channel_id,),
                ).fetchall()
            ]
            detail["strikes"] = [
                {"kind": r[0], "note": r[1], "created_at": r[2]}
                for r in conn.execute(
                    "SELECT kind, note, created_at FROM channel_strikes WHERE channel_id = %s "
                    "ORDER BY id DESC LIMIT 10",
                    (channel_id,),
                ).fetchall()
            ]
            detail["inputs_new"] = conn.execute(
                "SELECT count(*) FROM owner_inputs WHERE channel_id = %s AND status = 'new'",
                (channel_id,),
            ).fetchone()[0]
        return detail

    # ------------------------------------------------------------------ users (admin)

    def users(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT u.id::text, u.username, u.display_name, u.role, u.disabled_at, "
                "u.created_at, count(w.credential_id), max(w.last_used_at) "
                "FROM hub_users u LEFT JOIN webauthn_credentials w ON w.user_id = u.id "
                "GROUP BY u.id ORDER BY u.created_at"
            ).fetchall()
        return [
            {
                "id": r[0],
                "username": r[1],
                "display_name": r[2],
                "role": r[3],
                "disabled": r[4] is not None,
                "created_at": r[5],
                "passkeys": r[6],
                "last_used_at": r[7],
            }
            for r in rows
        ]


def q_scalar(cursor: Any) -> int:
    row = cursor.fetchone()
    return int(row[0]) if row else 0
