"use client";

import Link from "next/link";
import { Badge, Button, Card, LinkButton, Notice, PageHead, Progress, SkeletonList, Stat, StateBadge } from "@/components/ui";
import { Icon } from "@/components/icons";
import { api } from "@/lib/api";
import { countdown, money, relTime } from "@/lib/format";
import { useApi } from "@/lib/useApi";

export default function TodayPage() {
  const { data: ov, error, loading, reload } = useApi(() => api.overview(), [], 30_000);
  if (error && !ov) return <Notice tone="err">{error}</Notice>;
  if (loading || !ov) return <SkeletonList rows={5} />;
  const due = countdown(ov.awaiting_approval.oldest_deadline);
  const budgetPct = ov.daily_budget_cents ? (ov.cost_today_cents / ov.daily_budget_cents) * 100 : 0;
  return (
    <div className="stack">
      <PageHead
        title="Today"
        sub={`${ov.channels} channels · updated ${relTime(ov.generated_at)}`}
        actions={<Button size="small" icon="zap" onClick={() => void reload()}>Refresh</Button>}
      />
      {ov.awaiting_approval.count > 0 ? (
        <Link href="/review" className="card card-pad row between" style={{ borderColor: "var(--brand)" }}>
          <div>
            <div className="strong" style={{ fontSize: 18 }}>
              {ov.awaiting_approval.count} {ov.awaiting_approval.count === 1 ? "video needs" : "videos need"} your approval
            </div>
            <div className="muted small">
              Oldest deadline: {due.text}
              {ov.awaiting_approval.due_within_2h > 0 && ` · ${ov.awaiting_approval.due_within_2h} due within 2 hours`}
            </div>
          </div>
          <span className="btn primary">Start reviewing</span>
        </Link>
      ) : (
        <Notice tone="ok">Nothing is waiting for you. New videos appear here when they pass the quality check.</Notice>
      )}
      <div className="stats">
        <Stat label="In production" value={ov.in_production} icon="film" href="/pipeline" />
        <Stat label="Live (24 h)" value={ov.last_24h.live} icon="play" hint={`${ov.last_24h.rejected} rejected`} />
        <Stat label="Alerts (24 h)" value={ov.alerts_24h} icon="bell" href="/alerts" tone={ov.alerts_24h ? "attention" : ""} />
        <Stat label="Spend today" value={money(ov.cost_today_cents)} icon="chart" hint={`of ${money(ov.daily_budget_cents)} budget`} />
      </div>
      <Progress value={budgetPct} tone={budgetPct > 90 ? "bad" : budgetPct > 70 ? "warn" : ""} />
      <div className="grid cols-2">
        <Card title="Recent activity" pad={false}>
          <div className="list">
            {ov.activity.map((a) => (
              <Link key={a.id} href={`/jobs/${a.job_id}`} className="item">
                <div className="item-main">
                  <div className="item-title">{a.title || a.job_id}</div>
                  <div className="item-sub">{a.channel_id} · {relTime(a.at)}</div>
                </div>
                <StateBadge state={a.to_state} />
              </Link>
            ))}
          </div>
        </Card>
        <div className="stack">
          <Card title="Alerts" action={<Link href="/alerts" className="small">All</Link>} pad={false}>
            <div className="list">
              {ov.alerts.length === 0 && <div className="muted">No alerts.</div>}
              {ov.alerts.map((a) => (
                <div key={a.id} className="item">
                  <Icon name="alert" size={18} />
                  <div className="item-main">
                    <div className="item-title">{a.message}</div>
                    <div className="item-sub">{a.channel_id ?? a.cluster_id ?? "fleet"} · {relTime(a.created_at)}</div>
                  </div>
                </div>
              ))}
            </div>
          </Card>
          <Card title="Clusters" pad={false}>
            <div className="list">
              {ov.clusters.map((c) => (
                <Link key={c.id} href="/clusters" className="item">
                  <div className="item-main">
                    <div className="item-title">{c.name}</div>
                    <div className="item-sub">{c.channels} channels · {c.awaiting_approval} waiting</div>
                  </div>
                  <Badge tone={c.live_enabled ? "ok" : ""}>{c.live_enabled ? "Live" : "Not live yet"}</Badge>
                </Link>
              ))}
            </div>
          </Card>
          {ov.tiers.pending_recommendations > 0 && (
            <LinkButton href="/tiers" icon="shield">{ov.tiers.pending_recommendations} tier recommendation(s) to review</LinkButton>
          )}
        </div>
      </div>
    </div>
  );
}
