"use client";

import Link from "next/link";
import { Badge, Notice, PageHead, SkeletonList } from "@/components/ui";
import { api } from "@/lib/api";
import { money, pct, relTime } from "@/lib/format";
import { useApi } from "@/lib/useApi";

export default function ChannelsPage() {
  const { data, error, loading } = useApi(() => api.channels(), [], 30_000);
  return (
    <div className="stack">
      <PageHead title="Channels" sub="Each channel has its own policy, memory, budget and OAuth token." />
      {error && <Notice tone="err">{error}</Notice>}
      {loading && !data && <SkeletonList />}
      <div className="grid cols-2">
        {data?.map((c) => (
          <Link key={c.id} href={`/channels/${c.id}`} className="card card-pad stack-sm">
            <div className="row between nowrap">
              <div className="strong">{c.name}</div>
              <Badge tone={c.status === "paused" ? "bad" : "ok"}>{c.status === "paused" ? "Paused" : "Active"}</Badge>
            </div>
            <div className="faint small">{c.cluster_id ?? "no cluster"} · {c.timezone} · tier {c.approval_tier}</div>
            <div className="chips">
              <Badge tone={c.awaiting_approval ? "warn" : ""}>{c.awaiting_approval} waiting</Badge>
              <Badge>{c.in_production} in production</Badge>
              <Badge>{c.published_7d} live (7 d)</Badge>
            </div>
            <div className="faint small">Approval rate {pct(c.approval_rate_30d)} · budget {money(c.daily_budget_cents)}/day · next slot {relTime(c.next_slot)}</div>
          </Link>
        ))}
      </div>
    </div>
  );
}
