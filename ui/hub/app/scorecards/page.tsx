"use client";

import { useState } from "react";
import { Notice, PageHead, Segmented, SkeletonList } from "@/components/ui";
import { api } from "@/lib/api";
import { money, pct } from "@/lib/format";
import { useApi } from "@/lib/useApi";

export default function ScorecardsPage() {
  const [days, setDays] = useState("14");
  const { data, error, loading } = useApi(() => api.scorecards({ days }), [days]);
  return (
    <div className="stack">
      <PageHead title="Scorecards" sub="Daily results per channel." />
      <Segmented label="Range" value={days} onChange={setDays} options={[{ value: "7", label: "7 days" }, { value: "14", label: "14 days" }, { value: "30", label: "30 days" }]} />
      {error && <Notice tone="err">{error}</Notice>}
      {loading && !data && <SkeletonList />}
      <div className="table-wrap card">
        <table className="table">
          <thead><tr><th>Date</th><th>Channel</th><th>Approved</th><th>Flags</th><th>Published</th><th>Cost/video</th><th>CTR</th><th>Retention</th><th>Views</th></tr></thead>
          <tbody>
            {data?.map((r) => (
              <tr key={`${r.channel_id}-${r.score_date}`} data-testid="scorecard-row">
                <td className="mono">{r.score_date}</td><td>{r.channel_id}</td><td>{pct(r.approval_rate)}</td><td>{r.policy_flags}</td>
                <td>{r.videos_published}</td><td>{money(r.cost_per_video)}</td><td>{pct(r.avg_ctr)}</td><td>{pct(r.avg_retention)}</td><td>{r.views}</td>
              </tr>
            ))}
            {data?.length === 0 && <tr><td colSpan={9} className="muted">No scorecards yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
