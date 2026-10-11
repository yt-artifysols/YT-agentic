"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type Controls } from "@/lib/api";
import { useMe } from "@/lib/useMe";

export default function ControlsPage() {
  const me = useMe();
  const [data, setData] = useState<Controls | null>(null);
  const [reason, setReason] = useState("");
  const [message, setMessage] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const load = useCallback(() => {
    api.controls().then(setData).catch((e: Error) => setMessage({ kind: "err", text: e.message }));
  }, []);
  useEffect(() => {
    if (me) load();
  }, [me, load]);
  if (!me || !data) return null;
  const canAct = me.role === "operator" || me.role === "admin";

  async function flip(label: string, paused: boolean, fn: () => Promise<unknown>) {
    if (paused && !window.confirm(`Pause publishing for ${label}?`)) return;
    try {
      await fn();
      setMessage({ kind: "ok", text: `${label}: publishing ${paused ? "paused" : "resumed"}` });
      load();
    } catch (e) {
      setMessage({ kind: "err", text: (e as Error).message });
    }
  }

  const toggle = (label: string, paused: boolean, fn: (p: boolean) => Promise<unknown>, testid: string) => (
    <button className={paused ? "primary" : "danger"} disabled={!canAct} data-testid={testid}
      onClick={() => void flip(label, !paused, () => fn(!paused))}>
      {paused ? "Resume" : "Pause"}
    </button>
  );

  return (
    <>
      <h1>Kill switches</h1>
      {!canAct && <div className="notice warn">Only operators and admins can change kill switches.</div>}
      {message && <div className={`notice ${message.kind}`} role="status">{message.text}</div>}
      <label style={{ maxWidth: 480, marginBottom: 12 }}>
        Reason (recorded in the audit log)
        <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. strike warning on channel" />
      </label>
      <div className="card">
        <div className="switch">
          <div><strong>Global</strong><div className="muted">Stops all publishing on every channel.</div></div>
          <div className="row">
            <span className={`badge ${data.global_paused ? "risk-high" : "risk-low"}`}>{data.global_paused ? "PAUSED" : "active"}</span>
            {toggle("all channels", data.global_paused, (p) => api.setGlobal(p, reason), "toggle-global")}
          </div>
        </div>
      </div>
      <h2>Clusters</h2>
      <div className="card">
        {data.clusters.map((c) => (
          <div className="switch" key={c.id}>
            <div><strong>{c.name}</strong> <span className="mono muted">{c.id}</span></div>
            <div className="row">
              <span className={`badge ${c.status === "paused" ? "risk-high" : "risk-low"}`}>{c.status}</span>
              {toggle(`cluster ${c.id}`, c.status === "paused", (p) => api.setScoped("clusters", c.id, p, reason), `toggle-cluster-${c.id}`)}
            </div>
          </div>
        ))}
      </div>
      <h2>Channels</h2>
      <div className="card">
        {data.channels.map((c) => (
          <div className="switch" key={c.id}>
            <div><strong>{c.name}</strong> <span className="mono muted">{c.id} · {c.cluster_id ?? "no cluster"} · {c.tier}</span></div>
            <div className="row">
              <span className={`badge ${c.status === "paused" ? "risk-high" : "risk-low"}`}>{c.status}</span>
              {toggle(`channel ${c.id}`, c.status === "paused", (p) => api.setScoped("channels", c.id, p, reason), `toggle-channel-${c.id}`)}
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
