"""Tier service: promotion recommendations, automatic demotion, strikes, spot checks, reports.

Rules for who may do what are enforced by the database (migration 013): a promotion needs an
admin-confirmed recommendation, a demotion needs a recorded reason. This module only decides
WHEN to recommend or demote, and writes through those guards.
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from ytcore.store.errors import TierRefused
from ytcore.store.postgres import Connect, map_db_error
from ytcore.tiers.rules import ChannelStats, TierRules, assess_demotion, assess_promotion

HUB_SYSTEM = "system:tiers"


class Rng(Protocol):
    def random(self) -> float: ...


@dataclass(frozen=True)
class Recommendation:
    id: int
    channel_id: str
    evidence: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class Demotion:
    channel_id: str
    reason: str


class TierService:
    def __init__(self, connect: Connect, *, rng: Rng | None = None) -> None:
        self._connect = connect
        self._rng: Rng = rng or random.SystemRandom()

    @classmethod
    def from_conninfo(cls, conninfo: str, **kw: Any) -> TierService:
        return cls(lambda: psycopg.connect(conninfo, autocommit=True), **kw)

    @contextmanager
    def _tx(self, actor: str) -> Iterator[psycopg.Connection[Any]]:
        try:
            with self._connect() as conn, conn.transaction():
                conn.execute("SELECT set_config('app.actor', %s, true)", (actor,))
                yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    # ------------------------------------------------------------------ rules

    def rules(self) -> TierRules:
        with self._tx(HUB_SYSTEM) as conn:
            row = conn.execute("SELECT rules FROM tier_rules").fetchone()
        return TierRules.model_validate(row[0]) if row else TierRules()

    # ------------------------------------------------------------------ measurements

    def stats(
        self, channel_id: str, rules: TierRules, *, now: datetime | None = None
    ) -> ChannelStats:
        now = now or datetime.now(UTC)
        p, d = rules.promotion, rules.demotion
        with self._tx(HUB_SYSTEM) as conn:
            first, n, rejected, edited, avg_risk = conn.execute(
                "SELECT min(created_at), count(*), "
                "count(*) FILTER (WHERE decision = 'rejected'), "
                "count(*) FILTER (WHERE decision = 'edited'), avg(risk_score) "
                "FROM review_outcomes WHERE channel_id = %s",
                (channel_id,),
            ).fetchone()  # type: ignore[misc]
            cost = conn.execute(
                "WITH jobs AS (SELECT DISTINCT job_id FROM review_outcomes WHERE channel_id = %s) "
                "SELECT (SELECT sum(cost_cents) FROM job_costs WHERE job_id IN "
                "(SELECT job_id FROM jobs))::float / nullif((SELECT count(*) FROM jobs), 0)",
                (channel_id,),
            ).fetchone()
            strikes = conn.execute(
                "SELECT count(*) FROM channel_strikes WHERE channel_id = %s AND created_at >= %s",
                (channel_id, now - timedelta(days=p.strike_free_days)),
            ).fetchone()
            flags = self._flags(conn, channel_id, now - timedelta(days=p.strike_free_days))
            recent_flags = self._flags(
                conn, channel_id, now - timedelta(days=d.policy_flag_window_days)
            )
            recent = conn.execute(
                "SELECT count(*), count(*) FILTER (WHERE decision = 'rejected') FROM ("
                "SELECT decision FROM review_outcomes WHERE channel_id = %s "
                "ORDER BY id DESC LIMIT %s) r",
                (channel_id, d.window_decisions),
            ).fetchone()
        return ChannelStats(
            days_observed=0.0 if first is None else (now - first).total_seconds() / 86400,
            decisions=int(n),
            rejected=int(rejected),
            edited=int(edited),
            avg_risk=None if avg_risk is None else float(avg_risk),
            cost_per_video_cents=None if cost is None or cost[0] is None else float(cost[0]),
            strikes=int(strikes[0]),  # type: ignore[index]
            policy_flags=flags,
            recent_decisions=int(recent[0]),  # type: ignore[index]
            recent_rejections=int(recent[1]),  # type: ignore[index]
            recent_policy_flags=recent_flags,
        )

    @staticmethod
    def _flags(conn: psycopg.Connection[Any], channel_id: str, since: datetime) -> int:
        """Jobs whose QA raised a blocking policy flag since `since`."""
        row = conn.execute(
            "SELECT count(DISTINCT q.job_id) FROM qa_reports q "
            "JOIN video_jobs j ON j.id = q.job_id WHERE j.channel_id = %s AND q.created_at >= %s "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements(q.flags) f "
            "            WHERE f->>'severity' = 'block')",
            (channel_id, since),
        ).fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------ evaluation

    def evaluate_promotions(self, *, now: datetime | None = None) -> list[Recommendation]:
        """Create a recommendation for every approve-all channel that earned one. Never promotes."""
        rules = self.rules()
        made: list[Recommendation] = []
        with self._tx(HUB_SYSTEM) as conn:
            channels = [
                r[0]
                for r in conn.execute(
                    "SELECT c.id FROM channels c WHERE c.approval_tier = 1 "
                    "AND c.autonomy_tier = 'gated' AND c.status = 'active' "
                    "AND NOT EXISTS (SELECT 1 FROM tier_recommendations t "
                    "                WHERE t.channel_id = c.id AND t.status = 'pending') "
                    "ORDER BY c.id"
                ).fetchall()
            ]
        for channel_id in channels:
            stats = self.stats(channel_id, rules, now=now)
            if assess_promotion(stats, rules):
                continue
            evidence = {**stats.evidence(), "rules": rules.promotion.model_dump(mode="json")}
            made.append(self.recommend(channel_id, evidence))
        return made

    def recommend(self, channel_id: str, evidence: dict[str, Any]) -> Recommendation:
        with self._tx(HUB_SYSTEM) as conn:
            row = conn.execute(
                "INSERT INTO tier_recommendations (channel_id, evidence) VALUES (%s, %s) "
                "RETURNING id, created_at",
                (channel_id, Jsonb(evidence)),
            ).fetchone()
            assert row is not None
            conn.execute(
                "INSERT INTO alerts (channel_id, kind, message) VALUES (%s, %s, %s)",
                (channel_id, "tier.recommended",
                 "channel earned a promotion recommendation; an admin must confirm it"),
            )  # fmt: skip
        return Recommendation(row[0], channel_id, evidence, row[1])

    def evaluate_demotions(self, *, now: datetime | None = None) -> list[Demotion]:
        """Return risky tier-2 channels to approve-all. Takes effect on queued jobs at once."""
        rules = self.rules()
        with self._tx(HUB_SYSTEM) as conn:
            channels = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM channels WHERE approval_tier = 2 ORDER BY id"
                ).fetchall()
            ]
        out: list[Demotion] = []
        for channel_id in channels:
            stats = self.stats(channel_id, rules, now=now)
            reason = assess_demotion(stats, rules)
            if reason is not None and self.demote(channel_id, reason, stats.evidence()):
                out.append(Demotion(channel_id, reason))
        return out

    def demote_if_needed(self, channel_id: str, *, now: datetime | None = None) -> Demotion | None:
        """Re-check one tier-2 channel right now (called after every human decision)."""
        with self._tx(HUB_SYSTEM) as conn:
            row = conn.execute(
                "SELECT approval_tier FROM channels WHERE id = %s", (channel_id,)
            ).fetchone()
        if row is None or row[0] != 2:
            return None
        rules = self.rules()
        stats = self.stats(channel_id, rules, now=now)
        reason = assess_demotion(stats, rules)
        if reason is not None and self.demote(channel_id, reason, stats.evidence()):
            return Demotion(channel_id, reason)
        return None

    def demote(
        self, channel_id: str, reason: str, evidence: dict[str, Any], *, actor: str = HUB_SYSTEM
    ) -> bool:
        with self._tx(actor) as conn:
            conn.execute("SELECT set_config('app.tier_reason', %s, true)", (reason,))
            conn.execute(
                "SELECT set_config('app.tier_evidence', %s, true)", (json.dumps(evidence),)
            )
            cur = conn.execute(
                "UPDATE channels SET approval_tier = 1 WHERE id = %s AND approval_tier = 2",
                (channel_id,),
            )
            return cur.rowcount == 1

    # ------------------------------------------------------------------ human decisions

    def confirm(
        self, recommendation_id: int, admin: str, note: str, *, recheck: bool = True
    ) -> None:
        """An admin confirms a recommendation; the promotion happens in the same transaction.
        The rules are re-checked on today's numbers first (a strike or flag since the
        recommendation blocks it). `recheck=False` is for test fixtures only."""
        if recheck:
            with self._tx(HUB_SYSTEM) as conn:
                found = conn.execute(
                    "SELECT channel_id FROM tier_recommendations WHERE id = %s "
                    "AND status = 'pending'",
                    (recommendation_id,),
                ).fetchone()
            if found is None:
                raise LookupError("no pending recommendation with that id")
            rules = self.rules()
            problems = assess_promotion(self.stats(found[0], rules), rules)
            if problems:
                raise TierRefused("no longer eligible: " + "; ".join(problems))
        with self._tx(f"hub:{admin}") as conn:
            row = conn.execute(
                "UPDATE tier_recommendations SET status = 'confirmed', decided_by = %s, "
                "note = %s WHERE id = %s AND status = 'pending' RETURNING channel_id",
                (admin, note, recommendation_id),
            ).fetchone()
            if row is None:
                raise LookupError("no pending recommendation with that id")
            cur = conn.execute(
                "UPDATE channels SET approval_tier = 2 WHERE id = %s AND approval_tier = 1",
                (row[0],),
            )
            if cur.rowcount != 1:
                raise LookupError("the channel is not on approve-all any more")

    def dismiss(self, recommendation_id: int, admin: str, note: str) -> None:
        with self._tx(f"hub:{admin}") as conn:
            row = conn.execute(
                "UPDATE tier_recommendations SET status = 'dismissed', decided_by = %s, "
                "note = %s WHERE id = %s AND status = 'pending' RETURNING id",
                (admin, note, recommendation_id),
            ).fetchone()
            if row is None:
                raise LookupError("no pending recommendation with that id")

    def record_strike(
        self, channel_id: str, kind: str, note: str, *, video_ref: str | None, actor: str
    ) -> int:
        with self._tx(actor) as conn:
            if (
                conn.execute("SELECT 1 FROM channels WHERE id = %s", (channel_id,)).fetchone()
                is None
            ):
                raise LookupError(f"no channel {channel_id!r}")
            row = conn.execute(
                "INSERT INTO channel_strikes (channel_id, kind, video_ref, note) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (channel_id, kind, video_ref, note),
            ).fetchone()
        assert row is not None
        return int(row[0])

    # ------------------------------------------------------------------ routing (spot checks)

    def spot_check(
        self,
        conn: psycopg.Connection[Any],
        job_id: str,
        content_hash: str,
        channel_id: str,
        percent: float,
    ) -> bool:
        """Draw once per content version whether this low-risk video is a spot check."""
        known = conn.execute(
            "SELECT spot_check FROM job_review_routing WHERE job_id = %s AND content_hash = %s",
            (job_id, content_hash),
        ).fetchone()
        if known is not None:
            return bool(known[0])
        earlier = conn.execute(
            "SELECT spot_check FROM job_review_routing WHERE job_id = %s ORDER BY drawn_at LIMIT 1",
            (job_id,),
        ).fetchone()
        # An edit gives new content a new hash but never a new draw (no re-rolling by editing).
        drawn = bool(earlier[0]) if earlier else self._rng.random() * 100 < percent
        conn.execute(
            "INSERT INTO job_review_routing "
            "(job_id, content_hash, channel_id, spot_check, percent) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (job_id, content_hash) DO NOTHING",
            (job_id, content_hash, channel_id, drawn, percent),
        )
        row = conn.execute(
            "SELECT spot_check FROM job_review_routing WHERE job_id = %s AND content_hash = %s",
            (job_id, content_hash),
        ).fetchone()
        return bool(row and row[0])

    # ------------------------------------------------------------------ reporting

    def overview(self) -> list[dict[str, Any]]:
        """Every channel with its tier, last change and pending recommendation."""
        with self._tx(HUB_SYSTEM) as conn:
            rows = conn.execute(
                "SELECT c.id, c.display_name, c.cluster_id, c.approval_tier, "
                "c.approval_tier_since, "
                "lc.kind, lc.reason, lc.created_at, pr.id, pr.evidence "
                "FROM channels c "
                "LEFT JOIN LATERAL (SELECT kind, reason, created_at FROM tier_changes t "
                "                   WHERE t.channel_id = c.id ORDER BY id DESC LIMIT 1) lc ON true "
                "LEFT JOIN tier_recommendations pr "
                "ON pr.channel_id = c.id AND pr.status = 'pending' "
                "ORDER BY c.cluster_id NULLS LAST, c.id"
            ).fetchall()
        return [
            {
                "channel_id": r[0], "name": r[1], "cluster_id": r[2], "tier": r[3],
                "tier_since": r[4], "last_change_kind": r[5], "last_change_reason": r[6],
                "last_change_at": r[7], "recommendation_id": r[8],
                "recommendation_evidence": r[9],
            }
            for r in rows
        ]  # fmt: skip

    def changes(self, limit: int = 100, channel_id: str | None = None) -> list[dict[str, Any]]:
        with self._tx(HUB_SYSTEM) as conn:
            rows = conn.execute(
                "SELECT id, channel_id, from_tier, to_tier, kind, reason, actor, evidence, "
                "recommendation_id, created_at FROM tier_changes "
                "WHERE (%s::text IS NULL OR channel_id = %s) ORDER BY id DESC LIMIT %s",
                (channel_id, channel_id, max(1, min(limit, 500))),
            ).fetchall()
        keys = ("id", "channel_id", "from_tier", "to_tier", "kind", "reason", "actor",
                "evidence", "recommendation_id", "created_at")  # fmt: skip
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def disagreement(self, channel_id: str | None = None) -> dict[str, Any]:
        """How often a human overruled a low risk score, and how risk buckets fare.

        `spot_checks` is the unbiased sample (tier-2 low-risk videos sent to deep review);
        `low_risk_all` includes every reviewed low-risk video, tier 1 included.
        """
        with self._tx(HUB_SYSTEM) as conn:
            rows = conn.execute(
                "SELECT spot_check, low_risk, decision, risk_score FROM review_outcomes "
                "WHERE (%s::text IS NULL OR channel_id = %s)",
                (channel_id, channel_id),
            ).fetchall()
        spot = [r for r in rows if r[0]]
        low = [r for r in rows if r[1]]

        def rate(sample: list[Any]) -> dict[str, Any]:
            bad = sum(1 for r in sample if r[2] != "approved")
            return {"n": len(sample), "disagreements": bad,
                    "rate": round(bad / len(sample), 4) if sample else None}  # fmt: skip

        buckets: dict[str, dict[str, int]] = {}
        for _, _, decision, risk in rows:
            if risk is None:
                continue
            key = f"{risk // 20 * 20}-{risk // 20 * 20 + 19}"
            b = buckets.setdefault(key, {"n": 0, "not_approved": 0})
            b["n"] += 1
            b["not_approved"] += decision != "approved"
        return {
            "spot_checks": rate(spot),
            "low_risk_all": rate(low),
            "by_risk_bucket": dict(
                sorted(buckets.items(), key=lambda kv: int(kv[0].split("-")[0]))
            ),
        }
