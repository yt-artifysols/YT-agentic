"""Apply the fleet configuration to the database, idempotently.

Running it twice changes nothing the second time. Rules that keep it safe:
  * it never turns a cluster live on an EXISTING cluster (rollout is a human decision in the hub;
    new clusters start live only if their file says so and nothing is required first);
  * brand and policy become knowledge DRAFTS proposed by `config:<slug>`; humans approve them;
  * it never moves a channel to another cluster, never deletes a channel;
  * credentials are stored as vault references, never values.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import psycopg

from ytcore.channels.config import ChannelCfg, ClusterCfg, ConfigError, FleetCfg
from ytcore.knowledge.models import KnowledgeKind
from ytcore.knowledge.store import PostgresChannelKnowledge
from ytcore.store.errors import StoreError
from ytcore.store.postgres import Connect, map_db_error

ACTOR = "config"


@dataclass
class ApplyReport:
    projects: dict[str, str] = field(default_factory=dict)
    clusters: dict[str, str] = field(default_factory=dict)
    channels: dict[str, str] = field(default_factory=dict)
    pools: int = 0
    drafts: dict[str, dict[str, str]] = field(default_factory=dict)  # slug -> kind -> state
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return any(
            v != "unchanged"
            for group in (self.projects, self.clusters, self.channels)
            for v in group.values()
        ) or any(s == "proposed" for d in self.drafts.values() for s in d.values())


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


class _Rollback(Exception):
    pass


def apply_fleet(connect: Connect, fleet: FleetCfg, *, dry_run: bool = False) -> ApplyReport:
    report = ApplyReport(dry_run=dry_run)
    try:
        with connect() as conn:
            try:
                with conn.transaction():
                    conn.execute("SELECT set_config('app.actor', %s, true)", (ACTOR,))
                    _apply_sql(conn, fleet, report)
                    if dry_run:
                        raise _Rollback
            except _Rollback:
                pass
    except psycopg.Error as exc:
        raise map_db_error(exc) from exc
    for slug, ch in fleet.channels.items():
        report.drafts[slug] = _knowledge(connect, ch, dry_run)
    return report


def _apply_sql(conn: psycopg.Connection[Any], fleet: FleetCfg, report: ApplyReport) -> None:
    for cluster in fleet.clusters.values():
        for p in cluster.google_projects:
            before = conn.execute(
                "SELECT daily_quota_units, oauth_client_ref FROM google_projects WHERE id = %s",
                (p.id,),
            ).fetchone()
            conn.execute(
                "INSERT INTO google_projects (id, daily_quota_units, oauth_client_ref) "
                "VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
                "daily_quota_units = EXCLUDED.daily_quota_units, "
                "oauth_client_ref = EXCLUDED.oauth_client_ref",
                (p.id, p.daily_quota_units, p.oauth_client_ref),
            )
            report.projects[p.id] = _state(before, (p.daily_quota_units, p.oauth_client_ref))
    # Clusters in dependency order, so a required cluster exists first.
    for cluster in _in_order(fleet.clusters):
        _cluster(conn, cluster, report)
    for ch in fleet.channels.values():
        _channel(conn, fleet, ch, report)


def _state(before: Any, after: tuple[Any, ...]) -> str:
    if before is None:
        return "created"
    return "unchanged" if tuple(before) == after else "updated"


def _in_order(clusters: dict[str, ClusterCfg]) -> Iterator[ClusterCfg]:
    done: set[str] = set()
    pending = dict(clusters)
    while pending:
        progressed = False
        for cid, cfg in list(pending.items()):
            if cfg.requires_cluster is None or cfg.requires_cluster in done:
                yield cfg
                done.add(cid)
                del pending[cid]
                progressed = True
        if not progressed:  # validate_fleet rejects cycles; this is a backstop
            raise ConfigError("cluster dependency cycle")


def _cluster(conn: psycopg.Connection[Any], cfg: ClusterCfg, report: ApplyReport) -> None:
    cols = (
        "display_name, niche, requires_cluster, gate_days, slot_spacing_minutes, "
        "max_concurrent_jobs, daily_budget_cents"
    )
    before = conn.execute(f"SELECT {cols} FROM clusters WHERE id = %s", (cfg.id,)).fetchone()  # noqa: S608
    values = (
        cfg.display_name, cfg.niche, cfg.requires_cluster, cfg.gate_days,
        cfg.slot_spacing_minutes, cfg.max_concurrent_jobs, cfg.daily_budget_cents,
    )  # fmt: skip
    if before is None:
        conn.execute(
            "INSERT INTO clusters (id, display_name, niche, requires_cluster, gate_days, "
            "slot_spacing_minutes, max_concurrent_jobs, daily_budget_cents, live_enabled) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (cfg.id, *values, cfg.live),
        )
    elif tuple(before) != values:
        conn.execute(
            "UPDATE clusters SET display_name = %s, niche = %s, requires_cluster = %s, "
            "gate_days = %s, slot_spacing_minutes = %s, max_concurrent_jobs = %s, "
            "daily_budget_cents = %s WHERE id = %s",
            (*values, cfg.id),
        )
    report.clusters[cfg.id] = _state(before, values)
    report.pools += _pools(conn, "cluster", cfg.id, {str(k): v for k, v in cfg.pools.items()})


def _pools(conn: psycopg.Connection[Any], scope: str, scope_id: str, limits: dict[str, int]) -> int:
    conn.execute(
        "DELETE FROM pool_limits WHERE scope = %s AND scope_id = %s AND NOT (pool = ANY(%s))",
        (scope, scope_id, list(limits)),
    )
    for pool, n in limits.items():
        conn.execute(
            "INSERT INTO pool_limits (scope, scope_id, pool, max_concurrent) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (scope, scope_id, pool) "
            "DO UPDATE SET max_concurrent = EXCLUDED.max_concurrent",
            (scope, scope_id, pool, n),
        )
    return len(limits)


def _channel(
    conn: psycopg.Connection[Any], fleet: FleetCfg, ch: ChannelCfg, report: ApplyReport
) -> None:
    project = fleet.project_of(ch)
    cols = (
        "display_name, cluster_id, autonomy_tier, daily_budget_cents, job_budget_cents, "
        "google_project_id, youtube_credentials_ref, analytics_credentials_ref, "
        "audience_timezone, publish_hour_local, publish_window_minutes, approval_lead_minutes"
    )
    before = conn.execute(f"SELECT {cols} FROM channels WHERE id = %s", (ch.slug,)).fetchone()  # noqa: S608
    values = (
        ch.display_name, ch.cluster, ch.autonomy_tier, ch.budget.daily_cents, ch.budget.job_cents,
        project.id, ch.youtube_credentials_ref, ch.analytics_credentials_ref,
        ch.audience_timezone, ch.publish.hour_local, ch.publish.window_minutes,
        ch.publish.approval_lead_minutes,
    )  # fmt: skip
    if before is None:
        conn.execute(
            "INSERT INTO channels (id, storage_prefix, memory_namespace, display_name, "
            "cluster_id, autonomy_tier, daily_budget_cents, job_budget_cents, google_project_id, "
            "youtube_credentials_ref, analytics_credentials_ref, audience_timezone, "
            "publish_hour_local, publish_window_minutes, approval_lead_minutes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (ch.slug, f"channels/{ch.slug}/", f"mem:{ch.slug}", *values),
        )
    elif tuple(before) != values:
        if before[1] != ch.cluster:
            raise ConfigError(f"channel {ch.slug}: moving to another cluster is not supported")
        conn.execute(
            "UPDATE channels SET display_name = %s, autonomy_tier = %s, daily_budget_cents = %s, "
            "job_budget_cents = %s, google_project_id = %s, youtube_credentials_ref = %s, "
            "analytics_credentials_ref = %s, audience_timezone = %s, publish_hour_local = %s, "
            "publish_window_minutes = %s, approval_lead_minutes = %s WHERE id = %s",
            (values[0], *values[2:], ch.slug),
        )
    report.channels[ch.slug] = _state(before, values)
    report.pools += _pools(conn, "channel", ch.slug, {str(k): v for k, v in ch.pools.items()})


def _knowledge(connect: Connect, ch: ChannelCfg, dry_run: bool) -> dict[str, str]:
    """Propose each configured document as a draft unless that exact text already exists."""
    repo = PostgresChannelKnowledge(connect, ch.slug)
    try:
        existing = repo.latest_approved()
        drafts = repo.drafts()
    except StoreError:  # channel not created yet (dry run on a fresh database)
        return {k.value: "would propose" for k in ch.knowledge()}
    out: dict[str, str] = {}
    for kind, content in ch.knowledge().items():
        digest = _sha(content)
        approved = existing.docs.get(kind)
        if approved is not None and approved.content_sha256 == digest:
            out[kind.value] = "approved"
        elif any(d.kind is kind and d.content_sha256 == digest for d in drafts):
            out[kind.value] = "pending"
        elif dry_run:
            out[kind.value] = "would propose"
        else:
            repo.propose(
                kind,
                content,
                proposed_by=f"{ACTOR}:{ch.slug}",
                base_version=approved.version if approved else None,
            )
            out[kind.value] = "proposed"
    return out


__all__ = ["ApplyReport", "KnowledgeKind", "apply_fleet"]
