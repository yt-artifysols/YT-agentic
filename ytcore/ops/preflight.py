"""Go-live preflight: a read-only go/no-go report for a database (and optionally its secrets).

    ytctl ops preflight           (exit 1 when any check fails)

It never writes, never prints a secret value, and never changes a kill switch. FAIL blocks
go-live, WARN needs a human look, OK passes. Things only a person can verify (YouTube Studio,
OAuth consent, backups restored) stay in docs/runbooks/go-live.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import psycopg

from ytcore.db.migrate import migration_files
from ytcore.observability.monitor import OpsMonitor
from ytcore.ops import reconcile
from ytcore.secrets.provider import SecretRef, SecretsProvider
from ytcore.store.postgres import Connect

Status = Literal["ok", "warn", "fail"]
REQUIRED_DOCS = ("brand_voice", "policy", "banned_claims")


@dataclass(frozen=True)
class Check:
    status: Status
    name: str
    detail: str = ""


def _q(conn: psycopg.Connection[Any], sql: str, *params: Any) -> list[tuple[Any, ...]]:
    return conn.execute(sql, params or None).fetchall()


def _migrations(conn: psycopg.Connection[Any]) -> Check:
    applied = {r[0] for r in _q(conn, "SELECT name FROM schema_migrations")}
    missing = [p.name for p in migration_files() if p.name not in applied]
    if missing:
        return Check("fail", "migrations applied", "missing: " + ", ".join(missing))
    return Check("ok", "migrations applied", f"{len(applied)} applied")


def _audit(conn: psycopg.Connection[Any]) -> Check:
    broken = _q(conn, "SELECT audit_chain_first_break()")[0][0]
    if broken is not None:
        return Check("fail", "audit chain intact", f"first break at event {broken}")
    return Check("ok", "audit chain intact")


def _kill_switches(conn: psycopg.Connection[Any]) -> list[Check]:
    paused = _q(conn, "SELECT publishing_paused FROM global_controls")[0][0]
    out = [
        Check(
            "warn" if paused else "ok",
            "global kill switch",
            "ENGAGED: nothing can be approved or published until it is released"
            if paused
            else "released",
        )
    ]
    held = _q(
        conn,
        "SELECT 'cluster:' || id FROM clusters WHERE status <> 'active' "
        "UNION ALL SELECT 'channel:' || id FROM channels WHERE status <> 'active'",
    )
    if held:
        out.append(Check("warn", "scoped kill switches", ", ".join(r[0] for r in held)))
    return out


def _roles(conn: psycopg.Connection[Any]) -> list[Check]:
    """Each service logs in as a member of exactly one service role (migration 014)."""
    rows = _q(
        conn,
        "SELECT g.rolname, m.rolname, m.rolsuper FROM pg_roles g "
        "JOIN pg_auth_members a ON a.roleid = g.oid JOIN pg_roles m ON m.oid = a.member "
        "WHERE g.rolname IN ('yt_hub', 'yt_worker', 'yt_gateway', 'yt_ops')",
    )
    by_login: dict[str, set[str]] = {}
    for role, login, _ in rows:
        by_login.setdefault(login, set()).add(role)
    problems = [f"{login} is in {sorted(r)}" for login, r in by_login.items() if len(r) > 1]
    problems += [f"{login} is a superuser" for _, login, sup in rows if sup]
    missing = sorted({"yt_hub", "yt_worker", "yt_gateway", "yt_ops"} - {r[0] for r in rows})
    if problems:
        return [Check("fail", "one database role per service", "; ".join(problems))]
    if missing:
        return [
            Check("warn", "one database role per service", "no login yet for " + ", ".join(missing))
        ]
    return [Check("ok", "one database role per service", f"{len(by_login)} service login(s)")]


def _channels(conn: psycopg.Connection[Any], secrets: SecretsProvider | None) -> list[Check]:
    rows = _q(
        conn,
        "SELECT c.id, c.youtube_credentials_ref, c.google_project_id, c.daily_budget_cents, "
        "c.approval_tier, k.id, k.live_enabled FROM channels c "
        "LEFT JOIN clusters k ON k.id = c.cluster_id WHERE c.autonomy_tier = 'gated'",
    )
    if not rows:
        return [Check("warn", "gated channels", "none: every channel is still in shadow mode")]
    out = [Check("ok", "gated channels", f"{len(rows)} channel(s)")]
    approved = {
        (r[0], r[1])
        for r in _q(conn, "SELECT channel_id, kind FROM knowledge_docs WHERE status = 'approved'")
    }
    for cid, cred, project, budget, tier, cluster, live in rows:
        problems: list[str] = []
        if not cred or not project:
            problems.append("no OAuth credentials reference or Google project")
        if not budget:
            problems.append("daily budget is zero")
        if cluster is None or not live:
            problems.append("its cluster is not enabled for live publishing")
        missing = [k for k in REQUIRED_DOCS if (cid, k) not in approved]
        if missing:
            problems.append("unapproved knowledge: " + ", ".join(missing))
        if secrets is not None and cred:
            try:
                resolved = bool(secrets.get(SecretRef(cred)).reveal())
            except Exception:  # missing, denied, unconfigured
                resolved = False
            if not resolved:
                problems.append("credentials reference does not resolve")
        if tier == 2 and not _q(conn, "SELECT 1 FROM tier_rules"):
            problems.append("risk-based tier without tier rules")
        out.append(
            Check("fail", f"channel {cid} ready", "; ".join(problems))
            if problems
            else Check("ok", f"channel {cid} ready")
        )
    return out


def _operations(connect: Connect) -> list[Check]:
    findings = OpsMonitor(connect).findings()
    pending = reconcile.pending_uploads(connect)
    return [
        Check(
            "fail" if findings else "ok",
            "no stuck jobs, budget or quota alerts",
            "; ".join(f"{f.kind} {f.subject}" for f in findings[:8]),
        ),
        Check(
            "fail" if pending else "ok",
            "no uploads left pending",
            f"{len(pending)} pending: run `ytctl ops pending-uploads`" if pending else "",
        ),
    ]


def run(connect: Connect, *, secrets: SecretsProvider | None = None) -> list[Check]:
    with connect() as conn:
        checks = [_migrations(conn), _audit(conn), *_roles(conn), *_kill_switches(conn)]
        checks += _channels(conn, secrets)
    return checks + _operations(connect)


def render(checks: list[Check]) -> str:
    mark = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[FAIL]"}
    return "\n".join(
        f"{mark[c.status]} {c.name}" + (f": {c.detail}" if c.detail else "") for c in checks
    )


def go(checks: list[Check]) -> bool:
    return all(c.status != "fail" for c in checks)
