"use client";

import { useState } from "react";
import { Badge, Button, Card, Notice, PageHead, SkeletonList, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { dateTime, pct } from "@/lib/format";
import { useSession } from "@/lib/session";
import { useApi } from "@/lib/useApi";

export default function TiersPage() {
  const { me } = useSession();
  const toast = useToast();
  const rows = useApi(() => api.tiers(), []);
  const changes = useApi(() => api.tierChanges(), []);
  const report = useApi(() => api.tierReport(), []);
  const [notes, setNotes] = useState<Record<number, string>>({});
  const admin = me?.role === "admin";

  const decide = async (id: number, kind: "confirm" | "dismiss") => {
    try {
      const note = notes[id] ?? "";
      await (kind === "confirm" ? api.confirmTier(id, note) : api.dismissTier(id, note));
      toast("ok", kind === "confirm" ? "Channel promoted to the risk-based tier." : "Recommendation dismissed.");
      await Promise.all([rows.reload(), changes.reload()]);
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };

  return (
    <div className="stack">
      <PageHead title="Review tiers" sub="Tier 1 reviews every video. Tier 2 adds a fast lane for low-risk videos (still one approval each). Promotion is a recommendation an admin confirms; any strike or rejection spike returns the channel to tier 1." />
      {rows.error && <Notice tone="err">{rows.error}</Notice>}
      {!rows.data && <SkeletonList />}
      {rows.data?.map((r) => (
        <Card key={r.channel_id} title={r.name || r.channel_id} action={<Badge tone={r.tier === 2 ? "info" : ""}>Tier {r.tier}</Badge>}>
          <div data-testid="tier-row" className="stack">
            <div className="faint small">{r.cluster_id ?? "no cluster"} · since {dateTime(r.tier_since)}{r.last_change_kind ? ` · ${r.last_change_kind}: ${r.last_change_reason}` : ""}</div>
            {r.recommendation_id !== null && (
              <div className="stack">
                <Notice tone="info">Recommended for tier 2: {Object.entries(r.recommendation_evidence ?? {}).filter(([k]) => k !== "rules").map(([k, v]) => `${k.replace(/_/g, " ")} ${String(v)}`).join(" · ")}</Notice>
                {admin && (
                  <div className="row">
                    <input className="input" style={{ flex: 1, minWidth: 180 }} aria-label="decision note" placeholder="Why (10+ characters)" value={notes[r.recommendation_id] ?? ""} onChange={(e) => setNotes({ ...notes, [r.recommendation_id as number]: e.target.value })} />
                    <Button variant="primary" onClick={() => void decide(r.recommendation_id as number, "confirm")}>Promote (passkey)</Button>
                    <Button onClick={() => void decide(r.recommendation_id as number, "dismiss")}>Dismiss</Button>
                  </div>
                )}
              </div>
            )}
          </div>
        </Card>
      ))}
      {report.data && (
        <Card title="Risk score vs human decisions">
          <div data-testid="tier-report" className="stack">
            <div className="small">Spot checks: {report.data.spot_checks.n} reviewed, {report.data.spot_checks.disagreements} overruled ({pct(report.data.spot_checks.rate)}). All low-risk: {report.data.low_risk_all.n} reviewed, {report.data.low_risk_all.disagreements} overruled ({pct(report.data.low_risk_all.rate)}).</div>
            <div className="table-wrap">
              <table className="table"><thead><tr><th>Risk score</th><th>Reviewed</th><th>Rejected or edited</th></tr></thead>
                <tbody>{Object.entries(report.data.by_risk_bucket).map(([b, v]) => <tr key={b}><td className="mono">{b}</td><td>{v.n}</td><td>{v.not_approved}</td></tr>)}</tbody></table>
            </div>
          </div>
        </Card>
      )}
      <Card title="Tier decisions" pad={false}>
        <div className="list">
          {changes.data?.map((c) => (
            <div key={c.id} className="item" data-testid="tier-change">
              <div className="item-main"><div className="item-title">{c.channel_id}: {c.kind} {c.from_tier} → {c.to_tier}</div><div className="item-sub">{c.actor} · {dateTime(c.created_at)} · {c.reason}</div></div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
