"use client";

import { useEffect, useState } from "react";
import { api, type Scorecard } from "@/lib/api";
import { useMe } from "@/lib/useMe";

const pct = (v: number | null) => (v === null ? "-" : `${(v * 100).toFixed(1)}%`);

export default function ScorecardsPage() {
  const me = useMe();
  const [filters, setFilters] = useState({ channel: "", cluster: "", days: "14" });
  const [rows, setRows] = useState<Scorecard[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!me) return;
    api.scorecards(filters).then(setRows).catch((e: Error) => setError(e.message));
  }, [me, filters]);

  if (!me) return null;
  const set = (k: keyof typeof filters) => (e: { target: { value: string } }) => setFilters({ ...filters, [k]: e.target.value });

  return (
    <>
      <h1>Channel scorecards</h1>
      <div className="card row" style={{ marginBottom: 12 }}>
        <label>Cluster<input value={filters.cluster} onChange={set("cluster")} size={10} /></label>
        <label>Channel<input value={filters.channel} onChange={set("channel")} size={12} /></label>
        <label>Days<input type="number" min={1} max={90} value={filters.days} onChange={set("days")} style={{ width: 80 }} /></label>
      </div>
      {error && <div className="notice err" role="status">{error}</div>}
      <div className="card" style={{ padding: 0, overflowX: "auto" }}>
        <table>
          <thead>
            <tr><th>Date</th><th>Channel</th><th>Approval rate</th><th>Policy flags</th><th>Published</th><th>Cost / video</th><th>CTR</th><th>Retention</th><th>Views</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.channel_id}-${r.score_date}`} data-testid="scorecard-row">
                <td className="mono">{r.score_date}</td>
                <td className="mono">{r.channel_id}</td>
                <td>{pct(r.approval_rate)}</td>
                <td>{r.policy_flags}</td>
                <td>{r.videos_published}</td>
                <td>{r.cost_per_video === null ? "-" : `${(r.cost_per_video / 100).toFixed(2)}`}</td>
                <td>{pct(r.avg_ctr)}</td>
                <td>{pct(r.avg_retention)}</td>
                <td>{r.views}</td>
              </tr>
            ))}
            {!rows.length && <tr><td colSpan={9} className="muted">No scorecards yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
