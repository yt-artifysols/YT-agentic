"use client";

import { Icon } from "@/components/icons";
import { Empty, Notice, PageHead, SkeletonList } from "@/components/ui";
import { api } from "@/lib/api";
import { dateTime, relTime } from "@/lib/format";
import { useApi } from "@/lib/useApi";

export default function AlertsPage() {
  const { data, error, loading } = useApi(() => api.alerts(), [], 30_000);
  return (
    <div className="stack">
      <PageHead title="Alerts" sub="Things the system noticed. Nothing here publishes anything." />
      {error && <Notice tone="err">{error}</Notice>}
      {loading && !data && <SkeletonList />}
      {data?.length === 0 && <Empty title="No alerts" />}
      <div className="list card">
        {data?.map((a) => (
          <div key={a.id} className="item">
            <Icon name="alert" size={18} />
            <div className="item-main">
              <div className="item-title">{a.message}</div>
              <div className="item-sub">{a.kind} · {a.channel_id ?? a.cluster_id ?? "fleet"} · <span title={dateTime(a.created_at)}>{relTime(a.created_at)}</span></div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
