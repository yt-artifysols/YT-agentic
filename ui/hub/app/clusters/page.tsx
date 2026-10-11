"use client";

import { useState } from "react";
import { Badge, Button, Card, Field, Notice, PageHead, SkeletonList, useToast } from "@/components/ui";
import { api, type Gate } from "@/lib/api";
import { dateTime, relTime } from "@/lib/format";
import { useSession } from "@/lib/session";
import { useApi } from "@/lib/useApi";

export default function ClustersPage() {
  const { me } = useSession();
  const toast = useToast();
  const { data, error, loading, reload } = useApi(() => api.clusters(), []);
  const [gates, setGates] = useState<Record<string, Gate>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const staff = me?.role !== "approver";
  const act = async (fn: () => Promise<unknown>, ok: string) => {
    try {
      await fn();
      toast("ok", ok);
      await reload();
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };
  return (
    <div className="stack">
      <PageHead title="Clusters" sub="Cluster B cannot go live until cluster A has run cleanly and an admin signed off." />
      {error && <Notice tone="err">{error}</Notice>}
      {loading && !data && <SkeletonList />}
      {data?.map((c) => {
        const g = gates[c.id];
        return (
          <Card key={c.id} title={`${c.display_name || c.id}`} action={<Badge tone={c.live_enabled ? "ok" : ""}>{c.live_enabled ? `Live since ${c.live_since ? dateTime(c.live_since) : "?"}` : `Not live (needs ${c.requires_cluster ?? "nothing"})`}</Badge>}>
            <div data-testid="cluster-row" className="stack">
              <div className="muted small">{c.niche || "-"} · {c.channels} channels · {c.awaiting_approval} waiting{c.oldest_waiting_since ? ` · oldest ${relTime(c.oldest_waiting_since)}` : ""}</div>
              {c.health && <div className="faint small">Supervisor: {String(c.health.running)} running, {String(c.health.pending)} pending{c.health.budget_held ? " (budget held)" : ""}</div>}
              {staff && <div><Button size="small" onClick={() => void api.gate(c.id).then((x) => setGates({ ...gates, [c.id]: x }))}>Rollout gate report</Button></div>}
              {g && (
                <div className="stack">
                  <div className="small">Live {g.live_days.toFixed(1)} of {g.required_days} days · {g.decisions} decisions{g.rejection_rate !== null && ` (${Math.round(g.rejection_rate * 100)}% rejected)`} · {g.breaker_trips} breaker trips · {g.failed_uploads} failed uploads</div>
                  {g.problems.length ? <Notice tone="warn"><ul>{g.problems.map((p) => <li key={p}>{p}</li>)}</ul></Notice> : <Notice tone="ok">The measured evidence is good.</Notice>}
                  {me?.role === "admin" && (
                    <>
                      <Field label="Your review (20+ characters)">{(id) => <input id={id} className="input" value={notes[g.cluster_id] ?? ""} onChange={(e) => setNotes({ ...notes, [g.cluster_id]: e.target.value })} />}</Field>
                      <div className="row">
                        <Button disabled={!g.ok} onClick={() => void act(() => api.signoff(g.cluster_id, notes[g.cluster_id] ?? ""), "Signed off")}>Sign off</Button>
                        {data.filter((r) => r.requires_cluster === g.cluster_id && !r.live_enabled).map((r) => (
                          <Button key={r.id} variant="primary" onClick={() => void act(() => api.enableCluster(r.id), `${r.id} enabled`)}>Enable {r.id}</Button>
                        ))}
                      </div>
                    </>
                  )}
                </div>
              )}
            </div>
          </Card>
        );
      })}
    </div>
  );
}
