"use client";

import Link from "next/link";
import { useState } from "react";
import { Empty, Notice, PageHead, RiskBadge, SkeletonList, StateBadge } from "@/components/ui";
import { api } from "@/lib/api";
import { PIPELINE_ORDER, STATE_LABEL, relTime } from "@/lib/format";
import { useApi } from "@/lib/useApi";

export default function PipelinePage() {
  const [state, setState] = useState("");
  const { data, error, loading } = useApi(() => api.pipeline({ state }), [state], 20_000);
  const counts = data?.counts ?? {};
  const stages = [...PIPELINE_ORDER, "rejected", "failed", "cancelled"];
  return (
    <div className="stack">
      <PageHead title="Pipeline" sub="Every job from idea to live video." />
      <div className="chips" role="group" aria-label="Filter by stage">
        <button className={`btn small ${state === "" ? "primary" : ""}`} onClick={() => setState("")}>All</button>
        {stages.filter((s) => counts[s]).map((s) => (
          <button key={s} className={`btn small ${state === s ? "primary" : ""}`} onClick={() => setState(s)}>{STATE_LABEL[s] ?? s} · {counts[s]}</button>
        ))}
      </div>
      {error && <Notice tone="err">{error}</Notice>}
      {loading && !data && <SkeletonList />}
      {data && data.items.length === 0 && <Empty icon="film" title="No jobs here" />}
      <div className="list card">
        {data?.items.map((j) => (
          <Link key={j.job_id} href={`/jobs/${j.job_id}`} className="item">
            <div className="item-main">
              <div className="item-title">{j.title || j.job_id}</div>
              <div className="item-sub">{j.channel_id} · updated {relTime(j.updated_at)}</div>
            </div>
            <div className="chips">{j.risk_score !== null && <RiskBadge risk={j.risk_score} />}<StateBadge state={j.state} /></div>
          </Link>
        ))}
      </div>
    </div>
  );
}
