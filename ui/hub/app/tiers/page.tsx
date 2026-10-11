"use client";

import { useEffect, useState } from "react";
import { api, type TierChange, type TierReport, type TierRow } from "@/lib/api";
import { useMe } from "@/lib/useMe";

const pct = (r: number | null) => (r === null ? "-" : `${(r * 100).toFixed(0)}%`);

export default function TiersPage() {
  const me = useMe();
  const [rows, setRows] = useState<TierRow[]>([]);
  const [changes, setChanges] = useState<TierChange[]>([]);
  const [report, setReport] = useState<TierReport | null>(null);
  const [notes, setNotes] = useState<Record<number, string>>({});
  const [message, setMessage] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const load = () =>
    Promise.all([api.tiers(), api.tierChanges(), api.tierReport()])
      .then(([r, c, d]) => {
        setRows(r);
        setChanges(c);
        setReport(d);
      })
      .catch((e: Error) => setMessage({ kind: "err", text: e.message }));
  useEffect(() => {
    if (me) void load();
  }, [me]);

  if (!me) return null;
  const admin = me.role === "admin";
  const decide = async (id: number, kind: "confirm" | "dismiss") => {
    try {
      const note = notes[id] ?? "";
      await (kind === "confirm" ? api.confirmTier(id, note) : api.dismissTier(id, note));
      setMessage({ kind: "ok", text: kind === "confirm" ? "Channel promoted to the risk-based tier." : "Recommendation dismissed." });
      await load();
    } catch (e) {
      setMessage({ kind: "err", text: (e as Error).message });
    }
  };

  return (
    <>
      <h1>Approval tiers</h1>
      <p className="muted">
        Tier 1 reviews every video one by one. Tier 2 sends low-risk videos to a one-tap fast lane (still one
        approval per video) and the rest to deep review. A promotion is only ever a recommendation that an admin confirms;
        a strike, a rejection spike or a policy flag returns the channel to tier 1 at once.
      </p>
      {message && <div className={`notice ${message.kind}`} role="status">{message.text}</div>}
      <div className="card" style={{ padding: 0, overflowX: "auto" }}>
        <table>
          <thead><tr><th>Channel</th><th>Cluster</th><th>Tier</th><th>Since</th><th>Last change</th><th>Recommendation</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.channel_id} data-testid="tier-row">
                <td className="mono">{r.channel_id}</td>
                <td>{r.cluster_id ?? "-"}</td>
                <td><span className={`badge ${r.tier === 2 ? "risk-mid" : ""}`}>tier {r.tier}</span></td>
                <td className="mono">{new Date(r.tier_since).toLocaleDateString()}</td>
                <td>{r.last_change_kind ? `${r.last_change_kind}: ${r.last_change_reason}` : "-"}</td>
                <td>
                  {r.recommendation_id !== null && (
                    <div>
                      <div className="mono muted">
                        {Object.entries(r.recommendation_evidence ?? {})
                          .filter(([k]) => k !== "rules")
                          .map(([k, v]) => `${k}=${String(v)}`)
                          .join(" ")}
                      </div>
                      {admin && (
                        <div className="row">
                          <input
                            aria-label="decision note"
                            placeholder="why (10+ characters)"
                            value={notes[r.recommendation_id] ?? ""}
                            onChange={(e) => setNotes({ ...notes, [r.recommendation_id as number]: e.target.value })}
                          />
                          <button onClick={() => decide(r.recommendation_id as number, "confirm")}>Promote to tier 2</button>
                          <button onClick={() => decide(r.recommendation_id as number, "dismiss")}>Dismiss</button>
                        </div>
                      )}
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2>Risk score vs human decisions</h2>
      {report && (
        <div className="card" data-testid="tier-report">
          <p>
            Spot checks: {report.spot_checks.n} reviewed, {report.spot_checks.disagreements} overruled
            ({pct(report.spot_checks.rate)}). All low-risk videos: {report.low_risk_all.n} reviewed, {report.low_risk_all.disagreements} overruled
            ({pct(report.low_risk_all.rate)}).
          </p>
          <table>
            <thead><tr><th>Risk score</th><th>Reviewed</th><th>Rejected or edited</th></tr></thead>
            <tbody>
              {Object.entries(report.by_risk_bucket).map(([bucket, b]) => (
                <tr key={bucket}><td className="mono">{bucket}</td><td>{b.n}</td><td>{b.not_approved}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h2>Tier decisions</h2>
      <div className="card" style={{ padding: 0, overflowX: "auto" }}>
        <table>
          <thead><tr><th>When</th><th>Channel</th><th>Change</th><th>By</th><th>Reason</th></tr></thead>
          <tbody>
            {changes.map((c) => (
              <tr key={c.id} data-testid="tier-change">
                <td className="mono">{new Date(c.created_at).toLocaleString()}</td>
                <td className="mono">{c.channel_id}</td>
                <td>{c.kind} {c.from_tier} → {c.to_tier}</td>
                <td className="mono">{c.actor}</td>
                <td>{c.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
