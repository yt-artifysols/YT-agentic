"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Badge, Card, LinkButton, Notice, PageHead, SkeletonList, StateBadge } from "@/components/ui";
import { api } from "@/lib/api";
import { money, pct, relTime } from "@/lib/format";
import { useApi } from "@/lib/useApi";

export function ChannelView() {
  const { id } = useParams<{ id: string }>();
  const { data: c, error } = useApi(() => api.channel(id), [id]);
  if (error) return <Notice tone="err">{error}</Notice>;
  if (!c) return <SkeletonList rows={6} />;
  return (
    <div className="stack">
      <PageHead title={c.name} sub={`${c.id} · ${c.cluster_id ?? "no cluster"} · ${c.timezone}`} actions={<LinkButton href="/inputs" icon="idea">Add an idea</LinkButton>} />
      <div className="chips">
        <Badge tone={c.status === "paused" ? "bad" : "ok"}>{c.status === "paused" ? "Paused" : "Active"}</Badge>
        <Badge>Tier {c.approval_tier}</Badge>
        <Badge>Autonomy: {c.autonomy}</Badge>
        <Badge>Approval rate {pct(c.approval_rate_30d)}</Badge>
        <Badge>Budget {money(c.daily_budget_cents)}/day</Badge>
        <Badge tone={c.inputs_new ? "brand" : ""}>{c.inputs_new} new input(s)</Badge>
      </div>
      {c.strikes.length > 0 && (
        <Notice tone="warn">{c.strikes.length} strike(s): {c.strikes.map((s) => `${s.kind} (${relTime(s.created_at)})`).join(", ")}. {c.strikes[0]?.note}</Notice>
      )}
      <div className="grid cols-2">
        <Card title="Recent videos" pad={false}>
          <div className="list">
            {c.recent_jobs.map((j) => (
              <Link key={j.job_id} href={`/jobs/${j.job_id}`} className="item">
                <div className="item-main"><div className="item-title">{j.title}</div><div className="item-sub">{relTime(j.updated_at)}</div></div>
                <StateBadge state={j.state} />
              </Link>
            ))}
          </div>
        </Card>
        <Card title="Knowledge (versioned, human-approved)" pad={false}>
          <div className="list">
            {c.knowledge.map((k) => (
              <div key={k.kind} className="item"><div className="item-main"><div className="item-title">{k.kind.replace(/_/g, " ")}</div><div className="item-sub">version {k.version}</div></div><Badge tone={k.status === "approved" ? "ok" : "warn"}>{k.status}</Badge></div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
