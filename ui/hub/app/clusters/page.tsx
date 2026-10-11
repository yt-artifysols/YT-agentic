"use client";

import { useEffect, useState } from "react";
import { api, type ClusterRow, type Gate } from "@/lib/api";
import { useMe } from "@/lib/useMe";

export default function ClustersPage() {
  const me = useMe();
  const [rows, setRows] = useState<ClusterRow[]>([]);
  const [gates, setGates] = useState<Record<string, Gate>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const load = () => api.clusters().then(setRows).catch((e: Error) => setMessage({ kind: "err", text: e.message }));
  useEffect(() => {
    if (me) void load();
  }, [me]);

  if (!me) return null;
  const canSee = me.role !== "approver";
  const act = async (fn: () => Promise<unknown>, ok: string) => {
    try {
      await fn();
      setMessage({ kind: "ok", text: ok });
      await load();
    } catch (e) {
      setMessage({ kind: "err", text: (e as Error).message });
    }
  };

  return (
    <>
      <h1>Clusters</h1>
      {message && <div className={`notice ${message.kind}`} role="status">{message.text}</div>}
      <div className="card" style={{ padding: 0, overflowX: "auto" }}>
        <table>
          <thead>
            <tr><th>Cluster</th><th>Niche</th><th>Status</th><th>Live</th><th>Channels</th><th>Waiting</th><th>Oldest waiting</th><th>Supervisor</th><th /></tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.id} data-testid="cluster-row">
                <td className="mono">{c.id}</td>
                <td>{c.niche || "-"}</td>
                <td>{c.status}</td>
                <td>
                  {c.live_enabled ? `live since ${c.live_since ? new Date(c.live_since).toLocaleDateString() : "?"}` : `dark (needs ${c.requires_cluster ?? "-"})`}
                </td>
                <td>{c.channels}</td>
                <td>{c.awaiting_approval}</td>
                <td className="mono">{c.oldest_waiting_since ? new Date(c.oldest_waiting_since).toLocaleString() : "-"}</td>
                <td className="mono">
                  {c.health ? `${c.health.running} running / ${c.health.pending} pending${c.health.budget_held ? " (budget held)" : ""}` : "-"}
                </td>
                <td>
                  {canSee && <button onClick={() => api.gate(c.id).then((g) => setGates({ ...gates, [c.id]: g }))}>Gate report</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {Object.values(gates).map((g) => (
        <div className="card" key={g.cluster_id} style={{ marginTop: 12 }}>
          <h3>Gate: {g.cluster_id}</h3>
          <p>
            Live {g.live_days.toFixed(1)} of {g.required_days} days · {g.decisions} human decisions
            {g.rejection_rate !== null && ` (${Math.round(g.rejection_rate * 100)}% rejected)`} · {g.scorecard_days} scorecards ·
            {` ${g.breaker_trips} breaker trips · ${g.stuck_or_plan_alerts} alerts · ${g.failed_uploads} failed uploads`}
          </p>
          {g.problems.length ? (
            <ul>{g.problems.map((p) => <li key={p}>{p}</li>)}</ul>
          ) : (
            <p className="notice ok">The measured evidence is good.</p>
          )}
          {me.role === "admin" && (
            <div className="row">
              <input
                placeholder="Your review: what you looked at and why this cluster is stable (20+ characters)"
                value={notes[g.cluster_id] ?? ""}
                onChange={(e) => setNotes({ ...notes, [g.cluster_id]: e.target.value })}
                style={{ flex: 1 }}
              />
              <button disabled={!g.ok} onClick={() => act(() => api.signoff(g.cluster_id, notes[g.cluster_id] ?? ""), "Signed off")}>
                Sign off
              </button>
              {rows.filter((r) => r.requires_cluster === g.cluster_id && !r.live_enabled).map((r) => (
                <button key={r.id} className="primary" onClick={() => act(() => api.enableCluster(r.id), `${r.id} enabled`)}>
                  Enable {r.id}
                </button>
              ))}
            </div>
          )}
        </div>
      ))}
    </>
  );
}
